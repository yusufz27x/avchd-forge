"""Unit tests for the Preferences dialog.

The dialog is an editor: it loads the stored preferences into its widgets and
writes them back on OK. Both halves are public (:meth:`PreferencesDialog.reload`
and :meth:`PreferencesDialog.apply`) exactly so these tests need no modal event
loop. Only the confirmation of *Restore defaults* and the two pickers are faked;
the one live part — *Check now* — is driven through an injected checker that
records what it was asked, which is the same seam ``MainWindow`` passes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="ui/preferences.py is Qt")
pytest.importorskip("pytestqt", reason="the ui tests use pytest-qt")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from avchdforge.backend import FAT32_MAX_BYTES, SPLIT_MB_BYTES, SPLIT_OVERSHOOT_ALLOWANCE
from avchdforge.constants import (
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_NAME,
    DEFAULT_FONT_SIZE,
    DEFAULT_SPLIT_SIZE_MB,
)
from avchdforge.errors import ToolNotFoundError
from avchdforge.prep import SubtitleStyle
from avchdforge.settings import AppSettings, settings_for
from avchdforge.tooling import FFMPEG, FFPROBE, TSMUXER, ToolInfo, ToolReport
from avchdforge.ui import theme
from avchdforge.ui.preferences import (
    MAX_SPLIT_SIZE_MB,
    STATUS_BAD,
    STATUS_OK,
    STATUS_WARN,
    TABS,
    PreferencesDialog,
)

from .conftest import dark_palette, text_contrast

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_dialog(
    tmp_path: Path, **kwargs: object
) -> tuple[PreferencesDialog, AppSettings]:
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    return PreferencesDialog(settings, None, **kwargs), settings


def tool_report() -> ToolReport:
    """One of each state the status column has to render."""
    return ToolReport(
        tools=(
            ToolInfo(name=TSMUXER, path=Path("/usr/bin/tsMuxeR"), version="2.7.0"),
            ToolInfo(
                name=FFMPEG,
                path=Path("/usr/bin/ffmpeg"),
                version="3.4.1",
                warning="ffmpeg 3.4.1 is older than the supported 4.0.0",
            ),
            ToolInfo(
                name=FFPROBE,
                searched=("/opt/ffprobe",),
                reason="/opt/ffprobe exists but is not executable (chmod +x it)",
            ),
        )
    )


# --------------------------------------------------------------------------
# tabs
# --------------------------------------------------------------------------


def test_the_dialog_opens_on_the_paths_tab_or_on_the_one_that_was_asked_for(
    tmp_path: Path,
) -> None:
    dialog, _settings = make_dialog(tmp_path)
    assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == "Paths"
    assert [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())] == [
        label for _name, label in TABS
    ]

    dialog.show_tab("tools")
    assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == "External tools"

    with pytest.raises(KeyError):
        dialog.show_tab("nope")

    tools_first, _settings = make_dialog(tmp_path, tab="tools")
    assert tools_first.tabs.tabText(tools_first.tabs.currentIndex()) == "External tools"


# --------------------------------------------------------------------------
# load / save
# --------------------------------------------------------------------------


def test_the_dialog_starts_from_the_stored_preferences(tmp_path: Path) -> None:
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    settings.set_temp_dir(tmp_path / "scratch")
    settings.set_keep_temp_on_success(True)
    settings.set_keep_temp_on_failure(False)
    settings.set_tool_path(TSMUXER, "/opt/tsMuxeR/tsMuxeR")
    settings.set_split_enabled(False)
    settings.set_split_size_mb(2048)
    settings.set_subtitle_style(
        SubtitleStyle(
            font_name="Noto Sans", font_size=70, font_color="0xEBFFFFFF", bottom_offset=30
        )
    )
    settings.set_audio_priority("eng, jpn")
    settings.set_subtitle_priority("eng")

    dialog = PreferencesDialog(settings)

    assert dialog.temp_edit.text() == str(tmp_path / "scratch")
    assert dialog.keep_success_check.isChecked()
    assert not dialog.keep_failure_check.isChecked()
    assert dialog.tool_edits[TSMUXER].text() == "/opt/tsMuxeR/tsMuxeR"
    assert dialog.tool_edits[FFMPEG].text() == ""
    assert not dialog.split_check.isChecked()
    assert dialog.split_spin.value() == 2048
    assert not dialog.split_spin.isEnabled()  # splitting is off
    assert dialog.font_name_edit.text() == "Noto Sans"
    assert dialog.font_size_spin.value() == 70
    assert dialog.font_color_edit.text() == "0xEBFFFFFF"
    assert dialog.bottom_offset_spin.value() == 30
    assert dialog.audio_priority_edit.text() == "eng, jpn"
    assert dialog.subtitle_priority_edit.text() == "eng"


def test_a_fresh_dialog_shows_the_defaults(tmp_path: Path) -> None:
    dialog, _settings = make_dialog(tmp_path)

    assert dialog.temp_edit.text() == ""
    assert not dialog.keep_success_check.isChecked()
    assert dialog.keep_failure_check.isChecked()
    assert all(edit.text() == "" for edit in dialog.tool_edits.values())
    assert dialog.split_check.isChecked()
    assert dialog.split_spin.value() == DEFAULT_SPLIT_SIZE_MB
    assert dialog.split_spin.isEnabled()
    assert dialog.font_name_edit.text() == DEFAULT_FONT_NAME
    assert dialog.font_size_spin.value() == DEFAULT_FONT_SIZE
    assert dialog.font_color_edit.text() == DEFAULT_FONT_COLOR
    assert dialog.audio_priority_edit.text() == ""
    assert dialog.subtitle_priority_edit.text() == ""


def test_apply_writes_every_widget_back_to_the_preferences(tmp_path: Path) -> None:
    dialog, settings = make_dialog(tmp_path)
    dialog.temp_edit.setText(str(tmp_path / "scratch"))
    dialog.keep_success_check.setChecked(True)
    dialog.keep_failure_check.setChecked(False)
    for name in (TSMUXER, FFMPEG, FFPROBE):
        dialog.tool_edits[name].setText(f"/opt/{name}")
    dialog.split_check.setChecked(True)
    dialog.split_spin.setValue(2000)
    dialog.font_name_edit.setText("  Noto Sans  ")
    dialog.font_size_spin.setValue(48)
    dialog.font_color_edit.setText("0xEBFFFFFF")
    dialog.bottom_offset_spin.setValue(12)
    dialog.audio_priority_edit.setText("ENG, jpn")
    dialog.subtitle_priority_edit.setText("eng, jpn")

    dialog.apply()

    assert settings.temp_dir() == tmp_path / "scratch"
    assert settings.keep_temp_on_success is True
    assert settings.keep_temp_on_failure is False
    assert settings.tool_path(TSMUXER) == Path("/opt/tsMuxeR")
    assert settings.tool_path(FFMPEG) == Path("/opt/ffmpeg")
    assert settings.tool_path(FFPROBE) == Path("/opt/ffprobe")
    assert settings.split_enabled is True
    assert settings.split_size_mb() == 2000
    assert settings.subtitle_style() == SubtitleStyle(
        font_name="Noto Sans", font_size=48, font_color="0xEBFFFFFF", bottom_offset=12
    )
    assert settings.audio_priority() == ("eng", "jpn")  # lower-cased, trimmed
    assert settings.subtitle_priority() == ("eng", "jpn")


def test_empty_boxes_mean_no_override_and_the_system_temp_dir(tmp_path: Path) -> None:
    dialog, settings = make_dialog(tmp_path)
    settings.set_temp_dir(tmp_path / "scratch")
    settings.set_tool_path(FFMPEG, "/opt/ffmpeg")
    dialog.reload()  # the dialog follows changes made elsewhere

    assert dialog.temp_edit.text() == str(tmp_path / "scratch")
    assert dialog.tool_edits[FFMPEG].text() == "/opt/ffmpeg"

    dialog.temp_edit.setText("   ")
    dialog.tool_edits[FFMPEG].setText("")
    dialog.font_name_edit.setText("")
    dialog.apply()

    assert settings.temp_dir() is None
    assert settings.tool_overrides() == {}
    assert settings.subtitle_style().font_name is None  # "let tsMuxeR decide"


def test_ok_saves_but_cancel_does_not(tmp_path: Path) -> None:
    dialog, settings = make_dialog(tmp_path)
    dialog.font_size_spin.setValue(48)

    dialog.reject()

    assert settings.subtitle_style().font_size == DEFAULT_FONT_SIZE
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Rejected

    dialog.font_size_spin.setValue(48)
    dialog.accept()

    assert settings.subtitle_style().font_size == 48
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted


def test_restore_defaults_asks_first_and_then_forgets_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog, settings = make_dialog(tmp_path)
    settings.set_temp_dir(tmp_path / "scratch")
    settings.set_tool_path(TSMUXER, "/opt/tsMuxeR")
    settings.set_split_enabled(False)
    settings.set_audio_priority("jpn")
    dialog.reload()
    dialog.set_tool_report(tool_report())

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.StandardButton.No,
    )
    dialog.restore_defaults()
    assert settings.tool_path(TSMUXER) == Path("/opt/tsMuxeR")  # a "no" changes nothing

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.StandardButton.Yes,
    )
    dialog.restore_defaults()

    assert settings.temp_dir() is None
    assert settings.tool_overrides() == {}
    assert settings.split_enabled is True
    assert settings.split_size_mb() == DEFAULT_SPLIT_SIZE_MB
    assert settings.audio_priority() == ()
    assert dialog.temp_edit.text() == ""
    assert dialog.tool_edits[TSMUXER].text() == ""
    assert dialog.split_check.isChecked()
    assert dialog.audio_priority_edit.text() == ""
    assert dialog.tool_status[TSMUXER].text() == "not checked yet"


# --------------------------------------------------------------------------
# the muxing tab
# --------------------------------------------------------------------------


def test_the_split_size_box_follows_the_split_checkbox(tmp_path: Path) -> None:
    dialog, _settings = make_dialog(tmp_path)
    assert dialog.split_spin.isEnabled()

    dialog.split_check.setChecked(False)
    assert not dialog.split_spin.isEnabled()

    dialog.split_check.setChecked(True)
    assert dialog.split_spin.isEnabled()


def test_the_split_limit_cannot_produce_a_chunk_above_the_fat32_ceiling() -> None:
    assert MAX_SPLIT_SIZE_MB * SPLIT_MB_BYTES + SPLIT_OVERSHOOT_ALLOWANCE <= FAT32_MAX_BYTES
    assert DEFAULT_SPLIT_SIZE_MB <= MAX_SPLIT_SIZE_MB, "the default must not be clamped"


# --------------------------------------------------------------------------
# external tools
# --------------------------------------------------------------------------


def test_the_tool_column_shows_versions_warnings_and_reasons(tmp_path: Path) -> None:
    dialog, _settings = make_dialog(tmp_path)
    assert all(label.text() == "not checked yet" for label in dialog.tool_status.values())
    assert all(not label.styleSheet() for label in dialog.tool_status.values())

    dialog.set_tool_report(tool_report())

    assert dialog.tool_status[TSMUXER].text() == "2.7.0 — /usr/bin/tsMuxeR"
    assert dialog.tool_status[TSMUXER].styleSheet() == STATUS_OK
    assert dialog.tool_status[FFMPEG].text() == "ffmpeg 3.4.1 is older than the supported 4.0.0"
    assert dialog.tool_status[FFMPEG].styleSheet() == STATUS_WARN
    assert (
        dialog.tool_status[FFPROBE].text()
        == "/opt/ffprobe exists but is not executable (chmod +x it)"
    )
    assert dialog.tool_status[FFPROBE].styleSheet() == STATUS_BAD


def test_the_tool_column_stays_readable_after_a_switch_to_dark_mode(
    tmp_path: Path, qapp
) -> None:
    dialog, _settings = make_dialog(tmp_path)
    dialog.set_tool_report(tool_report())
    dialog.show_tab("tools")
    dialog.show()
    original = qapp.palette()

    qapp.setPalette(dark_palette())
    try:
        qapp.processEvents()
        colors = theme.severity_colors(qapp.palette())
        assert dialog.tool_status[TSMUXER].styleSheet() == f"color: {colors.ok};"
        assert dialog.tool_status[FFMPEG].styleSheet() == f"color: {colors.warning};"
        assert dialog.tool_status[FFPROBE].styleSheet() == f"color: {colors.error};"
        for label in dialog.tool_status.values():
            assert text_contrast(label) > 3
    finally:
        dialog.close()
        qapp.setPalette(original)


def test_check_now_passes_the_typed_paths_and_stores_nothing(tmp_path: Path) -> None:
    seen: list[dict[str, Path]] = []

    def fake_check(overrides: object) -> ToolReport:
        seen.append(dict(overrides))  # type: ignore[arg-type]
        return tool_report()

    dialog, settings = make_dialog(tmp_path, tool_check=fake_check)
    dialog.tool_edits[TSMUXER].setText(" /opt/tsMuxeR ")
    dialog.tool_edits[FFMPEG].setText("")

    report = dialog.check_now()

    assert seen == [{TSMUXER: Path("/opt/tsMuxeR")}]  # empty boxes mean "search as usual"
    assert report is not None and report.tools[0].name == TSMUXER
    assert dialog.tool_status[TSMUXER].styleSheet() == STATUS_OK
    assert settings.tool_overrides() == {}  # nothing was saved
    assert settings.tool_path(TSMUXER) is None


def test_check_now_without_a_checker_just_answers_none(tmp_path: Path) -> None:
    dialog, _settings = make_dialog(tmp_path)

    assert dialog.check_now() is None

    assert dialog.tool_status[TSMUXER].text() == "not checked yet"


def test_a_failing_tool_check_is_shown_instead_of_crashing_the_dialog(tmp_path: Path) -> None:
    def broken(_overrides: object) -> ToolReport:
        raise ToolNotFoundError(TSMUXER, ("/opt/tsMuxeR",), "not executable")

    dialog, _settings = make_dialog(tmp_path, tool_check=broken)

    assert dialog.check_now() is None

    assert "not executable" in dialog.tool_status[TSMUXER].text()
    assert dialog.tool_status[FFMPEG].styleSheet() == STATUS_BAD


# --------------------------------------------------------------------------
# pickers
# --------------------------------------------------------------------------


def test_the_pickers_fill_the_boxes_and_return_the_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog, _settings = make_dialog(tmp_path)
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getExistingDirectory",
        lambda *args, **kwargs: str(tmp_path / "scratch"),
    )
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(tmp_path / "tsMuxeR"), "Executables (*)"),
    )

    assert dialog.choose_temp_dir() == tmp_path / "scratch"
    assert dialog.temp_edit.text() == str(tmp_path / "scratch")

    assert dialog.choose_tool(TSMUXER) == tmp_path / "tsMuxeR"
    assert dialog.tool_edits[TSMUXER].text() == str(tmp_path / "tsMuxeR")


def test_a_cancelled_picker_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog, _settings = make_dialog(tmp_path)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))

    assert dialog.choose_temp_dir() is None
    assert dialog.temp_edit.text() == ""

    assert dialog.choose_tool(FFMPEG) is None
    assert dialog.tool_edits[FFMPEG].text() == ""
