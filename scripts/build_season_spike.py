#!/usr/bin/env python3
"""Build a PS3-testable multi-title (season) AVCHD tree from episode files.

Every episode goes through the application's own mux path (probe -> plan ->
prepare -> tsMuxeR), the single-title trees are merged into one ``BDMV`` folder
(``avchdforge.season``), renamed to the AVCHD-on-FAT32 names a PS3 mounts from
USB and checked. The title table that was written is printed at the end.

    .venv/bin/python scripts/build_season_spike.py --make-demos /tmp/episodes
    .venv/bin/python scripts/build_season_spike.py --out /tmp/season \\
        /tmp/episodes/episode1.mkv /tmp/episodes/episode2.mkv

Then copy the produced ``AVCHD`` folder to the root of a FAT32 USB drive,
insert it into the PS3 and answer the spike questions:

* does the XMB show the AVCHD disc at all (the 8.3 names are present, §2.11)?
* does playback start with episode 1 (FirstPlay -> TopMenu -> title 1)?
* does the control panel's "Go To -> Title X" reach every episode?
* what does the Top Menu button do (it should restart title 1)?

Record the answers in ``docs/CONTEXT.md`` before building on them — this tree
is the same shape the application will ship, but nothing below proves the PS3
accepts it.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from avchdforge import backend, plan  # noqa: E402
from avchdforge.bdmv import (  # noqa: E402
    parse_index,
    parse_movie_objects,
    play_playlist,
)
from avchdforge.errors import AvchdForgeError, VerificationError  # noqa: E402
from avchdforge.process import SubprocessRunner  # noqa: E402
from avchdforge.season import MergedSeason, merge_avchd_trees  # noqa: E402
from avchdforge.tooling import locate_tool  # noqa: E402

FONT_CANDIDATES: Final[tuple[str, ...]] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
)


def resolve(tool: str, override: str | None) -> str:
    lookup = locate_tool(tool, override=override)
    if not lookup.found:
        reason = lookup.reason or "not found"
        searched = ", ".join(lookup.searched) or "(nothing to search)"
        raise SystemExit(f"{tool}: {reason}. Looked at: {searched}")
    return str(lookup.path)


# --------------------------------------------------------------------------
# demo episodes
# --------------------------------------------------------------------------


def make_demos(
    directory: Path, count: int, seconds: int, *, ffmpeg: str, runner: SubprocessRunner
) -> list[Path]:
    """Generate distinguishable demo episodes (big label + running timecode)."""
    directory.mkdir(parents=True, exist_ok=True)
    font = next((Path(path) for path in FONT_CANDIDATES if Path(path).exists()), None)
    episodes = []
    for number in range(1, count + 1):
        target = directory / f"episode{number}.mkv"
        filters = []
        if font is not None:
            filters.append(
                f"drawtext=fontfile={font}:text='EPISODE {number}':fontsize=160:"
                f"fontcolor=white:x=(w-text_w)/2:y=0.30*h"
            )
            filters.append(
                f"drawtext=fontfile={font}:text='%{{pts\\:hms}}':fontsize=110:"
                f"fontcolor=yellow:x=(w-text_w)/2:y=0.60*h"
            )
        argv = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=1920x1080:rate=24000/1001:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={330 * number}:sample_rate=48000:duration={seconds}",
            "-vf",
            ",".join(filters) if filters else "null",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            "2000k",
            "-g",
            "48",
            "-c:a",
            "ac3",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-metadata:s:a:0",
            "language=eng",
            str(target),
        ]
        print(f"demo: {target.name} ({seconds}s, label + timecode)")
        result = runner.run(argv, phase="demo")
        if not result.ok:
            raise SystemExit(f"ffmpeg failed to make {target}:\n{result.stderr[-2000:]}")
        episodes.append(target)
    return episodes


# --------------------------------------------------------------------------
# season build
# --------------------------------------------------------------------------


def mux_episode(
    runner: SubprocessRunner,
    source: Path,
    work: Path,
    number: int,
    *,
    tsmuxer: str,
    ffmpeg: str,
    ffprobe: str,
) -> Path:
    """One episode through the application's mux path; returns its ``AVCHD`` folder."""
    job = plan.plan_for_source(
        source,
        runner,
        output_dir=work / f"episode{number}",
        temp_dir=work / f"episode{number}-tmp",
        options=plan.PlanOptions(tsmuxer=tsmuxer, ffmpeg=ffmpeg),
        ffprobe=ffprobe,
    )
    job.temp_dir.mkdir(parents=True, exist_ok=True)
    for step in job.prep_steps:
        print(f"prepare: {step.label}")
        backend.run_prep_step(job, step, runner, ffprobe=ffprobe)
    print(f"mux:     episode {number} ({source.name})")
    backend.run_mux(job, runner)
    report = backend.check_output_tree(job.output_dir)
    if not report.ok:
        raise VerificationError(report.problems, job.output_dir)
    return job.output_dir


def report_titles(season: MergedSeason, episodes: list[Path]) -> None:
    """Check the written title table against the episodes and print it."""
    bdmv = season.root / "BDMV"
    index = parse_index((bdmv / "index.bdmv").read_bytes())
    objects = parse_movie_objects((bdmv / "MovieObject.bdmv").read_bytes())
    if len(index.titles) != len(episodes):
        raise VerificationError(
            [f"index has {len(index.titles)} titles for {len(episodes)} episodes"],
            season.root,
        )
    for number, (title, playlist) in enumerate(
        zip(index.titles, season.playlists, strict=True), start=1
    ):
        episode = objects[title.id_ref]
        if episode.commands[3] != play_playlist(playlist):
            raise VerificationError(
                [f"title {number} object {title.id_ref} does not play playlist {playlist:05d}"],
                season.root,
            )

    print()
    print(f"season tree: {season.root} ({season.title_count} titles)")
    for number, (episode, playlist) in enumerate(
        zip(episodes, season.playlists, strict=True), start=1
    ):
        print(f"  title {number}  playlist {playlist:05d}  <- {episode}")
    parts = sorted((season.root / "BDMV/STREAM").iterdir())
    total = sum(part.stat().st_size for part in parts)
    print(f"  clips: {len(parts)} parts, {total / 1_000_000:.1f} MB")
    print("  first play -> title 1; top menu button -> title 1")


def build(args: argparse.Namespace) -> int:
    tsmuxer = resolve("tsMuxeR", args.tsmuxer)
    ffmpeg = resolve("ffmpeg", args.ffmpeg)
    ffprobe = resolve("ffprobe", args.ffprobe)
    runner = SubprocessRunner()

    if args.make_demos is not None:
        episodes = make_demos(
            args.make_demos, args.demo_count, args.demo_seconds, ffmpeg=ffmpeg, runner=runner
        )
        print(f"\nwrote {len(episodes)} demo episodes to {args.make_demos}")
        return 0

    episodes = [Path(path).expanduser().absolute() for path in args.episodes]
    if not episodes:
        raise SystemExit("give episode files (or --make-demos DIR); see --help")
    if args.out is None:
        raise SystemExit("--out is required when building a season")
    destination = Path(args.out).expanduser().absolute()
    work = (
        Path(args.work).expanduser().absolute()
        if args.work
        else Path(tempfile.mkdtemp(prefix="avchd-season-"))
    )
    print(f"tsMuxeR: {tsmuxer}")
    print(f"work:    {work}")

    try:
        sources = [
            mux_episode(
                runner,
                source,
                work,
                number,
                tsmuxer=tsmuxer,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
            for number, source in enumerate(episodes, start=1)
        ]
        merged = merge_avchd_trees(sources, destination / "AVCHD")
        report_titles(merged, episodes)
        report = backend.check_output_tree(merged.root)
        if not report.ok:
            raise VerificationError(report.problems, merged.root)
        renamed = backend.rename_tree_for_usb(merged.root)
        print(f"  renamed {len(renamed)} files to the AVCHD-on-FAT32 names")
    except BaseException:
        print(f"failed — scratch files kept in {work}", file=sys.stderr)
        raise

    if args.keep_work:
        print(f"scratch kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("next steps")
    print(f"  1. copy {merged.root} to the root of the FAT32 USB drive")
    print("  2. insert it into the PS3 — the XMB should list the AVCHD disc")
    print("  3. check playback, 'Go To -> Title X' and the Top Menu button")
    print("  4. record what happened in docs/CONTEXT.md before trusting it")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a multi-title (season) AVCHD tree for a PS3 spike.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("episodes", nargs="*", type=Path, help="episode files, in title order")
    parser.add_argument("--out", type=Path, help="folder for the season tree (creates <out>/AVCHD)")
    parser.add_argument("--work", type=Path, help="scratch folder (default: a temp dir)")
    parser.add_argument("--keep-work", action="store_true", help="keep the scratch folder")
    parser.add_argument(
        "--make-demos", type=Path, metavar="DIR", help="generate demo episodes and exit"
    )
    parser.add_argument("--demo-count", type=int, default=2, help="demo episodes to make")
    parser.add_argument("--demo-seconds", type=int, default=40, help="demo episode length")
    parser.add_argument("--tsmuxer", help="tsMuxeR binary (default: $AVCHD_FORGE_TSMUXER, PATH)")
    parser.add_argument("--ffmpeg", help="ffmpeg binary (default: $AVCHD_FORGE_FFMPEG, PATH)")
    parser.add_argument("--ffprobe", help="ffprobe binary (default: $AVCHD_FORGE_FFPROBE, PATH)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return build(args)
    except AvchdForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
