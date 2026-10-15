"""Unit tests for the startup tool gate and the window's gate loop.

Two halves: the pure wording and the dialog's buttons (no modal event loop — the
class is driven through its own ``exec`` override and direct ``click()`` calls),
and ``MainWindow.ensure_tools``, whose loop is exercised by faking the dialog's
answer. Repairing the paths in Preferences is simulated by patching
``PreferencesDialog.exec``, which still runs the real comparison in
``MainWindow.open_preferences``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="ui/tool_gate.py is Qt")
pytest.importorskip("pytestqt", reason="the ui tests use pytest-qt")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from avchdforge.settings import AppSettings, settings_for
from avchdforge.tooling import FFMPEG, FFPROBE, TSMUXER, ToolInfo, ToolReport
from avchdforge.ui.main_window import MainWindow
from avchdforge.ui.preferences import PreferencesDialog
from avchdforge.ui.tool_gate import (
    CONTINUE,
    PREFERENCES,
    QUIT,
    RECHECK,
    ToolGateDialog,
    gate_text,
    searched_text,
)

from .conftest import FakeRunner
from .test_ui_main_window import CAPABILITIES, make_executable

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------------------
# report and text builders
# --------------------------------------------------------------------------


def found(name: str, version: str = "1.0.0") -> ToolInfo:
    return ToolInfo(name=name, path=Path("/usr/bin") / name, version=version)


def missing(name: str, *, reason: str = "") -> ToolInfo:
    return ToolInfo(
        name=name,
        searched=(f"{name} (PATH)", f"/usr/local/bin/{name}"),
        reason=reason,
    )


def ok_report() -> ToolReport:
    return ToolReport(tools=(found(TSMUXER, "2.7.0"), found(FFMPEG, "7.1.5"), found(FFPROBE)))


def missing_report(reason: str = "") -> ToolReport:
    return ToolReport(tools=(missing(TSMUXER, reason=reason), found(FFMPEG), found(FFPROBE)))


def warned_report() -> ToolReport:
    return ToolReport(
        tools=(
            ToolInfo(
                name=TSMUXER,
                path=Path("/usr/bin/tsMuxeR"),
                version="2.5.0",
                warning="tsMuxeR 2.5.0 is older than the supported 2.6.0",
            ),
            found(FFMPEG),
            found(FFPROBE),
        )
    )


# --------------------------------------------------------------------------
# gate_text / searched_text
# --------------------------------------------------------------------------


def test_gate_text_names_every_tool_and_what_to_do_about_the_missing_one() -> None:
    text = gate_text(missing_report(reason="/opt/tsMuxeR exists but is not executable"))
    lines = text.splitlines()

    assert lines[0].startswith("avchd-forge remuxes with tsMuxeR")
    assert "tsMuxeR: not found — /opt/tsMuxeR exists but is not executable" in lines
    assert "ffmpeg 1.0.0 — /usr/bin/ffmpeg" in lines
    assert "ffprobe 1.0.0 — /usr/bin/ffprobe" in lines
    assert "Preferences" in text and "Re-check" in text
    assert "Start stays disabled" in text


def test_gate_text_reports_warnings_without_calling_them_missing() -> None:
    text = gate_text(warned_report())

    assert "warning: tsMuxeR 2.5.0 is older than the supported 2.6.0" in text
    assert "not found" not in text
    assert "Nothing is missing" in text


def test_searched_text_lists_where_only_the_missing_tool_was_looked_for() -> None:
    text = searched_text(missing_report())

    assert text.startswith("tsMuxeR:\n")
    assert "/usr/local/bin/tsMuxeR" in text
    assert "ffmpeg" not in text

    assert searched_text(ok_report()) == ""


# --------------------------------------------------------------------------
# the dialog
# --------------------------------------------------------------------------


def test_a_missing_tool_offers_recheck_preferences_and_quit_only() -> None:
    dialog = ToolGateDialog(missing_report(), None)

    assert dialog.continue_button is None
    assert dialog.recheck_button.text() == "Re-check"
    assert dialog.preferences_button.text() == "Preferences…"
    assert dialog.quit_button.text() == "Quit"
    assert dialog.defaultButton() is dialog.recheck_button
    assert dialog.escapeButton() is dialog.quit_button
    assert dialog.icon() == QtWidgets.QMessageBox.Icon.Critical
    assert dialog.textFormat() == QtCore.Qt.TextFormat.PlainText
    assert "tsMuxeR: not found" in dialog.text()
    assert "/usr/local/bin/tsMuxeR" in dialog.detailedText()
    assert dialog.report is not None and not dialog.report.ok


def test_a_usable_report_may_be_continued_and_has_no_details_to_show() -> None:
    dialog = ToolGateDialog(warned_report(), None)

    assert dialog.continue_button is not None
    assert dialog.defaultButton() is dialog.continue_button
    assert dialog.escapeButton() is dialog.quit_button
    assert dialog.icon() == QtWidgets.QMessageBox.Icon.Warning
    assert "older than the supported 2.6.0" in dialog.text()
    assert dialog.detailedText() == ""


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [("recheck_button", RECHECK), ("preferences_button", PREFERENCES), ("quit_button", QUIT)],
)
def test_every_button_yields_its_own_choice(attribute: str, expected: str) -> None:
    dialog = ToolGateDialog(missing_report(), None)

    getattr(dialog, attribute).click()

    assert dialog.choice() == expected


def test_continue_yields_continue_when_it_is_offered() -> None:
    dialog = ToolGateDialog(warned_report(), None)
    assert dialog.continue_button is not None

    dialog.continue_button.click()

    assert dialog.choice() == CONTINUE


def test_closing_the_gate_any_other_way_means_quit() -> None:
    dialog = ToolGateDialog(missing_report(), None)

    assert dialog.choice() == QUIT  # Esc, the window manager, a dying parent


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [("recheck_button", RECHECK), ("preferences_button", PREFERENCES), ("quit_button", QUIT)],
)
def test_ask_returns_the_pressed_choice(
    attribute: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def press_the_button(self: ToolGateDialog) -> int:
        getattr(self, attribute).click()
        return 0

    monkeypatch.setattr(ToolGateDialog, "exec", press_the_button)

    assert ToolGateDialog.ask(missing_report(), None) == expected


# --------------------------------------------------------------------------
# the window's gate loop
# --------------------------------------------------------------------------


def script_tool_answers(
    runner: FakeRunner, *, times: int = 1, tsmuxer: str = "tsMuxeR version 2.7.0"
) -> None:
    """Everything ``check_toolchain`` asks of a resolved toolchain, ``times`` over."""
    for _ in range(times):
        runner.add_stdout(tsmuxer, match="tsMuxeR")
        runner.add_stdout("ffmpeg version 7.1.5", match="-version")
        runner.add_stdout("ffprobe version 7.1.5", match="-version")
        runner.add_stdout(CAPABILITIES["-encoders"], match="-encoders")
        runner.add_stdout(CAPABILITIES["-bsfs"], match="-bsfs")
        runner.add_stdout(CAPABILITIES["-muxers"], match="-muxers")


def broken_window(
    tmp_path: Path, runner: FakeRunner
) -> tuple[MainWindow, AppSettings]:
    """A window whose three tool overrides point at files that do not exist."""
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    for name in (TSMUXER, FFMPEG, FFPROBE):
        settings.set_tool_path(name, tmp_path / f"missing-{name}")
    window = MainWindow(settings=settings, runner_factory=lambda: runner)
    return window, settings


def working_window(tmp_path: Path, runner: FakeRunner) -> MainWindow:
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    for name in (TSMUXER, FFMPEG, FFPROBE):
        settings.set_tool_path(name, make_executable(tmp_path / name))
    return MainWindow(settings=settings, runner_factory=lambda: runner)


def record_answers(
    monkeypatch: pytest.MonkeyPatch, *answers: str
) -> list[ToolReport]:
    """Answer the gate with ``answers`` in turn (the last one repeats)."""
    asked: list[ToolReport] = []
    remaining = list(answers)

    def fake_ask(report: ToolReport, parent: object = None) -> str:
        asked.append(report)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr(ToolGateDialog, "ask", fake_ask)
    return asked


def test_a_clean_check_passes_the_gate_without_asking(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    script_tool_answers(runner)
    window = working_window(tmp_path, runner)
    asked = record_answers(monkeypatch, CONTINUE)

    assert window.ensure_tools() is True

    assert asked == []
    assert window.toolchain is not None
    assert window.toolchain.tsmuxer == tmp_path / "tsMuxeR"
    assert len(runner.calls) == 6  # three versions, three capability queries


def test_a_warned_toolchain_is_offered_continue_once(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    script_tool_answers(runner, tsmuxer="tsMuxeR version 2.5.0")
    window = working_window(tmp_path, runner)
    asked = record_answers(monkeypatch, CONTINUE)

    assert window.ensure_tools() is True

    assert len(asked) == 1
    report = asked[0]
    assert report.ok and report.warnings  # usable, but worth mentioning
    assert "older than the supported 2.6.0" in gate_text(report)
    assert window.toolchain is not None


def test_quitting_the_gate_abandons_the_startup(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    window, _settings = broken_window(tmp_path, runner)
    asked = record_answers(monkeypatch, QUIT)

    assert window.ensure_tools() is False

    assert len(asked) == 1
    assert [tool.name for tool in asked[0].missing] == [TSMUXER, FFMPEG, FFPROBE]
    assert window.toolchain is None
    assert runner.calls == []  # a broken override never spawns anything


def test_rechecking_a_still_broken_toolchain_asks_again(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    window, _settings = broken_window(tmp_path, runner)
    asked = record_answers(monkeypatch, RECHECK, QUIT)

    assert window.ensure_tools() is False

    assert len(asked) == 2
    assert all(not report.ok for report in asked)


def test_fixing_the_paths_in_preferences_satisfies_the_gate(
    tmp_path: Path, monkeypatch
) -> None:
    runner = FakeRunner()
    window, settings = broken_window(tmp_path, runner)
    script_tool_answers(runner, times=2)  # open_preferences re-checks, the loop re-checks
    asked = record_answers(monkeypatch, PREFERENCES)
    opened: list[PreferencesDialog] = []

    def fake_exec(dialog: PreferencesDialog) -> int:
        opened.append(dialog)
        for name in (TSMUXER, FFMPEG, FFPROBE):
            settings.set_tool_path(name, make_executable(tmp_path / name))
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(PreferencesDialog, "exec", fake_exec)

    assert window.ensure_tools() is True

    assert len(asked) == 1 and not asked[0].ok  # asked once, with the broken report
    assert window.toolchain is not None  # the repaired paths were adopted
    # The gate's shortcut lands on the tab that sets tool paths, with the report in hand.
    assert len(opened) == 1
    assert opened[0].tabs.tabText(opened[0].tabs.currentIndex()) == "External tools"
    assert opened[0].tool_status[TSMUXER].text() == "not found"
    # Two clean checks ran: the one open_preferences does when the paths changed,
    # and the gate's own re-check before it decides to let the window open.
    assert [Path(call[0]).name for call in runner.calls_to("tsMuxeR")] == ["tsMuxeR", "tsMuxeR"]
    assert "tsMuxeR 2.7.0" in window.log_pane.text()


def test_a_cancelled_preferences_dialog_rechecks_nothing_new(
    tmp_path: Path, monkeypatch
) -> None:
    runner = FakeRunner()
    window, _settings = broken_window(tmp_path, runner)
    asked = record_answers(monkeypatch, PREFERENCES, QUIT)

    monkeypatch.setattr(
        PreferencesDialog, "exec", lambda _dialog: QtWidgets.QDialog.DialogCode.Rejected
    )

    assert window.ensure_tools() is False

    assert len(asked) == 2  # nothing changed, so the gate comes back
    assert runner.calls == []
