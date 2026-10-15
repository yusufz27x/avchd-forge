"""Unit tests for the console entry point.

The argument parsing is pure and tested directly. ``main`` gets a fake window:
it would otherwise start an event loop, and the real window does all of this in
``tests/test_ui_main_window.py`` and ``tests/test_ui_tool_gate.py`` — what is
tested here is the *order* the entry point drives them in and the exit code when
the startup tool gate is quit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="__main__ builds a QApplication")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from avchdforge.__main__ import GATE_QUIT_EXIT_CODE, parse_args


def test_no_arguments_means_an_empty_window() -> None:
    args = parse_args([])

    assert args.file is None
    assert args.output is None


def test_a_source_and_an_output_can_be_preselected() -> None:
    args = parse_args(["movie.mkv", "--output", "/media/usb"])

    assert args.file == "movie.mkv"
    assert args.output == "/media/usb"


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--help"])

    assert exit_info.value.code == 0
    assert "avchd-forge" in capsys.readouterr().out


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    from avchdforge import __version__

    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


# --------------------------------------------------------------------------
# main: the startup order
# --------------------------------------------------------------------------


class FakeWindow:
    """Records what ``main`` asked of the window, in order."""

    def __init__(self, *, gate: bool = True) -> None:
        self.calls: list[str] = []
        self.gate = gate

    def ensure_tools(self) -> bool:
        self.calls.append("ensure_tools")
        return self.gate

    def refresh_drives(self) -> None:
        self.calls.append("refresh_drives")

    def set_output_dir(self, path: Path) -> None:
        self.calls.append(f"set_output_dir:{path}")

    def show(self) -> None:
        self.calls.append("show")

    def open_source(self, path: Path) -> None:
        self.calls.append(f"open_source:{path}")


def run_main(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], *, gate: bool = True
) -> tuple[int, FakeWindow]:
    from avchdforge import __main__ as entry
    from avchdforge.ui import main_window

    window = FakeWindow(gate=gate)
    monkeypatch.setattr(main_window, "MainWindow", lambda: window)
    monkeypatch.setattr(QtWidgets.QApplication, "exec", lambda self: 0)
    return entry.main(argv), window


def test_main_gates_the_tools_before_listing_drives_or_opening_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, window = run_main(monkeypatch, ["movie.mkv", "--output", "/media/usb"])

    assert code == 0
    assert window.calls == [
        "ensure_tools",
        "refresh_drives",
        f"set_output_dir:{Path('/media/usb')}",
        "show",
        f"open_source:{Path('movie.mkv')}",
    ]


def test_quitting_the_tool_gate_exits_without_opening_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, window = run_main(monkeypatch, ["movie.mkv"], gate=False)

    assert code == GATE_QUIT_EXIT_CODE == 1
    assert window.calls == ["ensure_tools"]  # no drives, no window, no probe
