"""Integration tests for :class:`QProcessRunner`.

This is the **only** test module that spawns a real child process. The runner is
signal plumbing — faking ``QProcess`` would test nothing — so the children here are
trivial ``sys.executable -c`` programs that need no external tools and no media,
and they run for a few hundred milliseconds at most. Everything else in the suite
keeps using :class:`~avchdforge.process.FakeRunner`.

The Qt side runs through pytest-qt's session ``qapp`` fixture on the offscreen
platform, so the tests behave the same in CI and on a desktop.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="QProcessRunner needs PySide6")
pytest.importorskip("pytestqt", reason="the Qt runner tests need pytest-qt")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore

from avchdforge.process import CompletedRun, QProcessRunner
from avchdforge.progress import TsmuxerScanner

pytestmark = pytest.mark.usefixtures("qapp")


def child(script: str) -> list[str]:
    return [sys.executable, "-c", script]


@pytest.fixture()
def qt_runner() -> QProcessRunner:
    return QProcessRunner()


def test_run_collects_streams_and_reports_lines(qt_runner: QProcessRunner) -> None:
    script = (
        "import sys\n"
        "print('out one', flush=True)\n"
        "print('err one', flush=True, file=sys.stderr)\n"
        "print('out two', flush=True)\n"
    )
    lines: list[str] = []

    result = qt_runner.run(child(script), on_line=lines.append)

    assert isinstance(result, CompletedRun)
    assert result.ok and result.returncode == 0
    assert result.stdout == "out one\nout two\n"
    assert result.stderr == "err one\n"
    # Both streams are streamed, complete lines keep their newline, and stderr is
    # not folded into stdout (the log pane shows them in arrival order, which is
    # not deterministic across pipes).
    assert sorted(lines) == ["err one\n", "out one\n", "out two\n"]
    assert result.argv == tuple(child(script))


def test_echo_is_used_when_no_line_callback() -> None:
    lines: list[str] = []
    runner = QProcessRunner(echo=lines.append)

    result = runner.run(child("print('hello')"))

    assert result.returncode == 0
    assert lines == ["hello\n"]


def test_exit_code_passes_through(qt_runner: QProcessRunner) -> None:
    result = qt_runner.run(child("import sys; sys.exit(7)"))

    assert result.returncode == 7
    assert not result.ok


def test_cwd_is_honoured(qt_runner: QProcessRunner, tmp_path: Path) -> None:
    result = qt_runner.run(
        child("import os; print(os.getcwd())"),
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(tmp_path)


def test_trailing_line_without_newline_is_flushed(qt_runner: QProcessRunner) -> None:
    lines: list[str] = []

    result = qt_runner.run(
        child("import sys; sys.stdout.write('no newline')"),
        on_line=lines.append,
    )

    assert result.stdout == "no newline"
    assert lines == ["no newline"]


def test_timeout_kills_the_child(qt_runner: QProcessRunner) -> None:
    started = time.monotonic()

    result = qt_runner.run(child("import time; time.sleep(30)"), timeout=0.5)

    assert time.monotonic() - started < 10
    assert result.returncode == -1
    assert "[avchd-forge] command timed out after 0.5s" in result.stderr


def test_cancel_kills_the_child(qt_runner: QProcessRunner) -> None:
    QtCore.QTimer.singleShot(200, qt_runner.cancel)

    result = qt_runner.run(child("import time; print('ready', flush=True); time.sleep(30)"))

    assert qt_runner.cancelled
    assert result.returncode == -1
    assert result.stdout == "ready\n"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX-only")
def test_crashed_child_reports_minus_one(qt_runner: QProcessRunner) -> None:
    result = qt_runner.run(
        child("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    )

    assert result.returncode == -1


def test_failed_start_explains_itself(qt_runner: QProcessRunner) -> None:
    result = qt_runner.run(["/definitely/not/a/real/tool-xyz"])

    assert result.returncode == -1
    assert "failed to start" in result.stderr


def test_second_run_while_active_raises(qt_runner: QProcessRunner) -> None:
    errors: list[Exception] = []

    def attempt() -> None:
        try:
            qt_runner.run(child("pass"))
        except RuntimeError as exc:
            errors.append(exc)

    QtCore.QTimer.singleShot(150, attempt)
    result = qt_runner.run(child("import time; time.sleep(0.8)"))

    assert result.returncode == 0
    assert len(errors) == 1
    assert "already running" in str(errors[0])


def test_scanner_sees_both_streams(qt_runner: QProcessRunner) -> None:
    scanner = TsmuxerScanner()
    fractions: list[float] = []

    result = qt_runner.run(
        child(
            "import sys\n"
            "print('7.0% complete', flush=True)\n"
            "print('Mux successful complete', flush=True, file=sys.stderr)\n"
        ),
        scanner=scanner,
        on_progress=fractions.append,
    )

    assert result.returncode == 0
    assert scanner.success and scanner.frames == 0
    assert fractions[-1] == 1.0


def test_in_place_terminal_reports_are_cleaned_and_tracked(
    qt_runner: QProcessRunner,
) -> None:
    """tsMuxeR's ``ESC M`` prefix must not hide a report from the progress bar."""
    lines: list[str] = []
    fractions: list[float] = []
    scanner = TsmuxerScanner()

    result = qt_runner.run(
        child(
            "print('0.0% complete', flush=True)\n"
            "print('\\x1bM24.5% complete', flush=True)\n"
            "print('\\x1bM49.0% complete', flush=True)\n"
            "print('Mux successful complete', flush=True)\n"
        ),
        on_line=lines.append,
        scanner=scanner,
        on_progress=fractions.append,
    )

    assert result.returncode == 0
    # the log pane and the evidence lose the reverse-index sequence ...
    assert lines == [
        "0.0% complete\n",
        "24.5% complete\n",
        "49.0% complete\n",
        "Mux successful complete\n",
    ]
    assert result.stdout == (
        "0.0% complete\n24.5% complete\n49.0% complete\nMux successful complete\n"
    )
    # ... and the scanner follows every report, not just the first
    assert pytest.approx([0.0, 0.245, 0.49, 1.0]) == fractions
    assert scanner.success


def test_rerun_after_cancel_clears_the_flag(qt_runner: QProcessRunner) -> None:
    qt_runner.cancel()  # idle cancel is a no-op that still raises the flag
    assert qt_runner.cancelled

    result = qt_runner.run(child("print('again')"))

    assert result.returncode == 0
    assert not qt_runner.cancelled
