#!/usr/bin/env python3
"""Build a PS3-testable multi-title (season) AVCHD tree from episode files.

Every episode goes through the application's own mux path and the trees are
merged by :func:`avchdforge.backend.execute_season` — the same call the GUI
makes: probe -> plan -> prepare -> tsMuxeR per episode, each tree verified,
then merged into one ``BDMV`` folder, renamed to the AVCHD-on-FAT32 names a PS3
mounts from USB and verified as one tree. The title table that was written is
printed at the end.

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


def echo_app_lines(line: str) -> None:
    """Print avchd-forge's own log lines; raw tool output stays out of the transcript."""
    if line.startswith(("[avchd-forge]", "$ ")):
        print(line, end="")


def build_season(
    runner: SubprocessRunner,
    episodes: list[Path],
    work: Path,
    destination: Path,
    *,
    tsmuxer: str,
    ffmpeg: str,
    ffprobe: str,
    keep_work: bool,
) -> backend.SeasonOutcome:
    """Every episode through the application's season path; returns the outcome."""
    plans = []
    for number, source in enumerate(episodes, start=1):
        scratch = work / f"episode{number:03d}"
        print(f"plan:    episode {number} ({source.name})")
        plans.append(
            plan.plan_for_source(
                source,
                runner,
                # A season plan muxes into <scratch>/AVCHD; execute_season
                # requires exactly that (backend.py) and merges the scratch
                # trees into one folder.
                output_dir=scratch / plan.AVCHD_DIR_NAME,
                temp_dir=scratch,
                options=plan.PlanOptions(tsmuxer=tsmuxer, ffmpeg=ffmpeg),
                ffprobe=ffprobe,
            )
        )
    return backend.execute_season(
        plans,
        runner,
        destination=plan.avchd_destination(destination),
        ffprobe=ffprobe,
        on_line=echo_app_lines,
        on_stage=lambda stage: print(f"{stage.key:8} {stage.label}"),
        keep_temp_on_success=keep_work,
        owns_temp=False,  # the script owns ``work``; it reports or removes it itself
    )


def read_first(base: Path, *names: str) -> bytes:
    """The bytes of the first of ``names`` that exists in ``base``.

    execute_season ships the FAT32 flavour (``INDEX.BDM``), while a tree that
    was never renamed has tsMuxeR's optical names (``index.bdmv``); the report
    reads whichever is there.
    """
    for name in names:
        path = base / name
        if path.is_file():
            return path.read_bytes()
    raise VerificationError([f"neither {' nor '.join(names)} in {base}"], base)


def report_titles(outcome: backend.SeasonOutcome, episodes: list[Path]) -> None:
    """Check the written title table against the episodes and print it."""
    bdmv = outcome.root / "BDMV"
    index = parse_index(read_first(bdmv, "INDEX.BDM", "index.bdmv"))
    objects = parse_movie_objects(read_first(bdmv, "MOVIEOBJ.BDM", "MovieObject.bdmv"))
    if len(index.titles) != len(episodes):
        raise VerificationError(
            [f"index has {len(index.titles)} titles for {len(episodes)} episodes"],
            outcome.root,
        )
    for number, (title, playlist) in enumerate(
        zip(index.titles, outcome.playlists, strict=True), start=1
    ):
        episode = objects[title.id_ref]
        if episode.commands[3] != play_playlist(playlist):
            raise VerificationError(
                [f"title {number} object {title.id_ref} does not play playlist {playlist:05d}"],
                outcome.root,
            )

    print()
    print(f"season tree: {outcome.root} ({len(outcome.playlists)} titles)")
    for number, (episode, playlist) in enumerate(
        zip(episodes, outcome.playlists, strict=True), start=1
    ):
        print(f"  title {number}  playlist {playlist:05d}  <- {episode}")
    parts = sorted((outcome.root / "BDMV" / "STREAM").iterdir())
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
        outcome = build_season(
            runner,
            episodes,
            work,
            destination,
            tsmuxer=tsmuxer,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            keep_work=args.keep_work,
        )
        report_titles(outcome, episodes)
    except BaseException:
        print(f"failed — scratch files kept in {work}", file=sys.stderr)
        raise

    if args.keep_work:
        print(f"scratch kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("next steps")
    print(f"  1. copy {outcome.root} to the root of the FAT32 USB drive")
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
