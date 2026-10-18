"""Real-tool smoke tests: generated media through the complete pipeline.

These tests are **excluded from the default run** (``pyproject.toml`` sets
``-m 'not e2e'``) because they spawn the real ``tsMuxeR``, ``ffmpeg`` and
``ffprobe``. Run them deliberately::

    .venv/bin/pytest -m e2e -v

``tests/test_e2e.py`` is the only other module besides ``test_qt_process.py``
that is allowed to start processes, and only under the ``e2e`` marker.

What only these tests can prove, with the tools actually installed:

* the generated meta file is accepted by tsMuxeR and really produces an
  ``AVCHD`` tree — renamed to the FAT32 (PS3 USB) names the unit tests assert
  against canned tsMuxeR output;
* the composition check reads a real stream part and finds the video still
  H.264 at 1920x1080, both AC-3 tracks as AC-3 (6ch + 2ch), and the SubRip
  track converted to PGS;
* ``--split-size`` really writes several parts, none above the limit;
* two real muxes merge into one tree whose title table libbluray's HDMV VM
  obeys: disc insert plays episode 1, "Go To -> Title 2" plays episode 2
  (:mod:`tests.libbluray`; skipped when libbluray is not installed).

Tools are located exactly like the application does: ``$AVCHD_FORGE_*``
overrides first, then ``PATH``, then the usual install directories (note that
tsMuxeR's release archive does not put it on ``PATH``). When one is missing the
module skips with the search list instead of failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avchdforge.backend import (
    SPLIT_MB_BYTES,
    SPLIT_OVERSHOOT_ALLOWANCE,
    JobOutcome,
    execute_plan,
    run_mux,
    run_prep_step,
)
from avchdforge.bdmv import parse_index, parse_movie_objects, play_playlist
from avchdforge.plan import PlanOptions, plan_for_source
from avchdforge.probe import probe
from avchdforge.process import SubprocessRunner
from avchdforge.season import merge_avchd_trees
from avchdforge.selection import TrackSelection
from avchdforge.tooling import locate_tool

from .libbluray import open_disc, starts_with_clip

pytestmark = pytest.mark.e2e

#: The toolchain the pipeline needs, in the order the startup gate checks it.
TOOL_NAMES = ("tsMuxeR", "ffmpeg", "ffprobe")

#: Long enough for real PES packets and several keyframes, short enough that
#: two full muxes stay in the seconds range.
SAMPLE_SECONDS = 12


@pytest.fixture(scope="session")
def tools() -> dict[str, Path]:
    """Real tool paths, or a skip that lists where each tool was searched."""
    found: dict[str, Path] = {}
    missing: list[str] = []
    for name in TOOL_NAMES:
        lookup = locate_tool(name)
        if lookup.path is None:
            missing.append(f"{name} not found, searched:\n{_searched(lookup.searched)}")
        else:
            found[name] = lookup.path
    if missing:
        pytest.skip(
            "e2e tests need the real toolchain: install the tools, put them on "
            "PATH, or set AVCHD_FORGE_TSMUXER / AVCHD_FORGE_FFMPEG / "
            "AVCHD_FORGE_FFPROBE.\n" + "\n".join(missing)
        )
    return found


def _searched(entries: tuple[str, ...], keep: int = 3) -> str:
    """The search list, condensed to its ends when it is a full PATH listing."""
    if len(entries) > 2 * keep + 1:
        hidden = len(entries) - 2 * keep
        entries = (*entries[:keep], f"… {hidden} more …", *entries[-keep:])
    return "\n".join(f"    {entry}" for entry in entries)


@pytest.fixture(scope="session")
def sample(tools: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A generated ``.mkv``: 1080p24 H.264 + AC-3 5.1 (eng) + AC-3 2.0 (jpn) + SubRip."""
    work = tmp_path_factory.mktemp("e2e-sample")
    subtitle = work / "sample.srt"
    subtitle.write_text(
        "1\n"
        "00:00:00,500 --> 00:00:03,000\n"
        "café — generated line\n"
        "\n"
        "2\n"
        "00:00:03,500 --> 00:00:06,000\n"
        "second line\n",
        encoding="utf-8",
    )
    source = work / "sample.mkv"
    argv = [
        str(tools["ffmpeg"]),
        "-nostdin", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=1920x1080:rate=24000/1001:duration={SAMPLE_SECONDS}",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={SAMPLE_SECONDS}",
        "-f", "lavfi", "-i",
        f"sine=frequency=660:sample_rate=48000:duration={SAMPLE_SECONDS}",
        "-f", "srt", "-i", str(subtitle),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-level", "4.1", "-pix_fmt", "yuv420p", "-b:v", "2000k", "-g", "48",
        "-c:a", "ac3",
        "-b:a:0", "448k", "-ac:a:0", "6",
        "-b:a:1", "192k", "-ac:a:1", "2",
        "-metadata:s:a:0", "language=eng",
        "-metadata:s:a:0", "title=English 5.1",
        "-metadata:s:a:1", "language=jpn",
        "-metadata:s:a:1", "title=Japanese 2.0",
        "-metadata:s:s:0", "language=eng",
        "-c:s", "srt",
        str(source),
    ]
    result = SubprocessRunner().run(argv, phase="e2e")
    if not result.ok and "Unknown encoder 'libx264'" in result.output:
        pytest.skip("the installed ffmpeg has no libx264 encoder to generate the sample")
    assert result.ok, f"could not generate the sample:\n{result.tail(15)}"
    return source


def _run_pipeline(
    source: Path,
    tools: dict[str, Path],
    output_dir: Path,
    temp_dir: Path,
    *,
    split_size_mb: int,
) -> JobOutcome:
    """Plan and execute the whole pipeline with the real toolchain.

    Every audio and subtitle track of the sample is selected, so one run
    exercises both the copy path and the SubRip -> PGS conversion path.
    """
    runner = SubprocessRunner()
    media = probe(source, runner, tool=str(tools["ffprobe"]))
    selection = TrackSelection(
        audio=tuple(track.index for track in media.audio),
        subtitles=tuple(track.index for track in media.subtitles),
    )
    options = PlanOptions(
        tsmuxer=str(tools["tsMuxeR"]), ffmpeg=str(tools["ffmpeg"])
    ).with_split_size(split_size_mb)
    plan = plan_for_source(
        source,
        runner,
        output_dir=output_dir,
        temp_dir=temp_dir,
        selection=selection,
        options=options,
        ffprobe=str(tools["ffprobe"]),
    )
    return execute_plan(plan, runner, ffprobe=str(tools["ffprobe"]))


@pytest.fixture(scope="session")
def window_sample(tools: dict[str, Path], sample: Path, tmp_path_factory) -> Path:
    """The sample's video with a window crop, as a BDRemux release ships it.

    A 1080p x264 encode is coded as 1920x1088 with the bottom eight rows hidden
    by the SPS conformance window. Replacing that window with 26/26 shows
    1920x1036 while the coded frame keeps every row: the Case A shape whose
    repair must be lossless (``docs/CONTEXT.md`` §2.12).
    """
    work = tmp_path_factory.mktemp("e2e-window")
    cropped = work / "window.mkv"
    argv = [
        str(tools["ffmpeg"]),
        "-nostdin", "-hide_banner", "-v", "error", "-y",
        "-i", str(sample),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c", "copy",
        "-bsf:v", "h264_metadata=crop_top=26:crop_bottom=26",
        str(cropped),
    ]
    result = SubprocessRunner().run(argv, phase="e2e")
    assert result.ok, f"could not generate the window-cropped sample:\n{result.tail(15)}"
    return cropped


def test_generated_media_remuxes_into_a_verified_avchd_tree(
    tools: dict[str, Path], sample: Path, tmp_path: Path
) -> None:
    output_dir = tmp_path / "out"
    temp_dir = tmp_path / "temp"

    outcome = _run_pipeline(sample, tools, output_dir, temp_dir, split_size_mb=4096)

    # the app creates the AVCHD folder a PS3 mounts from a USB drive root
    assert (output_dir / "AVCHD").is_dir()
    bdmv = output_dir / "AVCHD" / "BDMV"
    # the shipped tree is the AVCHD-on-FAT32 flavour a PS3 mounts from USB
    assert (bdmv / "INDEX.BDM").is_file()
    assert (bdmv / "MOVIEOBJ.BDM").is_file()
    assert list((bdmv / "PLAYLIST").glob("*.MPL"))
    assert list((bdmv / "STREAM").glob("*.MTS"))
    assert not (bdmv / "index.bdmv").exists()
    assert (output_dir / "AVCHD" / "CERTIFICATE").is_dir()
    assert len(outcome.stream_files) == 1, "4 GB of headroom must not split a 4 MB sample"
    assert outcome.verified_file == outcome.stream_files[0]
    assert not temp_dir.exists(), "a successful job must clean up its temp dir"

    media = probe(outcome.stream_files[0], SubprocessRunner(), tool=str(tools["ffprobe"]))
    video = media.video
    assert video is not None
    assert video.codec == "h264", "video must be copied, not re-encoded"
    assert (video.width, video.height) == (1920, 1080)
    assert [track.codec for track in media.audio] == ["ac3", "ac3"]
    assert {track.channels for track in media.audio} == {6, 2}
    assert [track.codec for track in media.subtitles] == ["hdmv_pgs_subtitle"]


def test_a_window_cropped_source_is_restored_to_1080p_losslessly(
    tools: dict[str, Path], window_sample: Path, tmp_path: Path
) -> None:
    runner = SubprocessRunner()
    source_media = probe(window_sample, runner, tool=str(tools["ffprobe"]))
    video = source_media.video
    assert video is not None
    assert (video.width, video.height) == (1920, 1036), "the release shows 1036 lines"
    assert video.coded_resolution == (1920, 1088), "the missing rows are still coded"
    assert video.output_resolution == (1920, 1080)

    outcome = _run_pipeline(
        window_sample, tools, tmp_path / "out", tmp_path / "temp", split_size_mb=4096
    )

    assert [step.kind for step in outcome.plan.prep_steps] == ["video-window"]
    assert outcome.plan.video is not None and outcome.plan.video.track_id == 0
    assert not (tmp_path / "temp").exists(), "a successful job must clean up its temp dir"

    media = probe(outcome.stream_files[0], SubprocessRunner(), tool=str(tools["ffprobe"]))
    assert media.video is not None
    assert media.video.codec == "h264", "video must be copied, not re-encoded"
    assert (media.video.width, media.video.height) == (1920, 1080)
    assert [track.codec for track in media.audio] == ["ac3"]
    assert media.audio[0].channels == 6


def test_small_split_size_produces_multiple_parts_within_the_limit(
    tools: dict[str, Path], sample: Path, tmp_path: Path
) -> None:
    split_size_mb = 1
    outcome = _run_pipeline(
        sample, tools, tmp_path / "out", tmp_path / "temp", split_size_mb=split_size_mb
    )

    parts = outcome.stream_files
    assert len(parts) >= 2, "a 1 MB split of a 4 MB sample must produce several parts"
    limit = split_size_mb * SPLIT_MB_BYTES + SPLIT_OVERSHOOT_ALLOWANCE
    for part in parts:
        assert part.stat().st_size <= limit, f"{part.name} is above the split limit"

    media = probe(parts[0], SubprocessRunner(), tool=str(tools["ffprobe"]))
    assert media.video is not None and media.video.codec == "h264"
    assert [track.codec for track in media.audio] == ["ac3", "ac3"]


# --------------------------------------------------------------------------
# season: two episodes, one tree, obeyed by libbluray's HDMV VM
# --------------------------------------------------------------------------


def _mux_episode(
    source: Path, tools: dict[str, Path], output_dir: Path, temp_dir: Path
) -> Path:
    """Mux one episode without the USB rename, the way the season merge needs it.

    ``execute_plan`` ends by renaming the tree to the AVCHD-on-FAT32 names; a
    season merge renumbers whole trees instead and renames once, afterwards, so
    this helper stops right before that step. Prep steps still run — the
    window-repaired episode needs one.
    """
    runner = SubprocessRunner()
    media = probe(source, runner, tool=str(tools["ffprobe"]))
    selection = TrackSelection(audio=tuple(track.index for track in media.audio))
    plan = plan_for_source(
        source,
        runner,
        output_dir=output_dir,
        temp_dir=temp_dir,
        selection=selection,
        options=PlanOptions(tsmuxer=str(tools["tsMuxeR"]), ffmpeg=str(tools["ffmpeg"])),
        ffprobe=str(tools["ffprobe"]),
    )
    plan.temp_dir.mkdir(parents=True, exist_ok=True)
    for step in plan.prep_steps:
        run_prep_step(plan, step, runner, ffprobe=str(tools["ffprobe"]))
    run_mux(plan, runner)
    return plan.output_dir


@pytest.fixture(scope="session")
def season_tree(tools: dict[str, Path], sample: Path, window_sample: Path, tmp_path_factory):
    """Two real muxes merged into one tree: the shape the season support ships."""
    work = tmp_path_factory.mktemp("e2e-season")
    sources = [
        _mux_episode(sample, tools, work / "episode1", work / "tmp1"),
        _mux_episode(window_sample, tools, work / "episode2", work / "tmp2"),
    ]
    merged = merge_avchd_trees(sources, work / "season" / "AVCHD")
    assert merged.playlists == (0, 1)
    return merged.root


def test_merged_season_tree_holds_one_title_per_episode(season_tree: Path) -> None:
    bdmv = season_tree / "BDMV"
    assert sorted(path.name for path in (bdmv / "STREAM").iterdir()) == [
        "00000.m2ts",
        "00001.m2ts",
    ]

    index = parse_index((bdmv / "index.bdmv").read_bytes())
    assert [title.id_ref for title in index.titles] == [0, 1]
    assert index.top_menu.id_ref == 2
    assert index.first_play.id_ref == 3

    objects = parse_movie_objects((bdmv / "MovieObject.bdmv").read_bytes())
    assert len(objects) == 4
    assert objects[0].commands[3] == play_playlist(0)
    assert objects[1].commands[3] == play_playlist(1)

    # the second episode's playlist was renumbered: its clip reference moved
    second = (bdmv / "PLAYLIST/00001.mpls").read_bytes()
    assert b"00001M2TS" in second
    assert b"00000M2TS" not in second

    # BACKUP carries the same rebuilt navigation
    assert (bdmv / "BACKUP/MovieObject.bdmv").read_bytes() == (
        bdmv / "MovieObject.bdmv"
    ).read_bytes()
    assert (bdmv / "BACKUP/index.bdmv").read_bytes() == (bdmv / "index.bdmv").read_bytes()


def test_libbluray_plays_episode_1_on_insert_and_episode_2_via_title_search(
    season_tree: Path,
) -> None:
    """libbluray runs the real HDMV VM: insert, then Go To -> Title 2."""
    try:
        disc = open_disc(season_tree)
    except OSError as exc:
        pytest.skip(str(exc))
    with disc:
        assert disc.play(), "disc insert must start the FirstPlay object"
        assert starts_with_clip(disc.read(), season_tree / "BDMV/STREAM/00000.m2ts"), (
            "inserting the disc must play episode 1 (title 1)"
        )
        assert disc.play_title(2), "title 2 must be selectable"
        assert starts_with_clip(disc.read(), season_tree / "BDMV/STREAM/00001.m2ts"), (
            "'Go To -> Title 2' must play episode 2"
        )
