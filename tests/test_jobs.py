"""Unit tests for :mod:`avchdforge.jobs` (Qt job orchestration).

Everything runs on a ``FakeRunner`` injected through ``runner_factory``: these
tests are about signal plumbing, the temp-file policy and cancellation routing,
not about Qt's process layer (``test_qt_process.py`` owns that).

Signals are connected as plain callables and fire synchronously, because
``start_*`` runs on the calling thread exactly as it does in the GUI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6", reason="jobs.py is Qt orchestration")
pytest.importorskip("pytestqt", reason="the job tests use pytest-qt")

from avchdforge.errors import ValidationFailed
from avchdforge.jobs import (
    PLAN_STAGE_LABEL,
    TEMP_PREFIX,
    JobCancellation,
    JobFailure,
    JobRequest,
    JobRunner,
    ProbeOutcome,
    build_job_plan,
    describe_media,
    describe_outcome,
)
from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import TrackSelection

from .conftest import (
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
    """Write the AVCHD layout tsMuxeR is supposed to produce."""
    bdmv = output_dir / "BDMV"
    for name in ("PLAYLIST", "CLIPINF", "STREAM"):
        (bdmv / name).mkdir(parents=True, exist_ok=True)
    (bdmv / "index.bdmv").write_bytes(b"IDX0200")
    (bdmv / "MovieObject.bdmv").write_bytes(b"MOBJ0200")
    (bdmv / "PLAYLIST" / "00000.mpls").write_bytes(b"MPLS0200")
    (bdmv / "CLIPINF" / "00000.clpi").write_bytes(b"HDMV0200")
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
    ``.m2ts``.
    """

    def write_tree(argv: list[str]) -> None:
        fake_tree(Path(argv[-1]))

    runner.add_stdout(PROBE_JSON_TEXT, match="ffprobe")
    runner.add_stdout(DETECTION, match=".mkv")
    runner.add(FakeResponse(stdout=MUX_OK, match="tsmuxer-meta.txt", side_effect=write_tree))
    runner.add_stdout(_json(output_payload(vstream(0), astream(1))), match=".m2ts")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


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
    assert [path.name for path in outcome.stream_files] == ["00000.m2ts"]
    assert outcome.verified_file == output_dir / "BDMV" / "STREAM" / "00000.m2ts"
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
    assert mux[2] == str(output_dir)
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
        match=".m2ts",
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
    assert any("00000.m2ts" in line for line in lines)
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
