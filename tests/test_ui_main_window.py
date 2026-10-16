"""Unit tests for the main window.

The window is driven on a ``FakeRunner`` exactly like ``jobs.py`` is, so these
tests cover UI state and signal plumbing: what Open probes, what a Start press
builds, what the badge says about the target, and how a finished, failed or
cancelled run is reported. Nothing here spawns a process or opens a dialog.
"""

from __future__ import annotations

import json
import os
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
from avchdforge.ui.main_window import MainWindow, drop_accepts, dropped_file
from avchdforge.ui.preferences import PreferencesDialog

from .conftest import FakeResponse, FakeRunner, astream, load_json, vstream
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


def test_allowing_non_standard_resolutions_unblocks_a_cropped_source(
    tmp_path: Path, monkeypatch
) -> None:
    runner = FakeRunner()
    runner.add_stdout(
        json.dumps(output_payload(vstream(0, height=1036), astream(1))), match="ffprobe"
    )
    window = make_window(tmp_path, runner)
    window.open_source(write_source(tmp_path))
    window.set_output_dir(tmp_path / "out")

    assert not window.track_view.report.ok
    assert not window.start_button.isEnabled()
    assert "1920x1036" in window.start_button.toolTip()

    def fake_exec(_dialog: PreferencesDialog) -> int:
        window._settings.set_allow_nonstandard_resolution(True)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(PreferencesDialog, "exec", fake_exec)

    assert window.open_preferences() is True

    assert window.track_view.report.ok
    assert "V4" in {issue.code for issue in window.track_view.report.warnings}
    assert window.start_button.isEnabled()
    assert "resolution policy changed" in window.log_pane.text()


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
    assert window.status_label.text().startswith("This file cannot be used")
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


def test_unchecking_every_audio_track_blocks_start_again(tmp_path: Path) -> None:
    window, _runner, _source = prepared_window(tmp_path)
    assert window.start_button.isEnabled()

    window.track_view.set_checked("audio", 1, False)

    assert window.track_view.selection() == TrackSelection()
    assert window.track_view.report.ok is False
    assert not window.start_button.isEnabled()
    assert window.status_label.text().startswith("This file cannot be used")


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


def test_drop_helpers_accept_exactly_one_local_media_file(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    other = write_source(tmp_path, "other.mkv")
    text = write_source(tmp_path, "notes.txt")

    assert dropped_file(mime_with(source)) == source
    assert dropped_file(mime_with(source, other)) is None
    assert dropped_file(QtCore.QMimeData()) is None

    assert drop_accepts(mime_with(source))
    assert not drop_accepts(mime_with(text))
    assert not drop_accepts(mime_with(source, other))
    assert not drop_accepts(mime_with(tmp_path))  # a directory is not a source


def test_dropping_a_file_probes_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_probe(runner)
    window = make_window(tmp_path, runner)
    source = write_source(tmp_path)

    assert window.handle_drop(mime_with(source)) == source

    assert window.source == source
    assert window.probe_outcome is not None


def test_dropping_several_files_changes_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    window = make_window(tmp_path, runner)
    source = write_source(tmp_path)
    other = write_source(tmp_path, "other.mkv")

    assert window.handle_drop(mime_with(source, other)) is None

    assert window.source is None
    assert runner.calls == []
    assert "Ignored the drop" in window.statusBar().currentMessage()


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
