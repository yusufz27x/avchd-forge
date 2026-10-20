"""Unit tests for :mod:`avchdforge.backend` (plan execution and verification).

Everything runs on a ``FakeRunner`` and hand-made directories: the point of these
tests is that the module refuses to trust tsMuxeR. The interesting cases are the
ones where the tool exits 0 and the output is still wrong — a dropped track, a
phantom 0-channel AC-3 stream, a missing success sentinel — because that is what
``docs/CONTEXT.md`` §2.2 documents about tsMuxeR 2.7.0.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from avchdforge.backend import (
    FAT32_MAX_BYTES,
    MUX_WEIGHT,
    PREP_STEP_WEIGHT,
    SPLIT_MB_BYTES,
    SPLIT_OVERSHOOT_ALLOWANCE,
    TAIL_SHARE,
    TEMP_HEADROOM,
    JobOutcome,
    JobStage,
    SeasonOutcome,
    check_output_tree,
    check_stream_composition,
    check_temp_space,
    execute_plan,
    execute_season,
    find_streams,
    rename_tree_for_usb,
    run_mux,
    temp_space_needed,
    verify_output,
)
from avchdforge.drives import format_size
from avchdforge.errors import (
    JobCancelled,
    MuxError,
    OutOfSpaceError,
    SeasonMergeError,
    VerificationError,
    looks_like_no_space,
)
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


def window_plan(tmp_path: Path) -> JobPlan:
    """The 1036 source whose video prep step restores it to 1080."""
    from .plan_jobs import WINDOW_CAPABILITIES, WINDOW_DETECTION, window_media

    return build_plan(
        window_media(),
        TrackSelection(audio=(1,), subtitles=(2,)),
        WINDOW_DETECTION,
        output_dir=tmp_path / "out",
        temp_dir=tmp_path / "tmp",
        options=PlanOptions().with_capabilities(WINDOW_CAPABILITIES),
    )


def test_composition_expects_the_restored_resolution(tmp_path: Path) -> None:
    plan = window_plan(tmp_path)
    output = parse_ffprobe_json(
        output_payload(vstream(0), astream(1), sstream(2, "hdmv_pgs_subtitle"))
    )

    report = check_stream_composition(plan, output)

    assert report.ok, report.problems


def test_composition_rejects_a_video_that_was_muxed_still_cropped(tmp_path: Path) -> None:
    plan = window_plan(tmp_path)
    output = parse_ffprobe_json(
        output_payload(
            vstream(0, width=1920, height=1036), astream(1), sstream(2, "hdmv_pgs_subtitle")
        )
    )

    report = check_stream_composition(plan, output)

    assert any("resolution is 1920x1036" in problem for problem in report.problems)
    assert any("expected 1920x1080" in problem for problem in report.problems)


def test_composition_rejects_a_subtitle_that_stayed_text(tmp_path: Path) -> None:
    plan = ac3_plan(tmp_path)
    output = parse_ffprobe_json(
        output_payload(vstream(0), astream(1), sstream(2, codec="subrip"))
    )

    report = check_stream_composition(plan, output)

    assert any("expected pgs" in problem for problem in report.problems)


def test_composition_tolerates_core_channel_reduction(tmp_path: Path) -> None:
    """A DTS-HD/TrueHD core may legitimately have fewer channels than the source."""
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
    # the ffmpeg block at 8s of an 8s source filled its slice of the bar
    slice_end = (1 - TAIL_SHARE) * PREP_STEP_WEIGHT / (PREP_STEP_WEIGHT + MUX_WEIGHT)
    assert any(abs(value - slice_end) < 1e-6 for value in values)


def test_execute_plan_progress_is_nearly_full_when_the_mux_ends(
    tmp_path: Path, runner
) -> None:
    """The tail after the mux is a sliver, so the mux must end near the top.

    Regression test: the verification phase used to hold a whole unit of the
    bar, so with ``n`` prep steps the mux ended at ``(n + 1) / (n + 2)`` — 80%
    with three — and the flush, rename and verification jumped the bar from
    there to 100% only once the whole job was done.
    """
    plan = dts_hd_plan(tmp_path)
    fake_tree(plan.output_dir)
    runner.add(FakeResponse(match="dca_core", stdout="progress=end\n"))
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
    # the run ends with the mux's last value, the busy ``None`` while
    # verification probes the output, then 1.0:
    assert values[-2] == pytest.approx(1 - TAIL_SHARE)
    assert values[-1] == 1.0
    assert None in progress


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


# --------------------------------------------------------------------------
# execute_season — several episodes in one AVCHD folder
# --------------------------------------------------------------------------


def season_episode_tree(output_dir: Path, *, parts: int = 1) -> Path:
    """tsMuxeR's single-title layout, including the BACKUP tree a merge insists on."""
    bdmv = output_dir / "BDMV"
    for name in ("PLAYLIST", "CLIPINF", "STREAM", "BACKUP/PLAYLIST", "BACKUP/CLIPINF"):
        (bdmv / name).mkdir(parents=True, exist_ok=True)
    for base in (bdmv, bdmv / "BACKUP"):
        (base / "index.bdmv").write_bytes(b"IDX0200")
        (base / "MovieObject.bdmv").write_bytes(b"MOBJ0200")
    playlist = b"MPLS0200" + b"\x00" * 0x3C
    playlist += b"".join(b"\x00\x50" + f"{stem:05d}M2TS".encode("ascii") for stem in range(parts))
    for path in (bdmv / "PLAYLIST/00000.mpls", bdmv / "BACKUP/PLAYLIST/00000.mpls"):
        path.write_bytes(playlist)
    for stem in range(parts):
        name = f"{stem:05d}"
        for base in (bdmv, bdmv / "BACKUP"):
            (base / "CLIPINF" / f"{name}.clpi").write_bytes(b"HDMV0200")
        (bdmv / "STREAM" / f"{name}.m2ts").write_bytes(b"\x00" * 4096)
    return bdmv / "STREAM/00000.m2ts"


def season_plan(tmp_path: Path, number: int, *, parts: int = 1) -> JobPlan:
    """One episode staged the way ``jobs.py`` stages a season run."""
    media = build_media(
        [vstream(0), astream(1), sstream(3)], path=f"episode{number}.mkv"
    )
    episode_dir = tmp_path / "season-tmp" / f"episode-{number:02d}"
    return build_plan(
        media,
        TrackSelection(audio=(1,), subtitles=(3,)),
        AC3_DETECTION,
        output_dir=episode_dir,
        temp_dir=episode_dir,
        options=PlanOptions().with_split_size(None),
    )


def script_season(
    runner: FakeRunner,
    *,
    parts: tuple[int, ...] = (1, 1),
    streams: Sequence[list[dict[str, Any]]] | None = None,
) -> None:
    """Script one mux plus one output probe per episode, in episode order.

    ``streams`` (one stream list per episode) overrides what the probes report;
    by default every probe reports the layout the passthrough plan asks for.
    """
    for index, count in enumerate(parts):
        runner.add(
            FakeResponse(
                stdout=MUX_OK,
                match="tsmuxer-meta.txt",
                side_effect=lambda argv, count=count: season_episode_tree(
                    Path(argv[-1]), parts=count
                ),
            )
        )
        probe_streams = streams[index] if streams is not None else default_output_streams()
        runner.add_stdout(json.dumps(output_payload(*probe_streams)), match=".m2ts")


def test_execute_season_muxes_each_episode_and_merges_the_trees(tmp_path: Path) -> None:
    plans = [season_plan(tmp_path, 1), season_plan(tmp_path, 2)]
    staging = tmp_path / "season-tmp"
    destination = tmp_path / "out" / "AVCHD"
    runner = FakeRunner()
    script_season(runner)
    stages: list[JobStage] = []
    progress: list[float | None] = []
    ticks: list[int] = []

    outcome = execute_season(
        plans,
        runner,
        destination=destination,
        on_stage=stages.append,
        on_progress=progress.append,
        pump=lambda: ticks.append(0),
        owns_temp=True,
    )

    assert isinstance(outcome, SeasonOutcome)
    assert outcome.title_count == 2
    assert outcome.playlists == (0, 1)
    assert outcome.root == destination
    assert outcome.temp_dir == staging
    # One title per episode, one clip each, in the FAT32 names a PS3 mounts.
    bdmv = destination / "BDMV"
    assert (bdmv / "INDEX.BDM").is_file()
    assert (bdmv / "PLAYLIST/00001.MPL").is_file()
    assert (bdmv / "BACKUP/PLAYLIST/00001.MPL").is_file()
    assert [path.name for path in outcome.stream_files] == ["00000.MTS", "00001.MTS"]
    # Both episodes were muxed and probed in order, then the staging root went away.
    assert [Path(call[0]).name for call in runner.calls] == [
        "tsMuxeR",
        "ffprobe",
        "tsMuxeR",
        "ffprobe",
    ]
    # Each episode is verified and then merged before the next one is muxed.
    assert [stage.key for stage in stages] == [
        "mux",
        "verify",
        "merge",
        "mux",
        "verify",
        "merge",
        "verify",
        "cleanup",
    ]
    mux_labels = [stage.label for stage in stages if stage.key == "mux"]
    assert mux_labels == [
        "episode 1/2: muxing episode1.mkv",
        "episode 2/2: muxing episode2.mkv",
    ]
    merge_labels = [stage.label for stage in stages if stage.key == "merge"]
    assert merge_labels == [
        "episode 1/2: merging into the season folder",
        "episode 2/2: merging into the season folder",
    ]
    # The transfer hands the caller's loop a turn once per stream file (per
    # block when a real file needs several).
    assert len(ticks) == 2
    assert not staging.exists()
    values = [value for value in progress if value is not None]
    assert values == sorted(values)
    assert values[0] == 0.0
    assert values[-1] == 1.0
    assert None in progress  # the probes and the merge tail show a busy bar


def test_execute_season_merges_multi_part_episodes(tmp_path: Path) -> None:
    """The realistic case: an episode over the split size muxes into several clips."""
    plans = [season_plan(tmp_path, 1, parts=2), season_plan(tmp_path, 2, parts=1)]
    runner = FakeRunner()
    script_season(runner, parts=(2, 1))

    outcome = execute_season(
        plans, runner, destination=tmp_path / "out" / "AVCHD", owns_temp=True
    )

    assert outcome.playlists == (0, 2)
    assert [path.name for path in outcome.stream_files] == [
        "00000.MTS",
        "00001.MTS",
        "00002.MTS",
    ]


def test_execute_season_refuses_an_episode_with_a_dropped_track(tmp_path: Path) -> None:
    plans = [season_plan(tmp_path, 1), season_plan(tmp_path, 2)]
    destination = tmp_path / "out" / "AVCHD"
    runner = FakeRunner()
    script_season(
        runner,
        streams=[default_output_streams(), [vstream(0), astream(1)]],  # no subtitle
    )

    with pytest.raises(VerificationError, match="subtitle streams"):
        execute_season(plans, runner, destination=destination, owns_temp=True)

    # Episode 1 was verified and merged before episode 2 lost its track, so the
    # destination holds episode 1 alone — a partial season, deleted before a
    # retry — while the failed episode's tree stays as evidence (D29).
    bdmv = destination / "BDMV"
    assert (bdmv / "PLAYLIST/00000.mpls").is_file()
    assert not (bdmv / "PLAYLIST/00001.mpls").exists()
    assert not plans[0].output_dir.exists()  # merged, and its scratch dropped
    assert (plans[1].output_dir / "BDMV/index.bdmv").is_file()


def test_execute_season_cancellation_keeps_the_staged_trees(tmp_path: Path) -> None:
    plans = [season_plan(tmp_path, 1), season_plan(tmp_path, 2)]
    runner = FakeRunner()

    def mux_and_cancel(argv: list[str]) -> None:
        season_episode_tree(Path(argv[-1]))
        runner.cancelled = True

    runner.add(FakeResponse(stdout=MUX_OK, match="tsmuxer-meta.txt", side_effect=mux_and_cancel))
    destination = tmp_path / "out" / "AVCHD"

    with pytest.raises(JobCancelled) as excinfo:
        execute_season(plans, runner, destination=destination, owns_temp=True)

    assert excinfo.value.stage == "episode 1/2: verifying"
    assert not (destination / "BDMV").exists()
    assert (plans[0].output_dir / "BDMV/STREAM/00000.m2ts").is_file()


def test_execute_season_can_be_cancelled_while_an_episode_is_merged(tmp_path: Path) -> None:
    plans = [season_plan(tmp_path, 1), season_plan(tmp_path, 2)]
    destination = tmp_path / "out" / "AVCHD"
    runner = FakeRunner()
    script_season(runner)

    def cancel_on_merge(stage: JobStage) -> None:
        # The cancel lands as episode 1's transfer is announced, i.e. while the
        # GUI would be inside the copy loop with the Cancel button live.
        if stage.key == "merge":
            runner.cancelled = True

    with pytest.raises(JobCancelled) as excinfo:
        execute_season(
            plans,
            runner,
            destination=destination,
            on_stage=cancel_on_merge,
            owns_temp=True,
        )

    assert excinfo.value.stage == "copying to the season folder"
    # The transfer stopped before the first stream: nothing half-written in the
    # destination, and the episode's staged tree is whole for a retry.
    assert list((destination / "BDMV/STREAM").iterdir()) == []
    assert (plans[0].output_dir / "BDMV/STREAM/00000.m2ts").is_file()


def test_execute_season_can_keep_the_staged_trees(tmp_path: Path) -> None:
    """Keeping the temp means keeping the muxed trees, so the merge copies."""
    plans = [season_plan(tmp_path, 1), season_plan(tmp_path, 2)]
    runner = FakeRunner()
    script_season(runner)

    outcome = execute_season(
        plans,
        runner,
        destination=tmp_path / "out" / "AVCHD",
        owns_temp=True,
        keep_temp_on_success=True,
    )

    assert outcome.temp_dir_kept
    assert (plans[0].output_dir / "BDMV/STREAM/00000.m2ts").is_file()
    assert (plans[1].output_dir / "BDMV/STREAM/00000.m2ts").is_file()
    assert (outcome.root / "BDMV/INDEX.BDM").is_file()


def test_execute_season_leaves_a_shared_staging_root_alone(tmp_path: Path) -> None:
    """A pre-existing staging root is the caller's; only this run's files go away."""
    plans = [season_plan(tmp_path, 1), season_plan(tmp_path, 2)]
    staging = tmp_path / "season-tmp"
    staging.mkdir(parents=True)
    foreign = staging / "keep-me.txt"
    foreign.write_text("not ours")
    runner = FakeRunner()
    script_season(runner)

    execute_season(
        plans, runner, destination=tmp_path / "out" / "AVCHD", owns_temp=False
    )

    assert foreign.exists()
    assert not plans[0].output_dir.exists()
    assert not plans[0].meta_path.exists()


def test_execute_season_refuses_plans_that_are_not_staged(tmp_path: Path) -> None:
    """A plan whose mux destination is not <scratch>/AVCHD cannot become a title."""
    plan = ac3_plan(tmp_path)  # output <tmp>/out/AVCHD, scratch <tmp>/tmp
    with pytest.raises(SeasonMergeError, match="season muxes into"):
        execute_season([plan], FakeRunner(), destination=tmp_path / "season")


# --------------------------------------------------------------------------
# the temp free-space check and the ENOSPC translation
# --------------------------------------------------------------------------


def test_temp_space_needed_leaves_room_for_the_prepared_files(tmp_path: Path) -> None:
    """A single job writes only its prepared files into temp, not the tree."""
    plan = ac3_plan(tmp_path)  # an AC-3 track is copied: no prep step at all

    assert temp_space_needed(plan) == TEMP_HEADROOM


def test_temp_space_needed_counts_a_source_sized_repair(tmp_path: Path) -> None:
    """A window repair leaves a source-sized elementary stream in temp."""
    plan = window_plan(tmp_path)
    estimate = plan.estimated_size_bytes

    assert estimate is not None
    assert temp_space_needed(plan) == estimate + TEMP_HEADROOM


def test_temp_space_needed_counts_a_season_staged_mux_output(tmp_path: Path) -> None:
    """A season plan muxes into temp, so the tree's size counts as well."""
    plan = season_plan(tmp_path, 1)
    estimate = plan.estimated_size_bytes

    assert estimate is not None
    assert temp_space_needed(plan) == estimate + TEMP_HEADROOM


def test_temp_space_needed_is_none_without_an_estimate(tmp_path: Path) -> None:
    """No duration and no container size means no estimate, so no check."""
    media = build_media([vstream(0), astream(1)], size=None, duration=None)
    plan = build_plan(
        media,
        TrackSelection(audio=(1,)),
        AC3_DETECTION,
        output_dir=tmp_path / "out",
        temp_dir=tmp_path / "tmp",
    )

    assert temp_space_needed(plan) is None


def test_check_temp_space_passes_when_there_is_room(tmp_path: Path, monkeypatch) -> None:
    plan = season_plan(tmp_path, 1)
    monkeypatch.setattr("avchdforge.backend.free_bytes", lambda path: 10**12)

    check_temp_space(plan)  # no exception


def test_check_temp_space_names_the_folder_and_the_numbers(
    tmp_path: Path, monkeypatch
) -> None:
    plan = season_plan(tmp_path, 1)
    monkeypatch.setattr("avchdforge.backend.free_bytes", lambda path: 1_000_000)

    with pytest.raises(OutOfSpaceError) as excinfo:
        check_temp_space(plan)

    message = str(excinfo.value)
    needed = temp_space_needed(plan)
    assert needed is not None
    assert "Not enough free space in" in message
    assert str(plan.temp_dir) in message
    assert format_size(needed) in message and "is needed" in message
    assert "is free" in message
    assert "Preferences" in message and "Temporary files" in message
    assert excinfo.value.path == plan.temp_dir
    assert excinfo.value.needed_bytes == needed


def test_check_temp_space_calls_out_a_memory_backed_folder(
    tmp_path: Path, monkeypatch
) -> None:
    """The system temp default is a tmpfs on many systems; say so."""
    plan = season_plan(tmp_path, 1)
    monkeypatch.setattr("avchdforge.backend.free_bytes", lambda path: 0)
    monkeypatch.setattr("avchdforge.backend.filesystem_type", lambda path: "tmpfs")

    with pytest.raises(OutOfSpaceError) as excinfo:
        check_temp_space(plan)

    assert "memory-backed (tmpfs)" in str(excinfo.value)
    assert "is RAM, not disk" in str(excinfo.value)


def test_execute_season_refuses_to_start_when_temp_is_too_small(
    tmp_path: Path, monkeypatch
) -> None:
    """The check runs before the first tool of the first episode."""
    plans = [season_plan(tmp_path, 1), season_plan(tmp_path, 2)]
    runner = FakeRunner()
    script_season(runner)
    monkeypatch.setattr("avchdforge.backend.free_bytes", lambda path: 1_000_000)

    with pytest.raises(OutOfSpaceError, match="Not enough free space"):
        execute_season(
            plans, runner, destination=tmp_path / "out" / "AVCHD", owns_temp=True
        )

    assert runner.calls == []  # neither ffmpeg, nor tsMuxeR, nor ffprobe ran
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (228, "", True),  # ffmpeg: AVERROR(ENOSPC) is -28; the OS keeps the byte
        (28, "", True),
        (1, "Could not write: No space left on device", True),
        (1, "not enough space to write the stream", True),
        (1, "Disk full", True),
        (1, "invalid stream", False),
        (-9, "killed", False),
        (None, "", False),
    ],
)
def test_looks_like_no_space(returncode: int | None, output: str, expected: bool) -> None:
    assert looks_like_no_space(returncode, output) is expected


def test_a_prep_step_that_runs_out_of_space_names_the_temp_folder(
    tmp_path: Path, runner
) -> None:
    plan = dts_hd_plan(tmp_path)  # the DTS core extraction runs ffmpeg first
    runner.add(
        FakeResponse(
            stderr="av_interleaved_write_frame(): No space left on device",
            returncode=228,
            match="dca_core",
        )
    )

    with pytest.raises(OutOfSpaceError) as excinfo:
        execute_plan(plan, runner)

    message = str(excinfo.value)
    assert "ffmpeg" in message and "228" in message
    assert str(plan.temp_dir) in message
    assert "Temporary files" in message
    assert "No space left on device" in message  # the tail is kept as evidence


def test_a_mux_that_runs_out_of_space_names_the_output_folder(
    tmp_path: Path, runner
) -> None:
    """tsMuxeR only says the words — no exit code a space check could trust."""
    plan = ac3_plan(tmp_path)  # no prep steps, so the mux is the first tool
    runner.add(
        FakeResponse(
            stderr="[error] not enough space to write the output",
            returncode=1,
            match="tsMuxeR",
        )
    )

    with pytest.raises(OutOfSpaceError) as excinfo:
        execute_plan(plan, runner)

    message = str(excinfo.value)
    assert "tsMuxeR" in message
    assert str(plan.output_dir) in message
    assert "another output folder" in message


def test_a_season_mux_that_runs_out_of_space_names_the_temp_folder(
    tmp_path: Path, runner
) -> None:
    """A season plan's output lives in temp, so the temp advice applies."""
    plan = season_plan(tmp_path, 1)
    runner.add(
        FakeResponse(
            stderr="No space left on device",
            returncode=228,
            match="tsMuxeR",
        )
    )

    with pytest.raises(OutOfSpaceError) as excinfo:
        execute_plan(plan, runner)

    message = str(excinfo.value)
    assert str(plan.output_dir) in message
    assert "Temporary files" in message
