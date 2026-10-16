"""Unit tests for :mod:`avchdforge.backend` (plan execution and verification).

Everything runs on a ``FakeRunner`` and hand-made directories: the point of these
tests is that the module refuses to trust tsMuxeR. The interesting cases are the
ones where the tool exits 0 and the output is still wrong — a dropped track, a
phantom 0-channel AC-3 stream, a missing success sentinel — because that is what
``docs/CONTEXT.md`` §2.2 documents about tsMuxeR 2.7.0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from avchdforge.backend import (
    FAT32_MAX_BYTES,
    SPLIT_MB_BYTES,
    SPLIT_OVERSHOOT_ALLOWANCE,
    JobOutcome,
    JobStage,
    check_output_tree,
    check_stream_composition,
    execute_plan,
    find_streams,
    rename_tree_for_usb,
    run_mux,
    verify_output,
)
from avchdforge.errors import JobCancelled, MuxError, VerificationError
from avchdforge.plan import JobPlan, PlanOptions, build_plan
from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import TrackSelection
from avchdforge.tracks import parse_detect_output

from .conftest import FakeResponse, FakeRunner, astream, build_media, sstream, vstream

#: tsMuxeR's report for the passthrough fixture (video, two AC-3, PGS).
AC3_DETECTION = parse_detect_output(
    "tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer\n"
    "Track ID:    1\n"
    "Stream type: H.264\n"
    "Stream ID:   V_MPEG4/ISO/AVC\n"
    "Stream lang: und\n"
    "\n"
    "Track ID:    2\n"
    "Stream type: AC3\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: eng\n"
    "\n"
    "Track ID:    3\n"
    "Stream type: AC3\n"
    "Stream ID:   A_AC3\n"
    "Stream lang: jpn\n"
    "\n"
    "Track ID:    4\n"
    "Stream type: PGS\n"
    "Stream ID:   S_HDMV/PGS\n"
    "Stream lang: eng\n"
)

#: tsMuxeR's report for the DTS-HD fixture (video, DTS-HD MA, PGS).
DTS_DETECTION = parse_detect_output(
    "tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer\n"
    "Track ID:    1\n"
    "Stream type: H.264\n"
    "Stream ID:   V_MPEG4/ISO/AVC\n"
    "Stream lang: und\n"
    "\n"
    "Track ID:    2\n"
    "Stream type: DTS\n"
    "Stream ID:   A_DTS\n"
    "Stream lang: eng\n"
    "\n"
    "Track ID:    3\n"
    "Stream type: PGS\n"
    "Stream ID:   S_HDMV/PGS\n"
    "Stream lang: eng\n"
)

MUX_OK = "22.2% complete\n100.0% complete\nMux successful complete\nMuxing time: 0 sec\n"


# --------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------


def ac3_media():
    return build_media([vstream(0), astream(1), astream(2), sstream(3)])


def ac3_plan(tmp_path: Path, *, options: PlanOptions | None = None) -> JobPlan:
    return build_plan(
        ac3_media(),
        TrackSelection(audio=(1,), subtitles=(3,)),
        AC3_DETECTION,
        output_dir=tmp_path / "out",
        temp_dir=tmp_path / "tmp",
        options=options,
    )


def dts_hd_plan(tmp_path: Path) -> JobPlan:
    media = build_media(
        [
            vstream(0),
            astream(1, codec="dts", profile="DTS-HD MA", channels=6, bit_rate="1536000"),
            sstream(2, "hdmv_pgs_subtitle"),
        ]
    )
    return build_plan(
        media,
        TrackSelection(audio=(1,), subtitles=(2,)),
        DTS_DETECTION,
        output_dir=tmp_path / "out",
        temp_dir=tmp_path / "tmp",
        options=PlanOptions().with_split_size(None),
    )


def output_payload(*streams: dict[str, Any]) -> dict[str, Any]:
    """ffprobe-shaped JSON for a produced ``.m2ts``."""
    return {
        "streams": list(streams),
        "format": {
            "format_name": "mpegts",
            "duration": "8.000000",
            "size": "2000000",
            "bit_rate": "2000000",
        },
    }


def default_output_streams() -> list[dict[str, Any]]:
    """What the produced file must look like for an AC-3 + PGS plan."""
    return [vstream(0), astream(1), sstream(2, "hdmv_pgs_subtitle")]


def fake_tree(
    output_dir: Path, *, stream_bytes: bytes = b"\x00" * 4096, stream_size: int | None = None
) -> Path:
    """Write the AVCHD layout tsMuxeR is supposed to produce.

    ``stream_size`` creates a sparse file of that size, so split-limit cases do not
    need megabytes of real data on disk.
    """
    bdmv = output_dir / "BDMV"
    for name in ("PLAYLIST", "CLIPINF", "STREAM"):
        (bdmv / name).mkdir(parents=True, exist_ok=True)
    (bdmv / "index.bdmv").write_bytes(b"IDX0200")
    (bdmv / "MovieObject.bdmv").write_bytes(b"MOBJ0200")
    (bdmv / "PLAYLIST" / "00000.mpls").write_bytes(b"MPLS0200")
    (bdmv / "CLIPINF" / "00000.clpi").write_bytes(b"HDMV0200")
    stream = bdmv / "STREAM" / "00000.m2ts"
    if stream_size is None:
        stream.write_bytes(stream_bytes)
    else:
        with stream.open("wb") as handle:
            handle.truncate(stream_size)
    return stream


def mux_json_runner(runner: FakeRunner, streams: list[dict[str, Any]] | None = None) -> None:
    """Script a run: tsMuxeR success followed by the probe of the produced file.

    ``execute_plan`` renames the tree before verifying, so the output probe is an
    ffprobe of a ``.MTS`` (the AVCHD-on-FAT32 name).
    """
    runner.add(FakeResponse(stdout=MUX_OK, match="tsMuxeR"))
    runner.add_json(output_payload(*(streams or default_output_streams())), match=".MTS")


# --------------------------------------------------------------------------
# check_output_tree
# --------------------------------------------------------------------------


def test_tree_accepts_the_avchd_layout(tmp_path: Path) -> None:
    fake_tree(tmp_path)

    report = check_output_tree(tmp_path)

    assert report.ok
    assert not report.warnings
    assert any("PLAYLIST" in detail and "00000.mpls" in detail for detail in report.details)


def test_tree_accepts_the_usb_naming(tmp_path: Path) -> None:
    """The renamed tree (what avchd-forge ships) passes the same checks."""
    fake_tree(tmp_path)
    rename_tree_for_usb(tmp_path)

    report = check_output_tree(tmp_path)

    assert report.ok
    assert not report.warnings
    assert any("PLAYLIST" in detail and "00000.MPL" in detail for detail in report.details)


def test_find_streams_sees_both_flavours(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    assert [path.name for path in find_streams(tmp_path)] == ["00000.m2ts"]

    rename_tree_for_usb(tmp_path)

    assert [path.name for path in find_streams(tmp_path)] == ["00000.MTS"]


def test_tree_rejects_a_missing_bdmv(tmp_path: Path) -> None:
    report = check_output_tree(tmp_path)

    assert not report.ok
    assert "did not write an AVCHD structure" in report.problems[0]


def test_tree_lists_missing_files_and_directories(tmp_path: Path) -> None:
    (tmp_path / "BDMV").mkdir()
    (tmp_path / "BDMV" / "index.bdmv").write_bytes(b"x")

    report = check_output_tree(tmp_path)

    joined = "\n".join(report.problems)
    assert "no MovieObject.bdmv or MOVIEOBJ.BDM file" in joined
    assert "contains no *.mpls or *.MPL file" in joined
    assert "contains no *.clpi or *.CPI file" in joined
    assert "contains no *.m2ts or *.MTS file" in joined


def test_tree_rejects_empty_files(tmp_path: Path) -> None:
    stream = fake_tree(tmp_path)
    (tmp_path / "BDMV" / "index.bdmv").write_bytes(b"")
    stream.write_bytes(b"")

    report = check_output_tree(tmp_path)

    assert any("index.bdmv is empty" in problem for problem in report.problems)
    assert any("00000.m2ts is empty" in problem for problem in report.problems)


def test_tree_warns_about_stray_entries(tmp_path: Path) -> None:
    fake_tree(tmp_path)
    (tmp_path / "notes.txt").write_text("hello")

    report = check_output_tree(tmp_path)

    assert report.ok
    warnings = "\n".join(report.warnings)
    assert "unexpected entries" in warnings
    assert "notes.txt" in warnings


def test_tree_rejects_a_chunk_that_ignores_the_split(tmp_path: Path) -> None:
    target = 1 * SPLIT_MB_BYTES
    fake_tree(tmp_path, stream_size=target + SPLIT_OVERSHOOT_ALLOWANCE + 1)

    report = check_output_tree(tmp_path, split_size_mb=1)

    assert not report.ok
    assert "did not split this output" in report.problems[0]


def test_tree_allows_the_keyframe_overshoot(tmp_path: Path) -> None:
    """tsMuxeR only splits at a keyframe, so chunks overshoot their target."""
    target = 1 * SPLIT_MB_BYTES
    fake_tree(tmp_path, stream_size=target + SPLIT_OVERSHOOT_ALLOWANCE)

    report = check_output_tree(tmp_path, split_size_mb=1)

    assert report.ok
    assert any("split target" in detail for detail in report.details)


def test_default_split_target_leaves_fat32_margin() -> None:
    """``4096MB`` is decimal in tsMuxeR; the result must fit FAT32 with room."""
    assert 4096 * SPLIT_MB_BYTES + SPLIT_OVERSHOOT_ALLOWANCE < FAT32_MAX_BYTES


# --------------------------------------------------------------------------
# rename_tree_for_usb
# --------------------------------------------------------------------------


def test_rename_tree_for_usb_maps_every_file(tmp_path: Path) -> None:
    """Every tsMuxeR name becomes the 8.3 name a PS3 needs on FAT32, BACKUP too."""
    bdmv = tmp_path / "BDMV"
    for folder in ("PLAYLIST", "CLIPINF", "STREAM", "BACKUP/PLAYLIST", "BACKUP/CLIPINF"):
        (bdmv / folder).mkdir(parents=True, exist_ok=True)
    files = {
        "index.bdmv": b"IDX",
        "MovieObject.bdmv": b"MOBJ",
        "PLAYLIST/00000.mpls": b"MPLS",
        "CLIPINF/00000.clpi": b"CPI",
        "STREAM/00000.m2ts": b"M2TS",
        "BACKUP/index.bdmv": b"IDX",
        "BACKUP/MovieObject.bdmv": b"MOBJ",
        "BACKUP/PLAYLIST/00000.mpls": b"MPLS",
        "BACKUP/CLIPINF/00000.clpi": b"CPI",
    }
    for name, content in files.items():
        (bdmv / name).write_bytes(content)

    renamed = rename_tree_for_usb(tmp_path)

    expected = {
        "INDEX.BDM": b"IDX",
        "MOVIEOBJ.BDM": b"MOBJ",
        "PLAYLIST/00000.MPL": b"MPLS",
        "CLIPINF/00000.CPI": b"CPI",
        "STREAM/00000.MTS": b"M2TS",
        "BACKUP/INDEX.BDM": b"IDX",
        "BACKUP/MOVIEOBJ.BDM": b"MOBJ",
        "BACKUP/PLAYLIST/00000.MPL": b"MPLS",
        "BACKUP/CLIPINF/00000.CPI": b"CPI",
    }
    assert len(renamed) == len(expected)
    for name, content in expected.items():
        assert (bdmv / name).read_bytes() == content, name
    for name in files:
        assert not (bdmv / name).exists(), name
    # a second pass has nothing left to rename
    assert rename_tree_for_usb(tmp_path) == ()


def test_rename_tree_for_usb_ignores_a_missing_tree(tmp_path: Path) -> None:
    assert rename_tree_for_usb(tmp_path) == ()


def test_rename_tree_for_usb_refuses_to_clobber(tmp_path: Path) -> None:
    bdmv = tmp_path / "BDMV"
    bdmv.mkdir()
    source = bdmv / "index.bdmv"
    target = bdmv / "INDEX.BDM"
    source.write_bytes(b"ours")
    target.write_bytes(b"not ours")
    if source.samefile(target):
        pytest.skip("case-insensitive filesystem: the collision cannot exist")

    with pytest.raises(OSError, match="refusing to overwrite"):
        rename_tree_for_usb(tmp_path)

    assert source.read_bytes() == b"ours"
    assert target.read_bytes() == b"not ours"


# --------------------------------------------------------------------------
# check_stream_composition
# --------------------------------------------------------------------------


def test_composition_accepts_the_planned_streams(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    output = parse_ffprobe_json(output_payload(*default_output_streams()), "00000.m2ts")

    report = check_stream_composition(plan, output)

    assert report.ok
    assert any("stream layout confirmed" in detail for detail in report.details)


def test_composition_rejects_a_dropped_audio_track(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    output = parse_ffprobe_json(output_payload(vstream(0), sstream(1, "hdmv_pgs_subtitle")))

    report = check_stream_composition(plan, output)

    assert not report.ok
    assert "audio streams: expected 1 (ac3 6ch), found 0" in report.problems[0]


def test_composition_rejects_the_phantom_zero_channel_track(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    phantom = astream(1, channels=0, channel_layout=None)
    output = parse_ffprobe_json(output_payload(vstream(0), phantom, sstream(2)))

    report = check_stream_composition(plan, output)

    assert any("0 channels" in problem for problem in report.problems)


def test_composition_rejects_a_wrong_codec_family(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    output = parse_ffprobe_json(
        output_payload(vstream(0), astream(1, codec="dts"), sstream(2))
    )

    report = check_stream_composition(plan, output)

    assert any("expected ac3" in problem for problem in report.problems)


def test_composition_rejects_a_wrong_channel_count(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    output = parse_ffprobe_json(output_payload(vstream(0), astream(1, channels=2), sstream(2)))

    report = check_stream_composition(plan, output)

    assert any("has 2 channels, expected 6" in problem for problem in report.problems)


def test_composition_rejects_a_resolution_change(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    output = parse_ffprobe_json(
        output_payload(vstream(0, width=1280, height=720), astream(1), sstream(2))
    )

    report = check_stream_composition(plan, output)

    assert any("resolution is 1280x720" in problem for problem in report.problems)


def test_composition_rejects_a_subtitle_that_stayed_text(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    output = parse_ffprobe_json(
        output_payload(vstream(0), astream(1), sstream(2, codec="subrip"))
    )

    report = check_stream_composition(plan, output)

    assert any("expected pgs" in problem for problem in report.problems)


def test_composition_tolerates_core_channel_reduction(tmp_path: Path) -> None:
    """A DD+/Atmos core may legitimately have fewer channels than the source."""
    plan = dts_hd_plan(tmp_path)
    # 5.1 DTS-HD MA → 2.0 DTS core: family matches, channel count drops.
    output = parse_ffprobe_json(
        output_payload(
            vstream(0),
            astream(1, codec="dts", profile="DTS", channels=2),
            sstream(2, "hdmv_pgs_subtitle"),
        )
    )

    report = check_stream_composition(plan, output)

    assert report.ok


# --------------------------------------------------------------------------
# execute_plan — happy paths
# --------------------------------------------------------------------------


def test_execute_plan_passthrough(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    fake_tree(plan.output_dir)
    mux_json_runner(runner)
    stages: list[JobStage] = []
    progress: list[float | None] = []

    outcome = execute_plan(plan, runner, on_stage=stages.append, on_progress=progress.append)

    assert isinstance(outcome, JobOutcome)
    assert outcome.verified_file == plan.output_dir / "BDMV" / "STREAM" / "00000.MTS"
    assert outcome.stream_files == (outcome.verified_file,)
    # tsMuxeR's optical names are gone: the shipped tree is the PS3 USB flavour
    assert (plan.output_dir / "BDMV" / "INDEX.BDM").is_file()
    assert not (plan.output_dir / "BDMV" / "index.bdmv").exists()
    assert not outcome.warnings
    assert [stage.key for stage in stages] == ["mux", "verify", "cleanup"]
    assert runner.calls[0] == ["tsMuxeR", str(plan.meta_path), str(plan.output_dir)]
    assert runner.phases == ["mux", "probe"]
    values = [value for value in progress if value is not None]
    assert values == sorted(values)
    assert values[0] == 0.0
    assert values[-1] == 1.0
    assert None in progress  # the probe phase shows a busy bar


def test_execute_plan_writes_the_meta_file(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    fake_tree(plan.output_dir)
    mux_json_runner(runner)

    outcome = execute_plan(plan, runner, keep_temp_on_success=True)

    assert outcome.temp_dir_kept
    assert plan.meta_path.read_text(encoding="utf-8") == plan.meta_text


def test_run_mux_creates_the_avchd_folder_it_writes_into(tmp_path: Path, runner) -> None:
    """The user may have pointed at a drive root: ``<root>/AVCHD`` need not exist."""
    plan = ac3_plan(tmp_path)
    plan.temp_dir.mkdir(parents=True)
    assert plan.output_dir.name == "AVCHD"
    assert not plan.output_dir.exists()
    runner.add(FakeResponse(stdout=MUX_OK, match="tsMuxeR"))

    run_mux(plan, runner)

    assert plan.output_dir.is_dir()


def test_execute_plan_removes_its_own_temp_dir(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    fake_tree(plan.output_dir)
    mux_json_runner(runner)

    execute_plan(plan, runner)

    assert not plan.temp_dir.exists()


def test_execute_plan_leaves_a_shared_temp_dir_alone(tmp_path: Path, runner) -> None:
    """A pre-existing temp directory is the user's; only the job's files go away."""
    plan = ac3_plan(tmp_path)
    plan.temp_dir.mkdir(parents=True)
    (plan.temp_dir / "keep-me.txt").write_text("not ours")
    fake_tree(plan.output_dir)
    mux_json_runner(runner)

    execute_plan(plan, runner)

    assert plan.temp_dir.is_dir()
    assert (plan.temp_dir / "keep-me.txt").exists()
    assert not plan.meta_path.exists()


def test_execute_plan_runs_prep_steps_before_the_mux(tmp_path: Path, runner) -> None:
    plan = dts_hd_plan(tmp_path)
    fake_tree(plan.output_dir)
    step = plan.prep_steps[0]
    assert "dca_core" in step.argv
    runner.add(FakeResponse(match="dca_core"))
    runner.add_json(output_payload(astream(0, codec="dts")), match="audio-1.dts")
    mux_json_runner(
        runner,
        [
            vstream(0),
            astream(1, codec="dts", profile="DTS", channels=6),
            sstream(2, "hdmv_pgs_subtitle"),
        ],
    )
    stages: list[JobStage] = []

    outcome = execute_plan(
        plan, runner, on_stage=stages.append, keep_temp_on_success=True
    )

    # no cleanup stage: keep_temp_on_success asks for the temp dir to stay
    assert [stage.key for stage in stages] == ["prepare", "mux", "verify"]
    assert stages[0].label == step.label
    assert runner.calls[0] == list(step.argv)
    assert runner.calls[1][-1] == str(step.output)
    assert runner.calls[2][0] == "tsMuxeR"
    # the meta file consumes the extracted core as a raw elementary stream
    meta = plan.meta_path.read_text(encoding="utf-8")
    assert f"A_DTS, {step.output}, track=0" in meta
    assert outcome.verified_file is not None


def test_verify_output_runs_ffprobe_on_the_first_part(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path, options=PlanOptions().with_split_size(4096))
    fake_tree(plan.output_dir)
    second = plan.output_dir / "BDMV" / "STREAM" / "00001.m2ts"
    second.write_bytes(b"\x00" * 64)
    runner.add_json(output_payload(*default_output_streams()), match="00000.m2ts")

    report = verify_output(plan, runner)

    assert report.ok
    assert report.verified_file == plan.output_dir / "BDMV" / "STREAM" / "00000.m2ts"
    assert runner.last_call[-1] == str(report.verified_file)


# --------------------------------------------------------------------------
# execute_plan — the failures tsMuxeR hides
# --------------------------------------------------------------------------


def test_execute_plan_rejects_a_missing_success_sentinel(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    fake_tree(plan.output_dir)
    runner.add(FakeResponse(stdout="100.0% complete\nMuxing time: 0 sec\n", match="tsMuxeR"))
    runner.add_json(output_payload(*default_output_streams()), match=".MTS")

    with pytest.raises(VerificationError) as excinfo:
        execute_plan(plan, runner)

    assert any("Mux successful complete" in problem for problem in excinfo.value.problems)


def test_execute_plan_rejects_a_dropped_track(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    fake_tree(plan.output_dir)
    runner.add(FakeResponse(stdout=MUX_OK, match="tsMuxeR"))
    runner.add_json(output_payload(vstream(0), sstream(1, "hdmv_pgs_subtitle")), match=".MTS")

    with pytest.raises(VerificationError) as excinfo:
        execute_plan(plan, runner)

    assert any("audio streams: expected 1" in problem for problem in excinfo.value.problems)
    # evidence survives the failure
    assert plan.meta_path.exists()


def test_execute_plan_reports_an_unreadable_output(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    fake_tree(plan.output_dir)
    runner.add(FakeResponse(stdout=MUX_OK, match="tsMuxeR"))
    runner.add(FakeResponse(stderr="Invalid data found", returncode=1, match=".MTS"))

    with pytest.raises(VerificationError) as excinfo:
        execute_plan(plan, runner)

    assert any("could not inspect 00000.MTS" in problem for problem in excinfo.value.problems)


def test_execute_plan_rejects_an_unsplit_output(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path, options=PlanOptions().with_split_size(1))
    fake_tree(plan.output_dir, stream_size=1 * SPLIT_MB_BYTES + SPLIT_OVERSHOOT_ALLOWANCE + 1)
    mux_json_runner(runner)

    with pytest.raises(VerificationError) as excinfo:
        execute_plan(plan, runner)

    assert any("did not split" in problem for problem in excinfo.value.problems)


def test_execute_plan_fails_when_a_prep_output_is_wrong(tmp_path: Path, runner) -> None:
    plan = dts_hd_plan(tmp_path)
    fake_tree(plan.output_dir)
    runner.add(FakeResponse(match="dca_core"))
    # ffmpeg claimed success but wrote something that is not a DTS core
    runner.add_json(output_payload(astream(0, codec="eac3", channels=6)), match="dtscore")

    with pytest.raises(VerificationError) as excinfo:
        execute_plan(plan, runner)

    joined = "\n".join(excinfo.value.problems)
    assert "still EAC3" in joined
    assert "DTS core" in joined
    assert len(runner.calls) == 2  # the mux never ran


def test_execute_plan_raises_mux_error_on_nonzero_exit(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    runner.add(FakeResponse(stderr="Something went wrong", returncode=1, match="tsMuxeR"))

    with pytest.raises(MuxError) as excinfo:
        execute_plan(plan, runner)

    assert excinfo.value.returncode == 1
    assert "Something went wrong" in str(excinfo.value)
    assert plan.meta_path.exists()


def test_execute_plan_reports_cancellation(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    runner = FakeRunner()
    runner.add(
        FakeResponse(
            stderr="killed",
            returncode=-9,
            match="tsMuxeR",
            side_effect=lambda _argv: runner.cancel(),
        )
    )

    with pytest.raises(JobCancelled):
        execute_plan(plan, runner, keep_temp_on_failure=False)

    # a cancelled job keeps its temp dir, but it is not an error — even when the
    # caller asked for failures not to be kept
    assert plan.meta_path.exists()


def test_execute_plan_drops_temp_on_failure_when_asked(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    runner.add(FakeResponse(stderr="nope", returncode=2, match="tsMuxeR"))

    with pytest.raises(MuxError):
        execute_plan(plan, runner, keep_temp_on_failure=False)

    assert not plan.temp_dir.exists()


def test_execute_plan_progress_covers_prep_and_mux(tmp_path: Path, runner) -> None:
    plan = dts_hd_plan(tmp_path)
    fake_tree(plan.output_dir)
    runner.add(
        FakeResponse(
            match="dca_core",
            stdout="out_time_us=2000000\nprogress=continue\nout_time_us=8000000\nprogress=end\n",
        )
    )
    runner.add_json(output_payload(astream(0, codec="dts")), match="dtscore")
    mux_json_runner(
        runner,
        [
            vstream(0),
            astream(1, codec="dts", profile="DTS", channels=6),
            sstream(2, "hdmv_pgs_subtitle"),
        ],
    )
    progress: list[float | None] = []

    execute_plan(plan, runner, on_progress=progress.append)

    values = [value for value in progress if value is not None]
    assert values == sorted(values)
    assert values[0] == 0.0
    assert values[-1] == 1.0
    # the ffmpeg block at 8s of an 8s source filled its third of the bar
    assert any(abs(value - 1 / 3) < 1e-6 for value in values)


def test_execute_plan_does_not_touch_foreign_files(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    plan.temp_dir.mkdir(parents=True)
    foreign = plan.temp_dir / "other-job.tmp"
    foreign.write_text("someone else")
    fake_tree(plan.output_dir)
    mux_json_runner(runner)

    execute_plan(plan, runner)

    assert foreign.exists()


def test_execute_plan_keeps_temp_dir_when_check_fails(tmp_path: Path, runner) -> None:
    plan = ac3_plan(tmp_path)
    fake_tree(plan.output_dir)
    runner.add(FakeResponse(stdout=MUX_OK, match="tsMuxeR"))
    runner.add_json(output_payload(vstream(0)), match=".MTS")

    with pytest.raises(VerificationError):
        execute_plan(plan, runner)

    assert plan.temp_dir.is_dir()
