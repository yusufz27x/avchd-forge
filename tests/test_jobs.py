"""Unit tests for :mod:`avchdforge.jobs` (Qt job orchestration).

Everything runs on a ``FakeRunner`` injected through ``runner_factory``: these
tests are about signal plumbing, the temp-file policy and cancellation routing,
not about Qt's process layer (``test_qt_process.py`` owns that).

Signals are connected as plain callables and fire synchronously, because
``start_*`` runs on the calling thread exactly as it does in the GUI.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6", reason="jobs.py is Qt orchestration")
pytest.importorskip("pytestqt", reason="the job tests use pytest-qt")

from avchdforge.backend import SeasonOutcome
from avchdforge.capabilities import FfmpegCapabilities
from avchdforge.errors import ExistingTreeError, PlanError, ValidationFailed
from avchdforge.jobs import (
    PLAN_STAGE_LABEL,
    TEMP_PREFIX,
    JobCancellation,
    JobFailure,
    JobRequest,
    JobRunner,
    ProbeOutcome,
    SeasonRequest,
    build_job_plan,
    describe_media,
    describe_outcome,
    describe_season_outcome,
)
from avchdforge.plan import PlanOptions
from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import TrackSelection

from .conftest import (
    DATA_DIR,
    FakeResponse,
    FakeRunner,
    astream,
    load_json,
    load_text,
    sstream,
    vstream,
)

pytestmark = pytest.mark.usefixtures("qapp")

#: The 1080p H.264 + two AC-3 + SubRip fixture, as ffprobe sees it.
PROBE_JSON_TEXT = json.dumps(load_json("mkv_1080p_h264_ac3x2_srt.json"))
#: tsMuxeR's detection report for that same file (Track IDs 1..4).
DETECTION = load_text("detect_mkv.txt")
#: The success sentinel tsMuxeR prints when a mux finished normally.
MUX_OK = "22.2% complete\n100.0% complete\nMux successful complete\n"


# --------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------


def output_payload(*streams: dict[str, Any]) -> dict[str, Any]:
    """ffprobe-shaped JSON for the produced ``.m2ts``."""
    return {
        "streams": list(streams),
        "format": {
            "format_name": "mpegts",
            "duration": "4.004000",
            "size": "1603584",
            "bit_rate": "3200000",
        },
    }


def fake_tree(output_dir: Path) -> Path:
    """Write the AVCHD layout tsMuxeR is supposed to produce.

    Faithful enough for the season merge too: the playlist names the episode's
    own clip (``00000M2TS``) and BACKUP mirrors PLAYLIST and CLIPINF, the way
    tsMuxeR's real output does.
    """
    bdmv = output_dir / "BDMV"
    for name in ("PLAYLIST", "CLIPINF", "STREAM", "BACKUP/PLAYLIST", "BACKUP/CLIPINF"):
        (bdmv / name).mkdir(parents=True, exist_ok=True)
    for base in (bdmv, bdmv / "BACKUP"):
        (base / "index.bdmv").write_bytes(b"IDX0200")
        (base / "MovieObject.bdmv").write_bytes(b"MOBJ0200")
        (base / "PLAYLIST" / "00000.mpls").write_bytes(b"MPLS0200\x00\x50" + b"00000M2TS")
        (base / "CLIPINF" / "00000.clpi").write_bytes(b"HDMV0200")
    stream = bdmv / "STREAM" / "00000.m2ts"
    stream.write_bytes(b"\x00" * 4096)
    return stream


class JobWatcher:
    """Collect everything a :class:`JobRunner` emits, in arrival order."""

    def __init__(self, job: JobRunner) -> None:
        self.events: list[str] = []
        self.stages: list[Any] = []
        self.progress: list[Any] = []
        self.lines: list[str] = []
        self.probed: list[ProbeOutcome] = []
        self.outcomes: list[Any] = []
        self.failures: list[JobFailure] = []
        self.cancellations: list[JobCancellation] = []
        job.stage_changed.connect(self._stage)
        job.progress.connect(self.progress.append)
        job.line.connect(self.lines.append)
        job.probed.connect(self._probed)
        job.finished.connect(self._finished)
        job.failed.connect(self._failed)
        job.cancelled.connect(self._cancelled)

    def _stage(self, stage: Any) -> None:
        self.stages.append(stage)
        self.events.append(f"stage:{stage.key}")

    def _probed(self, outcome: ProbeOutcome) -> None:
        self.probed.append(outcome)
        self.events.append("probed")

    def _finished(self, outcome: Any) -> None:
        self.outcomes.append(outcome)
        self.events.append("finished")

    def _failed(self, failure: JobFailure) -> None:
        self.failures.append(failure)
        self.events.append("failed")

    def _cancelled(self, cancellation: JobCancellation) -> None:
        self.cancellations.append(cancellation)
        self.events.append("cancelled")

    @property
    def stage_keys(self) -> list[str]:
        return [stage.key for stage in self.stages]

    @property
    def text(self) -> str:
        return "".join(self.lines)


def make_job(runner: FakeRunner) -> JobRunner:
    return JobRunner(runner_factory=lambda: runner)


def request_for(tmp_path: Path, **overrides: Any) -> JobRequest:
    values: dict[str, Any] = {
        "source": tmp_path / "source.mkv",
        "output_dir": tmp_path / "out",
        "selection": TrackSelection(audio=(1,)),
        "temp_dir": tmp_path / "temp",
    }
    values.update(overrides)
    return JobRequest(**values)


def script_success(runner: FakeRunner) -> None:
    """Script the four calls of a successful audio-only run, in order.

    Responses are consumed in order, so the first entry whose ``match`` appears in
    the argv wins: the source probe is ``ffprobe``, the detection pass is the only
    tsMuxeR call that mentions the source itself, the mux is the only one that
    mentions the meta file, and the output probe is the only ``ffprobe`` of a
    ``.MTS`` (the backend renames tsMuxeR's ``.m2ts`` before verifying).
    """

    def write_tree(argv: list[str]) -> None:
        fake_tree(Path(argv[-1]))

    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")
    runner.add(FakeResponse(stdout=MUX_OK, match="tsmuxer-meta.txt", side_effect=write_tree))
    runner.add_stdout(_json(output_payload(vstream(0), astream(1))), match=".MTS")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def sps_dump(name: str) -> str:
    """A real ``ffprobe -show_data`` extradata dump from ``tests/data/sps``."""
    return (DATA_DIR / "sps" / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# probe phase
# --------------------------------------------------------------------------


def test_probe_reports_media_and_default_selection(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_probe(tmp_path / "source.mkv")

    assert watcher.events == ["stage:probe", "probed"]
    outcome = watcher.probed[0]
    assert outcome.ok
    assert outcome.source == tmp_path / "source.mkv"
    assert outcome.media.duration_label == "0:00:04"
    assert outcome.media.audio[1].language == "jpn"
    # smart defaults: first compatible audio, text subtitles off
    assert outcome.selection == TrackSelection(audio=(1,))
    assert not job.busy
    assert "video    #0 H264" in watcher.text
    assert "subs     #3 SubRip eng" in watcher.text


def test_probe_failure_is_reported_with_a_title(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_error("moov atom not found", returncode=1, match="ffprobe")
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_probe(tmp_path / "broken.mkv")

    assert watcher.events == ["stage:probe", "failed"]
    failure = watcher.failures[0]
    assert not failure.internal
    assert failure.title == "Could not read the input file"
    assert "moov atom not found" in failure.message
    assert failure.temp_dir is None
    assert not job.busy


def test_probe_flags_unusable_media_without_failing(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(_json(load_json("mkv_1080p_hevc.json")), match="ffprobe")
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_probe(tmp_path / "hevc.mkv")

    outcome = watcher.probed[0]
    assert not outcome.ok
    assert watcher.failures == []
    assert any("H.264" in message for message in outcome.errors)


def test_terminal_signals_arrive_with_the_runner_released(tmp_path: Path) -> None:
    """A handler may start the next run: the runner is free before the signal."""
    runner = FakeRunner()
    runner.add_error("not a media file", returncode=1, match="ffprobe")
    job = make_job(runner)
    watcher = JobWatcher(job)
    busy_in_handler: list[bool] = []
    job.failed.connect(lambda _failure: busy_in_handler.append(job.busy))

    job.start_probe(tmp_path / "broken.mkv")

    assert watcher.events == ["stage:probe", "failed"]
    assert busy_in_handler == [False]
    assert not job.busy


def test_a_handler_may_start_the_next_probe(tmp_path: Path) -> None:
    """The window's probe queue reads a list of files exactly like this."""
    runner = FakeRunner()
    runner.add_error("not a media file", returncode=1, match="first.mkv")
    runner.add_stdout(PROBE_JSON_TEXT, match="second.mkv")
    job = make_job(runner)
    watcher = JobWatcher(job)
    job.failed.connect(lambda _failure: job.start_probe(tmp_path / "second.mkv"))

    job.start_probe(tmp_path / "first.mkv")

    assert [outcome.source.name for outcome in watcher.probed] == ["second.mkv"]
    assert [failure.title for failure in watcher.failures] == [
        "Could not read the input file"
    ]
    assert not job.busy


def test_cancelled_probe_is_reported_as_cancellation(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add(
        FakeResponse(
            stderr="killed",
            returncode=-9,
            match="ffprobe",
            side_effect=lambda _argv: runner.cancel(),
        )
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_probe(tmp_path / "source.mkv")

    assert watcher.events == ["stage:probe", "cancelled"]
    assert watcher.cancellations[0].stage == "probing"
    assert watcher.failures == []


# --------------------------------------------------------------------------
# mux phase
# --------------------------------------------------------------------------


def test_job_runs_detection_mux_and_verification(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    job = make_job(runner)
    watcher = JobWatcher(job)
    output_dir = tmp_path / "out"

    job.start_job(request_for(tmp_path))

    assert watcher.events[-1] == "finished"
    assert watcher.failures == [] and watcher.cancellations == []
    outcome = watcher.outcomes[0]
    # the plan writes into the AVCHD folder it creates inside the chosen folder
    assert outcome.plan.output_dir == output_dir / "AVCHD"
    assert [path.name for path in outcome.stream_files] == ["00000.MTS"]
    assert outcome.verified_file == output_dir / "AVCHD" / "BDMV" / "STREAM" / "00000.MTS"
    # the job layer ships the AVCHD-on-FAT32 names the PS3 mounts from USB
    assert (output_dir / "AVCHD" / "BDMV" / "INDEX.BDM").is_file()
    assert not (output_dir / "AVCHD" / "BDMV" / "index.bdmv").exists()
    # the plan stage comes from here, the rest from the backend
    assert watcher.stage_keys == ["plan", "mux", "verify", "cleanup"]
    assert watcher.stages[0].label == PLAN_STAGE_LABEL
    # and the calls happen in the documented order
    assert [Path(call[0]).name for call in runner.calls] == [
        "ffprobe",
        "tsMuxeR",
        "tsMuxeR",
        "ffprobe",
    ]
    detection, mux = runner.calls[1], runner.calls[2]
    assert detection[1].endswith("source.mkv")
    assert mux[1].endswith("tsmuxer-meta.txt")
    assert mux[2] == str(output_dir / "AVCHD")
    # the log pane shows what the plan will do
    assert "splitting  on (4096 MB)" in watcher.text
    assert "done: 1 stream part written" in watcher.text
    # progress runs from 0 to 1 and passes a busy value during verification
    assert watcher.progress[0] == 0.0
    assert watcher.progress[-1] == 1.0
    assert None in watcher.progress


def test_job_runs_prep_steps_before_the_mux(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")
    runner.add_stdout("", match="ffmpeg")
    runner.add_stdout(_json(output_payload(sstream(0, "subrip"))), match=".srt")

    def write_tree(argv: list[str]) -> None:
        fake_tree(Path(argv[-1]))

    runner.add(FakeResponse(stdout=MUX_OK, match="tsmuxer-meta.txt", side_effect=write_tree))
    runner.add_stdout(
        _json(output_payload(vstream(0), astream(1), sstream(2, "hdmv_pgs_subtitle"))),
        match=".MTS",
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(
        request_for(tmp_path, selection=TrackSelection(audio=(1,), subtitles=(3,)))
    )

    assert watcher.events[-1] == "finished"
    tools = [Path(call[0]).name for call in runner.calls]
    assert tools == ["ffprobe", "tsMuxeR", "ffmpeg", "ffprobe", "tsMuxeR", "ffprobe"]
    prepare = [stage for stage in watcher.stages if stage.key == "prepare"]
    assert [stage.label for stage in prepare] == ["normalise SubRip to UTF-8 for tsMuxeR"]
    assert watcher.stage_keys.count("prepare") == 1


def test_job_creates_its_temp_dir_under_the_configured_root(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    seen: list[Path] = []

    def remember(argv: list[str]) -> None:
        seen.append(Path(argv[1]).parent)

    runner.responses[-2].side_effect = _wrap(remember)
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path))

    assert watcher.outcomes, watcher.failures
    assert len(seen) == 1
    assert seen[0].parent == tmp_path / "temp"
    assert seen[0].name.startswith(TEMP_PREFIX)
    # a successful run cleans its temp dir unless asked to keep it
    assert not seen[0].exists()


def test_temp_dir_falls_back_to_the_system_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    script_success(runner)
    system = tmp_path / "system-temp"
    system.mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(system))
    seen: list[Path] = []

    runner.responses[-2].side_effect = _wrap(
        lambda argv: seen.append(Path(argv[1]).parent)
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path, temp_dir=None))

    assert watcher.outcomes, watcher.failures
    assert seen[0].parent == system


def test_job_keeps_temp_files_on_request(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    seen: list[Path] = []

    runner.responses[-2].side_effect = _wrap(
        lambda argv: seen.append(Path(argv[1]).parent)
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path, keep_temp_on_success=True))

    assert watcher.outcomes, watcher.failures
    assert (seen[0] / "tsmuxer-meta.txt").exists()
    assert watcher.outcomes[0].temp_dir_kept
    assert "temp files kept" in watcher.text


# --------------------------------------------------------------------------
# failure and cancellation
# --------------------------------------------------------------------------


def test_job_failure_keeps_the_temp_dir_as_evidence(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    runner.responses[-2] = FakeResponse(
        stderr="Something went wrong", returncode=1, match="tsmuxer-meta.txt"
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path))

    failure = watcher.failures[0]
    assert failure.title == "tsMuxeR failed"
    assert failure.temp_dir is not None and failure.temp_dir.exists()
    assert (failure.temp_dir / "tsmuxer-meta.txt").exists()
    assert "temp files kept in" in watcher.text
    assert not job.busy


def test_job_failure_drops_the_temp_dir_when_asked(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    runner.responses[-2] = FakeResponse(
        stderr="Something went wrong", returncode=1, match="tsmuxer-meta.txt"
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path, keep_temp_on_failure=False))

    assert watcher.failures[0].temp_dir is None
    assert list((tmp_path / "temp").iterdir()) == []


def test_validation_failure_never_starts_tsmuxer(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(_json(load_json("mkv_1080p_hevc.json")), match="ffprobe")
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path))

    failure = watcher.failures[0]
    assert failure.title == "This file cannot be used"
    assert runner.calls_to("tsMuxeR") == []
    assert failure.temp_dir is None  # dropped: no plan was ever built


def test_cancelled_job_reports_the_stage_and_what_it_left_behind(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    job = make_job(runner)
    watcher = JobWatcher(job)
    # unrelated files in the chosen folder must not count as leftovers
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out" / "unrelated.txt").write_text("x", encoding="utf-8")

    def cancel_with_partial_output(argv: list[str]) -> None:
        output_dir = Path(argv[-1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "partial.tmp").write_text("x", encoding="utf-8")
        job.cancel()

    runner.responses[-2] = FakeResponse(
        stderr="killed",
        returncode=-9,
        match="tsmuxer-meta.txt",
        side_effect=cancel_with_partial_output,
    )

    job.start_job(request_for(tmp_path, keep_temp_on_failure=False))

    assert watcher.events[-1] == "cancelled"
    assert watcher.failures == [] and watcher.outcomes == []
    cancellation = watcher.cancellations[0]
    assert cancellation.stage == "muxing"
    assert [path.name for path in cancellation.leftovers] == ["partial.tmp"]
    # the leftover scan looks under the AVCHD folder the mux wrote into, not
    # at the whole chosen folder
    assert cancellation.leftovers[0].parent == tmp_path / "out" / "AVCHD"
    # a cancellation keeps the temp dir even though failures would not be kept
    assert cancellation.temp_dir is not None and cancellation.temp_dir.exists()
    assert (cancellation.temp_dir / "tsmuxer-meta.txt").exists()
    assert not job.busy


def test_unexpected_bug_is_marked_internal(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)

    def boom(_argv: list[str]) -> None:
        raise ValueError("boom")

    runner.responses[-2] = FakeResponse(
        stdout=MUX_OK, match="tsmuxer-meta.txt", side_effect=boom
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path))

    failure = watcher.failures[0]
    assert failure.internal
    assert failure.title == "Internal error"
    assert failure.detail == "boom"
    # the evidence survives an unexpected bug, too
    assert failure.temp_dir is not None and failure.temp_dir.exists()


def test_starting_a_job_while_one_runs_is_refused(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    job = make_job(runner)
    watcher = JobWatcher(job)
    errors: list[str] = []
    busy_during_run: list[bool] = []

    def reenter(_argv: list[str]) -> None:
        busy_during_run.append(job.busy)
        try:
            job.start_probe(tmp_path / "other.mkv")
        except RuntimeError as exc:
            errors.append(str(exc))

    runner.responses[-2].side_effect = _wrap(reenter)

    job.start_job(request_for(tmp_path))

    assert busy_during_run == [True]
    assert errors == ["a job is already running"]
    assert watcher.outcomes and not job.busy


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def test_build_job_plan_uses_the_detection_run(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")

    plan = build_job_plan(request_for(tmp_path), runner, temp_dir=tmp_path / "tmp")

    assert [track.track_id for track in plan.tracks] == [1, 2]
    assert plan.mux.options.split_size_mb == 4096
    assert [Path(call[0]).name for call in runner.calls] == ["ffprobe", "tsMuxeR"]


def test_build_job_plan_raises_before_the_detection_run(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(_json(load_json("mkv_1080p_hevc.json")), match="ffprobe")

    with pytest.raises(ValidationFailed):
        build_job_plan(request_for(tmp_path), runner, temp_dir=tmp_path / "tmp")

    assert [Path(call[0]).name for call in runner.calls] == ["ffprobe"]


def test_build_job_plan_refuses_an_existing_avchd_tree(tmp_path: Path) -> None:
    runner = FakeRunner()
    output = tmp_path / "out"
    (output / "BDMV").mkdir(parents=True)

    with pytest.raises(ExistingTreeError) as excinfo:
        build_job_plan(
            request_for(tmp_path, output_dir=output), runner, temp_dir=tmp_path / "tmp"
        )

    assert runner.calls == []  # refused before any tool runs
    assert excinfo.value.marker == Path("BDMV")
    assert "already contains an AVCHD structure" in str(excinfo.value)


def test_build_job_plan_restores_a_window_cropped_source(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(_json(load_json("mkv_window_crop_h264_ac3_srt.json")), match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")
    capabilities = FfmpegCapabilities(
        bitstream_filters=frozenset({"h264_metadata", "dump_extra"})
    )

    plan = build_job_plan(
        request_for(tmp_path, options=PlanOptions(capabilities=capabilities)),
        runner,
        temp_dir=tmp_path / "tmp",
    )

    assert plan.report.ok
    assert "V4" in {issue.code for issue in plan.report.warnings}
    assert [track.kind for track in plan.tracks] == ["video", "audio"]
    video = plan.video
    assert video is not None
    assert video.prepared is True
    assert video.track_id == 0  # raw elementary stream, like an extracted core
    assert video.source.name.endswith(".264")
    assert plan.prep_steps[0].kind == "video-window"
    assert "1920x1080" in plan.prep_steps[0].label


def test_build_job_plan_refuses_a_window_crop_without_the_bitstream_filter(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    runner.add_stdout(_json(load_json("mkv_window_crop_h264_ac3_srt.json")), match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")

    with pytest.raises(PlanError, match="h264_metadata"):
        build_job_plan(request_for(tmp_path), runner, temp_dir=tmp_path / "tmp")


def test_build_job_plan_still_refuses_a_hard_cropped_source(tmp_path: Path) -> None:
    runner = FakeRunner()
    hard_crop = sps_dump("avcc_hard_crop_1036.txt")
    runner.add_stdout(
        _json(output_payload(vstream(0, height=1036, extradata=hard_crop), astream(1))),
        match="ffprobe",
    )

    with pytest.raises(ValidationFailed):
        build_job_plan(request_for(tmp_path), runner, temp_dir=tmp_path / "tmp")


def test_build_job_plan_uses_defaults_when_no_selection_is_given(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")

    plan = build_job_plan(
        request_for(tmp_path, selection=None), runner, temp_dir=tmp_path / "tmp"
    )

    assert [track.kind for track in plan.tracks] == ["video", "audio"]
    assert plan.audio_tracks()[0].index == 1


def test_describe_media_lists_every_stream_including_none() -> None:
    media = parse_ffprobe_json({"streams": [], "format": {"duration": "1.0"}}, "empty.mkv")

    lines = describe_media(media)

    assert lines[0].startswith("file     empty.mkv")
    assert sum("(none)" in line for line in lines) == 3


def test_describe_outcome_mentions_files_and_split_size(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_success(runner)
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_job(request_for(tmp_path))

    lines = describe_outcome(watcher.outcomes[0])

    assert lines[0] == "done: 1 stream part written"
    assert any("00000.MTS" in line for line in lines)
    assert any("split at 4096 MB" in line for line in lines)


# --------------------------------------------------------------------------
# season jobs: several episodes, one AVCHD folder
# --------------------------------------------------------------------------


def season_request_for(tmp_path: Path, count: int = 2, **overrides: Any) -> SeasonRequest:
    sources = []
    for number in range(1, count + 1):
        source = tmp_path / f"episode{number}.mkv"
        source.write_bytes(b"\x00")
        sources.append(source)
    values: dict[str, Any] = {
        "sources": tuple(sources),
        "output_dir": tmp_path / "out",
        "selections": (TrackSelection(audio=(1,)),) * count,
        "temp_dir": tmp_path / "temp",
    }
    values.update(overrides)
    return SeasonRequest(**values)


def script_season_success(
    runner: FakeRunner,
    *,
    count: int = 2,
    streams: Sequence[list[dict[str, Any]]] | None = None,
) -> None:
    """Script a whole season: per episode a plan probe, a detection run, a mux, a probe.

    The probes are matched on the episode's file name and the output probes on
    ``.m2ts`` (the staged trees keep tsMuxeR's lowercase names until the merge),
    so both episodes' four calls each land on the right scripted answer.
    ``streams`` overrides what each output probe reports, one list per episode;
    by default it is the layout an audio-only plan asks for.
    """
    for number in range(1, count + 1):
        runner.add_stdout(PROBE_JSON_TEXT, match=f"episode{number}.mkv")
        runner.add_stdout(DETECTION, match=f"episode{number}.mkv")
        runner.add(
            FakeResponse(
                stdout=MUX_OK,
                match="tsmuxer-meta.txt",
                side_effect=lambda argv: fake_tree(Path(argv[-1])),
            )
        )
        if streams is not None:
            probe_streams = streams[number - 1]
        else:
            probe_streams = [vstream(0), astream(1)]
        runner.add_stdout(_json(output_payload(*probe_streams)), match=".m2ts")


def test_season_job_muxes_the_episodes_into_one_tree(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_season_success(runner)
    job = make_job(runner)
    watcher = JobWatcher(job)
    output_dir = tmp_path / "out"

    job.start_season(season_request_for(tmp_path))

    assert watcher.events[-1] == "finished"
    assert watcher.failures == [] and watcher.cancellations == []
    outcome = watcher.outcomes[0]
    assert isinstance(outcome, SeasonOutcome)
    assert outcome.title_count == 2
    assert outcome.playlists == (0, 1)
    assert outcome.root == output_dir / "AVCHD"
    # the shipped tree is the AVCHD-on-FAT32 flavour, one part per episode
    assert [path.name for path in outcome.stream_files] == ["00000.MTS", "00001.MTS"]
    bdmv = output_dir / "AVCHD" / "BDMV"
    assert (bdmv / "INDEX.BDM").is_file()
    assert (bdmv / "BACKUP/PLAYLIST/00001.MPL").is_file()
    # every episode is planned before the first mux; each is muxed, verified and
    # merged before the next one starts, and the merge closes the run
    assert watcher.stage_keys == [
        "plan",
        "plan",
        "mux",
        "verify",
        "merge",
        "mux",
        "verify",
        "merge",
        "verify",
        "cleanup",
    ]
    assert [stage.label for stage in watcher.stages[0:2]] == [
        f"episode 1/2: {PLAN_STAGE_LABEL}",
        f"episode 2/2: {PLAN_STAGE_LABEL}",
    ]
    muxes = [stage.label for stage in watcher.stages if stage.key == "mux"]
    assert muxes == ["episode 1/2: muxing episode1.mkv", "episode 2/2: muxing episode2.mkv"]
    # the log pane summarises the season
    assert "season of 2 episodes" in watcher.text
    assert "title 1  playlist 00000  episode1.mkv" in watcher.text
    assert "title 2  playlist 00001  episode2.mkv" in watcher.text
    # progress runs from 0 to 1 across episodes, with busy phases for the probes
    assert watcher.progress[0] == 0.0
    assert watcher.progress[-1] == 1.0
    assert None in watcher.progress
    # the staging root existed only for this run
    assert list((tmp_path / "temp").iterdir()) == []


def test_season_job_uses_the_per_episode_selections(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_season_success(
        runner,
        streams=[
            [vstream(0), astream(1, channels=2, channel_layout="stereo")],
            [vstream(0), astream(1)],
        ],
    )
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_season(
        season_request_for(
            tmp_path,
            selections=(TrackSelection(audio=(2,)), TrackSelection(audio=(1,))),
        )
    )

    assert watcher.events[-1] == "finished"
    outcome = watcher.outcomes[0]
    assert outcome.plans[0].selection.audio == (2,)
    assert outcome.plans[1].selection.audio == (1,)
    assert outcome.plans[0].audio_tracks()[0].index == 2
    assert outcome.plans[1].audio_tracks()[0].index == 1


def test_season_job_keeps_the_staging_trees_on_failure(tmp_path: Path) -> None:
    """Episode 1 muxed fine; episode 2 lost a track: the failed tree stays as evidence."""
    runner = FakeRunner()
    script_season_success(runner, count=1)
    runner.add_stdout(PROBE_JSON_TEXT, match="episode2.mkv")
    runner.add_stdout(DETECTION, match="episode2.mkv")
    runner.add(
        FakeResponse(
            stdout=MUX_OK,
            match="tsmuxer-meta.txt",
            side_effect=lambda argv: fake_tree(Path(argv[-1])),
        )
    )
    runner.add_stdout(_json(output_payload(vstream(0))), match=".m2ts")  # audio dropped
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_season(season_request_for(tmp_path))

    assert watcher.events[-1] == "failed"
    failure = watcher.failures[0]
    assert failure.title == "The output failed verification"
    assert failure.temp_dir is not None and failure.temp_dir.is_dir()
    # episode 1 was already merged and its scratch dropped, so only the failed
    # episode's tree is kept
    assert not (failure.temp_dir / "episode-001" / "AVCHD").exists()
    assert (failure.temp_dir / "episode-002" / "AVCHD" / "BDMV").is_dir()
    # the destination holds the merged episodes — a partial season (D29)
    assert (tmp_path / "out" / "AVCHD" / "BDMV").is_dir()


def test_season_job_refuses_an_existing_tree_before_any_tool_runs(tmp_path: Path) -> None:
    output = tmp_path / "out"
    (output / "AVCHD" / "BDMV").mkdir(parents=True)
    runner = FakeRunner()
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_season(season_request_for(tmp_path, output_dir=output))

    assert watcher.events[-1] == "failed"
    failure = watcher.failures[0]
    assert failure.title == "The output folder cannot be used"
    # the check runs before the first probe, and an empty temp dir is not evidence
    assert runner.calls == []
    assert failure.temp_dir is None


def test_season_job_can_be_cancelled_after_the_first_episode(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_season_success(runner)
    job = make_job(runner)
    watcher = JobWatcher(job)

    def mux_and_cancel(argv: list[str]) -> None:
        # The mux itself finishes; the cancel arrives as it does, and the season
        # must notice before episode 2 is planned or merged.
        fake_tree(Path(argv[-1]))
        job.cancel()

    runner.responses[2] = FakeResponse(
        stdout=MUX_OK, match="tsmuxer-meta.txt", side_effect=mux_and_cancel
    )

    job.start_season(season_request_for(tmp_path))

    assert watcher.events[-1] == "cancelled"
    assert watcher.failures == [] and watcher.outcomes == []
    cancellation = watcher.cancellations[0]
    assert cancellation.stage == "episode 1/2: verifying"
    # a cancelled season keeps its staging trees whatever the failure policy says
    assert cancellation.temp_dir is not None
    assert (cancellation.temp_dir / "episode-001" / "AVCHD" / "BDMV").is_dir()
    assert not (tmp_path / "out" / "AVCHD" / "BDMV").exists()


def test_describe_season_outcome_lists_titles_and_parts(tmp_path: Path) -> None:
    runner = FakeRunner()
    script_season_success(runner)
    job = make_job(runner)
    watcher = JobWatcher(job)

    job.start_season(season_request_for(tmp_path))

    lines = describe_season_outcome(watcher.outcomes[0])

    assert lines[0] == "done: 2 titles (2 stream parts) written"
    assert "title 1  playlist 00000  episode1.mkv" in lines[1]
    assert "title 2  playlist 00001  episode2.mkv" in lines[2]
    assert any("00000.MTS" in line for line in lines)
    assert any("split at 4096 MB" in line for line in lines)


# --------------------------------------------------------------------------
# small internal helpers
# --------------------------------------------------------------------------


def _wrap(action: Any) -> Any:
    """Turn a side effect that only inspects the argv into one that still writes
    the fake AVCHD tree (the mux response needs both)."""

    def side_effect(argv: list[str]) -> None:
        action(argv)
        fake_tree(Path(argv[-1]))

    return side_effect
