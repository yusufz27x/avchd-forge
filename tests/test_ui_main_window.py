"""Unit tests for the main window.

The window is driven on a ``FakeRunner`` exactly like ``jobs.py`` is, so these
tests cover UI state and signal plumbing: what Open probes, what a Start press
builds, what the badge says about the target, and how a finished, failed or
cancelled run is reported. Nothing here spawns a process or opens a dialog.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="ui/main_window.py is Qt")
pytest.importorskip("pytestqt", reason="the ui tests use pytest-qt")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from avchdforge.drives import Drive
from avchdforge.selection import TrackSelection
from avchdforge.settings import AppSettings, settings_for
from avchdforge.tooling import FFMPEG, FFPROBE, TSMUXER, ToolInfo, ToolReport
from avchdforge.ui import theme
from avchdforge.ui.main_window import (
    MainWindow,
    drop_accepts,
    dropped_file,
    dropped_files,
    natural_key,
)
from avchdforge.ui.preferences import PreferencesDialog

from .conftest import (
    FakeResponse,
    FakeRunner,
    astream,
    dark_palette,
    load_json,
    text_contrast,
    vstream,
)
from .test_jobs import DETECTION, MUX_OK, PROBE_JSON_TEXT, fake_tree, output_payload

pytestmark = pytest.mark.usefixtures("qapp")

CAPABILITIES = {
    "-encoders": " V..... = Video\n S..... srt                  SubRip subtitle\n",
    "-bsfs": "dca_core\neac3_core\n",
    "-muxers": " E mpegts             MPEG-TS (MPEG-2 Transport Stream)\n",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_tools(tmp_path: Path) -> ToolReport:
    return ToolReport(
        tools=(
            ToolInfo(name=TSMUXER, path=tmp_path / "tsMuxeR", version="2.7.0"),
            ToolInfo(name=FFMPEG, path=tmp_path / "ffmpeg", version="7.1.5"),
            ToolInfo(name=FFPROBE, path=tmp_path / "ffprobe", version="7.1.5"),
        )
    )


def make_window(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    tools: bool = True,
    split: bool = True,
) -> MainWindow:
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    settings.set_temp_dir(tmp_path / "temp")
    settings.set_split_enabled(split)
    window = MainWindow(settings=settings, runner_factory=lambda: runner)
    if tools:
        window.set_tool_report(fake_tools(tmp_path))
    return window


def write_source(tmp_path: Path, name: str = "source.mkv") -> Path:
    source = tmp_path / name
    source.write_bytes(b"\x00")
    return source


def script_probe(runner: FakeRunner) -> None:
    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")


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


def script_success(runner: FakeRunner) -> None:
    """The window probes once to open the file, then the job probes again."""
    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")
    runner.add(
        FakeResponse(
            stdout=MUX_OK,
            match="tsmuxer-meta.txt",
            side_effect=lambda argv: fake_tree(Path(argv[-1])),
        )
    )
    runner.add_stdout(json.dumps(output_payload(vstream(0), astream(1))), match=".MTS")


def prepared_window(tmp_path: Path) -> tuple[MainWindow, FakeRunner, Path]:
    """A window with a probed source and a chosen output directory."""
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    source = write_source(tmp_path)
    window.open_source(source)
    window.set_output_dir(tmp_path / "out")
    return window, runner, source


# --------------------------------------------------------------------------
# gating
# --------------------------------------------------------------------------


def test_start_stays_disabled_until_source_output_and_tools_are_ready(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner, tools=False)

    assert not window.start_button.isEnabled()

    window.set_tool_report(fake_tools(tmp_path))
    assert not window.start_button.isEnabled()  # no source yet

    window.open_source(write_source(tmp_path))
    assert not window.start_button.isEnabled()  # no output yet

    window.set_output_dir(tmp_path / "out")
    assert window.start_button.isEnabled()

    window._set_busy(True)
    assert not window.start_button.isEnabled()
    assert window.cancel_button.isEnabled()


def test_missing_tool_keeps_start_disabled_and_says_so(tmp_path: Path) -> None:
    runner = FakeRunner()
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    settings.set_tool_path(TSMUXER, tmp_path / "gone-tsmuxeR")
    settings.set_tool_path(FFMPEG, make_executable(tmp_path / "ffmpeg"))
    settings.set_tool_path(FFPROBE, make_executable(tmp_path / "ffprobe"))
    runner.add_stdout("ffmpeg version 7.1.5", match="-version")
    runner.add_stdout("ffprobe version 7.1.5", match="-version")
    runner.add_stdout(CAPABILITIES["-encoders"], match="-encoders")
    runner.add_stdout(CAPABILITIES["-bsfs"], match="-bsfs")
    runner.add_stdout(CAPABILITIES["-muxers"], match="-muxers")
    window = MainWindow(settings=settings, runner_factory=lambda: runner)

    report = window.check_tools()

    assert not report.ok
    assert window.toolchain is None
    assert "missing external tools" in window.log_pane.text()
    assert "External tools missing" in window.start_button.toolTip()


def test_check_tools_resolves_the_toolchain_and_reads_capabilities(tmp_path: Path) -> None:
    runner = FakeRunner()
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    for name in (TSMUXER, FFMPEG, FFPROBE):
        settings.set_tool_path(name, make_executable(tmp_path / name))
    runner.add_stdout("tsMuxeR version 2.7.0", match="tsMuxeR")
    runner.add_stdout("ffmpeg version 7.1.5", match="-version")
    runner.add_stdout("ffprobe version 7.1.5", match="-version")
    runner.add_stdout(CAPABILITIES["-encoders"], match="-encoders")
    runner.add_stdout(CAPABILITIES["-bsfs"], match="-bsfs")
    runner.add_stdout(CAPABILITIES["-muxers"], match="-muxers")
    window = MainWindow(settings=settings, runner_factory=lambda: runner)

    report = window.check_tools()

    assert report.ok
    assert window.toolchain is not None
    assert window.toolchain.tsmuxer == tmp_path / "tsMuxeR"
    assert window.toolchain.capabilities.eac3_core_supported
    assert "tsMuxeR 2.7.0" in window.log_pane.text()


def test_a_tool_check_can_answer_without_adopting(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_tool_answers(runner)
    window = make_window(tmp_path, runner)  # already has good tools
    before = window.log_pane.text()
    overrides = {name: make_executable(tmp_path / name) for name in (FFMPEG, FFPROBE)}
    overrides[TSMUXER] = tmp_path / "elsewhere"

    report = window.check_tools(overrides, adopt=False)

    assert [tool.name for tool in report.missing] == [TSMUXER]  # the query answered ...
    assert len(runner.calls) == 5  # ... with the three versions minus tsMuxeR, plus capabilities
    assert window.tool_report is not None and window.tool_report.ok  # ... and changed nothing
    assert window.toolchain is not None
    assert window.log_pane.text() == before  # an unsaved path is not an event yet


# --------------------------------------------------------------------------
# preferences
# --------------------------------------------------------------------------


def test_changed_tool_paths_are_rechecked_and_adopted(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    script_tool_answers(runner)
    window = make_window(tmp_path, runner)
    settings = window._settings

    def fake_exec(_dialog: PreferencesDialog) -> int:
        for name in (TSMUXER, FFMPEG, FFPROBE):
            settings.set_tool_path(name, make_executable(tmp_path / name))
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(PreferencesDialog, "exec", fake_exec)

    assert window.open_preferences() is True

    assert window.toolchain is not None
    assert window.toolchain.tsmuxer == tmp_path / "tsMuxeR"
    assert "tsMuxeR 2.7.0" in window.log_pane.text()


def test_cancelling_preferences_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    window = make_window(tmp_path, runner)
    settings = window._settings

    def fake_exec(_dialog: PreferencesDialog) -> int:
        settings.set_tool_path(TSMUXER, tmp_path / "elsewhere")
        return QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(PreferencesDialog, "exec", fake_exec)

    assert window.open_preferences() is False

    assert settings.tool_path(TSMUXER) is not None  # the dialog wrote it ...
    assert runner.calls == []  # ... but no tool check ran, so nothing was adopted
    assert window.toolchain is not None  # the old report still stands


def test_a_language_priority_change_re_derives_the_track_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    window.open_source(write_source(tmp_path))
    assert window.track_view.selection() == TrackSelection(audio=(1,))
    settings = window._settings

    def fake_exec(_dialog: PreferencesDialog) -> int:
        settings.set_audio_priority("jpn")
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(PreferencesDialog, "exec", fake_exec)

    assert window.open_preferences() is True

    assert window.track_view.selection() == TrackSelection(audio=(2,))
    assert window.track_view.report.ok
    assert "defaults re-derived" in window.log_pane.text()


def test_a_split_change_re_assesses_the_target(tmp_path: Path, monkeypatch) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    window.set_drives(
        [Drive(device="/dev/sdb1", mountpoint=tmp_path, fstype="vfat", removable=True)]
    )
    settings = window._settings
    assert window.output_warning.text() == ""

    def fake_exec(_dialog: PreferencesDialog) -> int:
        settings.set_split_enabled(False)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(PreferencesDialog, "exec", fake_exec)

    window.open_preferences()

    assert "Enable splitting" in window.output_warning.text()


def test_a_window_cropped_source_is_repaired_not_blocked(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(
        json.dumps(load_json("mkv_window_crop_h264_ac3_srt.json")), match="ffprobe"
    )
    window = make_window(tmp_path, runner)
    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")

    report = window.track_view.report
    assert report.ok
    assert "V4" in {issue.code for issue in report.warnings}
    assert "1920x1080" in next(
        issue.message for issue in report.warnings if issue.code == "V4"
    )
    assert window.start_button.isEnabled()


def test_the_menu_closes_while_a_job_runs(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    assert window.preferences_action.isEnabled()

    window._set_busy(True)

    assert not window.preferences_action.isEnabled()
    assert not window.recheck_action.isEnabled()
    assert not window.versions_action.isEnabled()
    assert not window.open_action.isEnabled()

    window._set_busy(False)

    assert window.preferences_action.isEnabled()
    assert window.open_action.isEnabled()


# --------------------------------------------------------------------------
# source and probe
# --------------------------------------------------------------------------


def test_open_source_probes_and_fills_the_panes(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    source = write_source(tmp_path)

    assert window.open_source(source)

    outcome = window.probe_outcome
    assert outcome is not None and outcome.ok
    assert window.track_view.media is outcome.media
    assert window.track_view.selection() == TrackSelection(audio=(1,))
    assert window.source == source
    assert "source.mkv" in window.windowTitle()
    assert "source.mkv — 2 audio, 1 subtitle track(s)" in window.status_label.text()
    assert not window.busy
    assert [Path(call[0]).name for call in runner.calls] == ["ffprobe"]


def test_open_source_rejects_a_missing_file(tmp_path: Path) -> None:
    runner = FakeRunner()
    window = make_window(tmp_path, runner)

    assert not window.open_source(tmp_path / "nope.mkv")

    assert runner.calls == []
    assert window.source is None
    assert "No such file" in window.statusBar().currentMessage()


def test_open_source_refuses_without_tools(tmp_path: Path) -> None:
    runner = FakeRunner()
    window = make_window(tmp_path, runner, tools=False)

    assert not window.open_source(write_source(tmp_path))

    assert runner.calls == []
    assert "External tools are missing" in window.statusBar().currentMessage()


def test_an_unusable_file_disables_start_and_explains_why(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(json.dumps(load_json("mkv_1080p_hevc.json")), match="ffprobe")
    window = make_window(tmp_path, runner)

    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")

    assert window.probe_outcome is not None and not window.probe_outcome.ok
    assert not window.start_button.isEnabled()
    assert window.status_label.text().startswith("source.mkv cannot be used")
    assert "cannot be used" in window.start_button.toolTip()


def test_the_language_priority_from_preferences_picks_the_audio_default(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    script_probe(runner)
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    settings.set_audio_priority("jpn")
    window = MainWindow(settings=settings, runner_factory=lambda: runner)
    window.set_tool_report(fake_tools(tmp_path))

    window.open_source(write_source(tmp_path))

    assert window.track_view.selection() == TrackSelection(audio=(2,))


def test_preferences_tick_every_audio_track_in_the_preferred_languages(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    script_probe(runner)
    settings = AppSettings(settings_for(tmp_path / "prefs.ini"))
    settings.set_audio_priority("jpn, eng")
    window = MainWindow(settings=settings, runner_factory=lambda: runner)
    window.set_tool_report(fake_tools(tmp_path))

    window.open_source(write_source(tmp_path))

    # Both are preferred; the view reports them in stream order.
    assert window.track_view.selection() == TrackSelection(audio=(1, 2))


def test_unchecking_every_audio_track_blocks_start_again(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    assert window.start_button.isEnabled()

    window.track_view.set_checked("audio", 1, False)

    assert window.track_view.selection() == TrackSelection()
    assert window.track_view.report.ok is False
    assert not window.start_button.isEnabled()
    assert window.status_label.text().startswith("source.mkv cannot be used")


# --------------------------------------------------------------------------
# output and drives
# --------------------------------------------------------------------------


def test_output_without_a_known_drive_reports_unknown_filesystem(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)

    report = window.target_report()

    assert report is not None
    assert report.badge == "unknown filesystem"
    assert window.badge_label.text() == "unknown filesystem"
    assert "Could not identify the filesystem" in window.output_warning.text()


def test_output_on_a_fat32_drive_gets_the_badge_and_no_warning(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    drive = Drive(
        device="/dev/sdb1",
        mountpoint=tmp_path,
        fstype="vfat",
        label="PS3USB",
        removable=True,
        size_bytes=32 * 1024**3,
    )

    window.set_drives([drive])

    report = window.target_report()
    assert report is not None and report.is_fat32 and report.removable
    assert window.badge_label.text() == "FAT32"
    assert window.output_warning.text() == ""


def test_fat32_with_splitting_off_warns(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    window.set_drives(
        [Drive(device="/dev/sdb1", mountpoint=tmp_path, fstype="vfat", removable=True)]
    )

    window._settings.set_split_enabled(False)
    window.set_output_dir(tmp_path / "out")

    assert "Enable splitting" in window.output_warning.text()


def test_an_existing_avchd_tree_blocks_start_and_explains_why(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    (tmp_path / "out" / "BDMV").mkdir(parents=True)

    window.set_output_dir(tmp_path / "out")  # same folder: re-assessed explicitly

    assert not window.start_button.isEnabled()
    assert "already contains an AVCHD structure" in window.start_button.toolTip()
    assert "already contains an AVCHD structure" in window.output_warning.text()
    report = window.target_report()
    assert report is not None and not report.ok
    assert window.start() is False


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the read-only check uses POSIX modes and root ignores them",
)
def test_a_read_only_target_blocks_start(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    output.chmod(0o500)

    window.set_output_dir(output)

    assert not window.start_button.isEnabled()
    assert "not writable" in window.start_button.toolTip()


def test_a_drive_combo_entry_sets_the_output_folder(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    window.set_drives(
        [Drive(device="/dev/sdb1", mountpoint=tmp_path, fstype="vfat", label="PS3USB")]
    )
    window.set_output_dir(tmp_path.parent / "somewhere-else")
    assert window.drive_combo.currentIndex() == 0

    window.drive_combo.setCurrentIndex(1)

    assert window.output_dir() == tmp_path
    assert window.drive_combo.currentIndex() == 1


def test_choosing_a_path_selects_the_matching_drive(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    window.set_drives(
        [Drive(device="/dev/sdb1", mountpoint=tmp_path, fstype="vfat", label="PS3USB")]
    )

    window.set_output_dir(tmp_path / "out")

    assert window.drive_combo.currentIndex() == 1


# --------------------------------------------------------------------------
# the job request
# --------------------------------------------------------------------------


def test_job_request_carries_everything_the_pipeline_needs(tmp_path: Path) -> None:
    window, _runner, source = prepared_window(tmp_path)

    request = window.job_request()

    assert request is not None
    assert request.source == source
    assert request.output_dir == tmp_path / "out"
    assert request.selection == TrackSelection(audio=(1,))
    assert request.options.mux.split_size_mb == 4096
    assert request.options.tsmuxer == str(tmp_path / "tsMuxeR")
    assert request.options.ffmpeg == str(tmp_path / "ffmpeg")
    assert request.ffprobe == str(tmp_path / "ffprobe")
    assert request.temp_dir == tmp_path / "temp"
    assert request.keep_temp_on_success is False
    assert request.keep_temp_on_failure is True


def test_job_request_is_none_without_a_source_or_an_output(tmp_path: Path) -> None:
    runner = FakeRunner()
    window = make_window(tmp_path, runner)

    assert window.job_request() is None

    script_probe(runner)
    window.open_source(write_source(tmp_path))
    assert window.job_request() is None  # still no output


def test_split_disabled_removes_the_split_size(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner, split=False)
    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")

    request = window.job_request()

    assert request is not None and request.options.mux.split_size_mb is None


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def test_start_runs_the_job_and_reports_success(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")
    script_success(runner)
    runner.responses.pop(0)  # the probe already happened through the UI

    assert window.start()

    assert not window.busy
    assert window.stage_label.text() == "Done"
    assert window.status_label.text().startswith("Done — 1 stream part written to")
    assert window.progress_bar.value() == 1000
    assert window.open_output_button.isEnabled()
    assert window.open_output_action.isEnabled()
    assert window.temp_dir is None
    # the run wrote into the AVCHD folder it created, and that is remembered
    assert window._settings.last_output_dir() == tmp_path / "out" / "AVCHD"
    assert "done: 1 stream part written" in window.log_pane.text()


def test_a_finished_run_blocks_the_same_output_folder_again(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")
    script_success(runner)
    runner.responses.pop(0)  # the probe already happened through the UI

    assert window.start()

    assert not window.start_button.isEnabled()
    assert "already contains an AVCHD structure" in window.output_warning.text()


def test_cancel_can_be_pressed_from_the_mux_response(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")
    script_success(runner)
    runner.responses.pop(0)
    runner.responses[-2] = FakeResponse(
        stderr="killed",
        returncode=-9,
        match="tsmuxer-meta.txt",
        side_effect=lambda _argv: window.cancel(),
    )

    assert window.start()

    assert not window.busy
    assert window.stage_label.text() == "Cancelled"
    assert window.status_label.text().startswith("Cancelled")
    assert window.temp_dir is not None and window.temp_dir.exists()
    assert window.open_temp_button.isEnabled()


def test_a_failed_job_reports_the_title_and_keeps_evidence(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")
    script_success(runner)
    runner.responses.pop(0)
    runner.responses[-2] = FakeResponse(
        stderr="Something went wrong", returncode=1, match="tsmuxer-meta.txt"
    )

    assert window.start()

    assert window.stage_label.text() == "Failed"
    assert window.status_label.text() == "tsMuxeR failed"
    assert "exit code 1" in window.status_label.toolTip()
    assert window.temp_dir is not None and window.temp_dir.exists()
    assert window.open_temp_button.isEnabled()


def test_a_new_run_wipes_the_previous_result_line(tmp_path: Path) -> None:
    window, runner, _source = prepared_window(tmp_path)
    script_success(runner)
    runner.responses.pop(0)  # the probe already happened through the UI
    runner.responses[-2] = FakeResponse(
        stderr="Something went wrong", returncode=1, match="tsmuxer-meta.txt"
    )
    assert window.start()
    assert window.status_label.text() == "tsMuxeR failed"
    assert "exit code 1" in window.status_label.toolTip()

    # The next run owns the line: neither the failure text nor its tooltip may
    # survive into it (the stage label says what is happening meanwhile).
    script_success(runner)
    seen: list[tuple[str, str]] = []

    def capture_then_mux(argv: Sequence[str]) -> None:
        seen.append((window.status_label.text(), window.status_label.toolTip()))
        fake_tree(Path(argv[-1]))

    runner.responses[-2] = FakeResponse(
        stdout=MUX_OK, match="tsmuxer-meta.txt", side_effect=capture_then_mux
    )

    assert window.start()

    assert seen == [("", "")]  # mid-run: wiped at start, still wiped
    assert window.status_label.text().startswith("Done — ")
    assert window.status_label.toolTip() == ""


def test_the_failure_tooltip_clears_with_the_failure_line(tmp_path: Path) -> None:
    window, runner, _source = prepared_window(tmp_path)
    script_success(runner)
    runner.responses.pop(0)
    runner.responses[-2] = FakeResponse(
        stderr="Something went wrong", returncode=1, match="tsmuxer-meta.txt"
    )
    assert window.start()
    assert window.status_label.toolTip()

    window.set_output_dir(tmp_path / "other")  # any settled state resets the line

    assert window.status_label.text().startswith("source.mkv —")
    assert window.status_label.toolTip() == ""


def test_the_stage_text_stays_readable_on_a_dark_palette(tmp_path: Path, qapp) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    original = qapp.palette()
    qapp.setPalette(dark_palette())
    try:
        # ``palette(mid)`` on Breeze-Dark-shaped colours: text == background.
        assert text_contrast(window.stage_label) > 3
    finally:
        qapp.setPalette(original)


def test_the_badge_and_target_warning_stay_readable_on_a_dark_palette(
    tmp_path: Path, qapp
) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    assert window.output_warning.text()

    original = qapp.palette()
    qapp.setPalette(dark_palette())
    try:
        qapp.processEvents()
        colors = theme.severity_colors(qapp.palette())
        assert window.badge_label.styleSheet() == f"color: {colors.muted};"
        assert window.output_warning.styleSheet() == f"color: {colors.warning};"
        assert text_contrast(window.badge_label) > 3
        assert text_contrast(window.output_warning) > 3

        window.set_drives(
            [
                Drive(
                    device="/dev/sdb1",
                    mountpoint=tmp_path,
                    fstype="vfat",
                    label="PS3USB",
                    removable=True,
                )
            ]
        )
        qapp.processEvents()
        assert window.badge_label.styleSheet() == f"color: {colors.ok};"
        assert text_contrast(window.badge_label) > 3

        (tmp_path / "out" / "BDMV").mkdir(parents=True)
        window.set_output_dir(tmp_path / "out")
        qapp.processEvents()
        assert window.output_warning.styleSheet() == f"color: {colors.error};"
        assert text_contrast(window.output_warning) > 3
    finally:
        qapp.setPalette(original)


def test_start_without_a_toolchain_does_nothing(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    window.set_tool_report(ToolReport(tools=(ToolInfo(name=TSMUXER),)))
    assert window.toolchain is None
    _runner.responses.clear()
    _runner.calls.clear()

    assert not window.start()

    assert not window.busy
    assert _runner.calls == []


def test_starting_a_job_opens_the_log_pane(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    script_success(_runner)
    _runner.responses.pop(0)
    window.log_pane.set_collapsed(True)

    window.start()

    assert not window.log_pane.collapsed


def test_progress_updates_the_bar_and_unmeasurable_stages_go_busy(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)

    window._on_progress(0.42)
    assert window.progress_bar.value() == 420

    window._on_progress(None)
    assert window.progress_bar.minimum() == 0 and window.progress_bar.maximum() == 0

    window._on_progress(1.0)
    assert window.progress_bar.value() == 1000


# --------------------------------------------------------------------------
# drag and drop
# --------------------------------------------------------------------------


def mime_with(*paths: Path) -> QtCore.QMimeData:
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def test_drop_helpers_take_one_or_more_local_media_files(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    other = write_source(tmp_path, "other.mkv")
    text = write_source(tmp_path, "notes.txt")

    assert dropped_files(mime_with(source)) == (source,)
    assert dropped_files(mime_with(source, other)) == (source, other)
    assert dropped_files(QtCore.QMimeData()) == ()
    assert dropped_file(mime_with(source)) == source
    assert dropped_file(mime_with(source, other)) is None  # a season needs the list

    assert drop_accepts(mime_with(source))
    assert drop_accepts(mime_with(source, other))
    assert not drop_accepts(mime_with(text))
    assert not drop_accepts(mime_with(source, text))  # every file must be usable
    assert not drop_accepts(mime_with(tmp_path))  # a directory is not a source


def test_natural_key_reads_numbers_as_numbers(tmp_path: Path) -> None:
    names = ["S01E10.mkv", "S01E2.mkv", "S01E02.mkv"]
    order = sorted(names, key=lambda name: natural_key(Path(name)))

    assert order == ["S01E2.mkv", "S01E02.mkv", "S01E10.mkv"]


def test_dropping_a_file_probes_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    source = write_source(tmp_path)

    assert window.handle_drop(mime_with(source)) == (source,)

    assert [episode.path for episode in window.episodes] == [source]
    assert window.source == source
    assert window.probe_outcome is not None


def test_dropping_several_files_adds_one_episode_each(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    script_probe(runner)
    window = make_window(tmp_path, runner)
    source = write_source(tmp_path)
    other = write_source(tmp_path, "other.mkv")

    dropped = window.handle_drop(mime_with(source, other))

    # A drop carries no meaningful order, so the list sorts by name — and a
    # second file is a season, not a replacement.
    assert dropped == (source, other)
    assert [episode.path for episode in window.episodes] == [other, source]
    assert window.source is None  # two episodes are not "the source"
    assert [call for call in runner.calls if "-show_streams" in call]  # probed both
    assert "2 episodes" in window.status_label.text()


# --------------------------------------------------------------------------
# seasons
# --------------------------------------------------------------------------


def script_season(runner: FakeRunner, names: Sequence[str]) -> None:
    """Everything a season run asks of tools, per file: two probes, detect, mux.

    The window probes a file when it is added and the job probes it again when
    Start builds the episode's plan, so both are scripted — matching on the file
    name keeps the two episodes' answers apart whatever order they are consumed
    in. The staged trees keep tsMuxeR's lowercase ``.m2ts`` names until the merge
    renames them, which is why the verification probe matches lowercase here.
    """
    for name in names:
        runner.add_stdout(PROBE_JSON_TEXT, match=name)  # the window's probe
        runner.add_stdout(PROBE_JSON_TEXT, match=name)  # the plan's own probe
        runner.add_stdout(DETECTION, match=name)
        runner.add(
            FakeResponse(
                stdout=MUX_OK,
                match="tsmuxer-meta.txt",
                side_effect=lambda argv: fake_tree(Path(argv[-1])),
            )
        )
        runner.add_stdout(json.dumps(output_payload(vstream(0), astream(1))), match=".m2ts")


def season_window(
    tmp_path: Path, *, names: Sequence[str] = ("EP01.mkv", "EP02.mkv")
) -> tuple[MainWindow, FakeRunner, tuple[Path, ...]]:
    """A window with a probed season listed and an output folder chosen."""
    runner = FakeRunner()
    script_season(runner, names)
    window = make_window(tmp_path, runner)
    episodes = tuple(write_source(tmp_path, name) for name in names)
    window.add_sources(episodes)
    window.set_output_dir(tmp_path / "out")
    return window, runner, episodes


def test_a_season_is_probed_one_episode_at_a_time(tmp_path: Path) -> None:
    window, runner, episodes = season_window(tmp_path, names=("EP02.mkv", "EP01.mkv"))

    # Added out of order: the list is the play order, so it sorts by name.
    assert [episode.path for episode in window.episodes] == [episodes[1], episodes[0]]
    assert all(episode.outcome is not None for episode in window.episodes)
    assert len(runner.calls) == 2  # one probe each, no mux yet
    assert window.probe_outcome is window.episodes[1].outcome  # the highlighted row
    assert window.start_button.isEnabled()
    assert "2 episodes" in window.status_label.text()


def test_each_episode_keeps_its_own_selection(tmp_path: Path) -> None:
    window, _runner, _episodes = season_window(tmp_path)

    window.episode_list.setCurrentRow(0)
    window.track_view.set_checked("audio", 2, True)  # keep both audio tracks, title 1
    window.track_view.set_checked("audio", 1, False)
    first = window.track_view.selection()

    window.episode_list.setCurrentRow(1)

    assert window.track_view.selection() == TrackSelection(audio=(1,))  # untouched

    window.episode_list.setCurrentRow(0)

    assert window.track_view.selection() == first == TrackSelection(audio=(2,))

    request = window.season_request()
    assert request is not None
    assert request.selections == (TrackSelection(audio=(2,)), TrackSelection(audio=(1,)))
    assert window.job_request() is None  # one file at a time is the single flow


def test_start_runs_a_season_into_one_title_per_episode(tmp_path: Path) -> None:
    window, runner, _episodes = season_window(tmp_path)

    assert window.start()

    assert [episode.path.name for episode in window.episodes] == ["EP01.mkv", "EP02.mkv"]
    # per episode: plan probe, detection, mux, verification probe
    assert len(runner.calls) == 2 + 4 * 2
    assert "starting season: 2 episodes" in window.log_pane.text()
    assert "2 titles" in window.status_label.text()
    assert not window.busy
    # each title got its own clip and playlist, renumbered by the merge
    bdmv = tmp_path / "out" / "AVCHD" / "BDMV"
    assert sorted(path.name for path in (bdmv / "STREAM").iterdir()) == [
        "00000.MTS",
        "00001.MTS",
    ]
    assert sorted(path.name for path in (bdmv / "PLAYLIST").iterdir()) == [
        "00000.MPL",
        "00001.MPL",
    ]
    assert "00001M2TS" in (bdmv / "PLAYLIST" / "00001.MPL").read_bytes().decode("ascii")


def test_removing_and_reordering_episodes_rewrites_the_title_order(tmp_path: Path) -> None:
    window, _runner, episodes = season_window(tmp_path)

    window.episode_list.setCurrentRow(0)
    assert window.move_down_button.isEnabled() and not window.move_up_button.isEnabled()

    assert window.move_episode(1)

    assert [episode.path for episode in window.episodes] == [episodes[1], episodes[0]]
    request = window.season_request()
    assert request is not None and request.sources == (episodes[1], episodes[0])

    assert window.remove_episode()

    assert [episode.path for episode in window.episodes] == [episodes[1]]
    assert window.source == episodes[1]  # one file again: the single flow
    assert "removed EP01.mkv" in window.log_pane.text()

    assert window.remove_episode()
    assert window.episodes == ()
    assert window.source is None and not window.start_button.isEnabled()
    assert "open one or more" in window.status_label.text()


def test_an_episode_that_cannot_be_read_is_marked_and_blocks_start(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_season(runner, ("EP00.mkv",))
    runner.responses.insert(
        0,
        FakeResponse(
            stderr="moov atom not found", returncode=1, match="EP01.mkv"
        ),
    )
    window = make_window(tmp_path, runner)
    good = write_source(tmp_path, "EP00.mkv")
    bad = write_source(tmp_path, "EP01.mkv")
    window.add_sources((good, bad))
    window.set_output_dir(tmp_path / "out")

    assert len(runner.calls) == 2  # one bad file does not stop the queue
    assert window.episodes[0].failure is None
    assert window.episodes[1].failure == "Could not read the input file"
    assert not window.start_button.isEnabled()
    assert "EP01.mkv cannot be read" in window.status_label.text()
    assert "EP01.mkv cannot be read" in window.start_button.toolTip()
    assert "EP01.mkv" in window.episode_list.item(1).text()


def test_adding_the_same_file_twice_lists_it_once(tmp_path: Path) -> None:
    window, runner, episodes = season_window(tmp_path, names=("EP01.mkv",))

    assert window.add_sources((episodes[0], episodes[0])) is False

    assert [episode.path for episode in window.episodes] == [episodes[0]]
    assert len(runner.calls) == 1  # only the first add probed


def test_new_language_priorities_re_derive_every_episode(tmp_path: Path) -> None:
    window, _runner, _episodes = season_window(tmp_path)
    window.episode_list.setCurrentRow(0)
    window.track_view.set_checked("audio", 2, True)
    window.track_view.set_checked("audio", 1, False)
    assert window.episodes[0].selection == TrackSelection(audio=(2,))

    window._reselect_defaults()  # what Preferences ▸ Apply calls after a change

    assert all(
        episode.selection == TrackSelection(audio=(1,)) for episode in window.episodes
    )
    assert "defaults re-derived" in window.log_pane.text()


def test_open_source_after_a_season_replaces_the_list(tmp_path: Path) -> None:
    window, runner, _episodes = season_window(tmp_path)
    runner.add_stdout(PROBE_JSON_TEXT, match="EP03.mkv")
    replacement = write_source(tmp_path, "EP03.mkv")

    assert window.open_source(replacement)

    assert [episode.path for episode in window.episodes] == [replacement]
    assert window.source == replacement
    assert "EP01.mkv" not in [episode.path.name for episode in window.episodes]


def test_cancelling_a_read_marks_the_row_and_pauses_the_queue(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add(
        FakeResponse(
            stderr="killed",
            returncode=-9,
            match="EP01.mkv",
            side_effect=lambda _argv: runner.cancel(),
        )
    )
    runner.add_stdout(PROBE_JSON_TEXT, match="EP02.mkv")
    window = make_window(tmp_path, runner)
    first = write_source(tmp_path, "EP01.mkv")
    second = write_source(tmp_path, "EP02.mkv")

    window.add_sources((first, second))

    assert len(runner.calls) == 1  # the cancelled read stops the queue
    assert window.episodes[0].failure == "Reading cancelled"
    assert "EP01.mkv — Reading cancelled" in window.episode_list.item(0).text()
    assert "not read yet" in window.episode_list.item(1).text()
    assert window.status_label.text().startswith("Cancelled")
    assert not window.start_button.isEnabled()
    assert "EP02.mkv has not been read yet" in window.start_button.toolTip()

    # Dropping the cancelled row pokes the queue, and the rest is read.
    window.episode_list.setCurrentRow(0)
    window.set_output_dir(tmp_path / "out")

    assert window.remove_episode()

    assert len(runner.calls) == 2
    assert [episode.path for episode in window.episodes] == [second]
    assert "0:00:04" in window.episode_list.item(0).text()
    assert window.start_button.isEnabled()


# --------------------------------------------------------------------------
# window state
# --------------------------------------------------------------------------


def test_closing_saves_geometry_and_log_visibility(tmp_path: Path) -> None:
    ini = tmp_path / "prefs.ini"
    runner = FakeRunner()
    window = MainWindow(settings=settings_for(ini), runner_factory=lambda: runner)
    window.log_pane.set_collapsed(False)
    window.show()

    window.close()

    stored = AppSettings(settings_for(ini))
    assert stored.geometry() is not None
    assert stored.window_state() is not None
    assert stored.log_visible is True


def test_the_saved_log_visibility_is_restored(tmp_path: Path) -> None:
    ini = tmp_path / "prefs.ini"
    prefs = AppSettings(settings_for(ini))
    prefs.set_log_visible(False)

    window = MainWindow(settings=settings_for(ini), runner_factory=FakeRunner)

    assert window.log_pane.collapsed
