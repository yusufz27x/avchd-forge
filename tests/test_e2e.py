"""Real-tool smoke tests: generated media through the complete pipeline.

These tests are **excluded from the default run** (``pyproject.toml`` sets
``-m 'not e2e'``) because they spawn the real ``tsMuxeR``, ``ffmpeg`` and
``ffprobe``. Run them deliberately::

    .venv/bin/pytest -m e2e -v

``tests/test_e2e.py`` is the only other module besides ``test_qt_process.py``
that is allowed to start processes, and only under the ``e2e`` marker.

What only these tests can prove, with the tools actually installed:

* the generated meta file is accepted by tsMuxeR and really produces an
  ``AVCHD`` tree — the unit tests feed canned tsMuxeR output to the verifier;
* the composition check reads a real ``.m2ts`` and finds the video still
  H.264 at 1920x1080, both AC-3 tracks as AC-3 (6ch + 2ch), and the SubRip
  track converted to PGS;
* ``--split-size`` really writes several parts, none above the limit.

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
)
from avchdforge.plan import PlanOptions, plan_for_source
from avchdforge.probe import probe
from avchdforge.process import SubprocessRunner
from avchdforge.selection import TrackSelection
from avchdforge.tooling import locate_tool

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


def test_generated_media_remuxes_into_a_verified_avchd_tree(
    tools: dict[str, Path], sample: Path, tmp_path: Path
) -> None:
    output_dir = tmp_path / "out"
    temp_dir = tmp_path / "temp"

    outcome = _run_pipeline(sample, tools, output_dir, temp_dir, split_size_mb=4096)

    bdmv = output_dir / "BDMV"
    assert (bdmv / "index.bdmv").is_file()
    assert (bdmv / "MovieObject.bdmv").is_file()
    assert list((bdmv / "PLAYLIST").glob("*.mpls"))
    assert (output_dir / "CERTIFICATE").is_dir()
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
