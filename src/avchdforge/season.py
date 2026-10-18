"""Merging single-title AVCHD trees into one multi-title (season) tree.

tsMuxeR cannot write more than one title into a tree: every mux starts its
clip numbers at ``00000`` again and its ``index.bdmv`` carries a single title
table entry. A PS3, however, only mounts one AVCHD folder per USB drive
(``docs/CONTEXT.md`` §2.11), so the episodes have to live side by side in one
``BDMV`` folder. This module builds that folder, one episode at a time
(:class:`SeasonBuilder`):

1. an episode's clips, clip information and playlists are renamed above the
   numbers already used and the clip references inside its ``.mpls`` files are
   retargeted — ``.clpi`` files carry no name of their own, and chapter marks
   keep pointing inside their own episode;
2. ``index.bdmv`` and ``MovieObject.bdmv`` are rebuilt for one title per
   episode by :func:`avchdforge.bdmv.season_navigation`, in ``BDMV/`` and in
   ``BDMV/BACKUP/``, once the last episode is in.

The result is what :func:`avchdforge.backend.rename_tree_for_usb` and
:func:`avchdforge.backend.check_output_tree` expect: a folder holding ``BDMV``.
Everything here is pure filesystem work — no subprocess, no Qt.

``move=True`` consumes the source trees: on one filesystem a renamed file, which
is how a season run that stages its muxes on the target drive avoids writing
every episode twice; across filesystems a copy and a delete, never an error.
Copies run in :data:`TRANSFER_BLOCK` blocks and hand control back between blocks
(``should_cancel``, ``pump``), so a caller on the GUI thread can keep painting
and offer a working Cancel through a multi-GB transfer (D29); an interrupted
transfer removes its half-written destination file and leaves the source intact.
A season run stages one episode at a time — ``backend.execute_season`` adds each
episode as soon as its own tree is verified and cleans that episode's scratch
files away — so the temp directory holds about one episode, not the whole season.

:func:`merge_avchd_trees` merges a whole batch at once on top of
:class:`SeasonBuilder`; it validates every source before the first byte is
written, which is what callers that can afford to stage the batch want (a failed
merge then leaves the destination empty, so it cannot block a retry). The
builder itself validates one episode at a time, before that episode's bytes.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .bdmv import season_navigation
from .drives import free_bytes
from .errors import JobCancelled, OutOfSpaceError, SeasonMergeError

BDMV_DIR: Final[str] = "BDMV"
PLAYLIST_DIR: Final[str] = "PLAYLIST"
CLIPINF_DIR: Final[str] = "CLIPINF"
STREAM_DIR: Final[str] = "STREAM"
BACKUP_DIR: Final[str] = "BACKUP"

#: Clip names are five ASCII digits followed by the stream's codec identifier
#: (``00000M2TS``) wherever a playlist names a clip.
_CLIP_REFERENCE: Final[re.Pattern[bytes]] = re.compile(rb"(\d{5})M2TS")

#: Bytes per read/write block of a transfer. Big enough that a drive stays
#: saturated (a season is tens of GB and device-bound), small enough that the
#: caller can pump its event loop and notice a cancellation between blocks: a
#: copied season spends minutes inside that loop on the GUI thread otherwise.
TRANSFER_BLOCK: Final[int] = 4 * 1024 * 1024


@dataclass(frozen=True)
class MergedSeason:
    """A merged tree: the folder holding ``BDMV`` and its title table.

    ``playlists[i]`` is the playlist number title ``i + 1`` runs; because every
    episode keeps tsMuxeR's one-playlist-per-episode layout, the playlist
    numbers are also the episodes' first clip numbers.
    """

    root: Path
    playlists: tuple[int, ...]

    @property
    def title_count(self) -> int:
        return len(self.playlists)


def merge_avchd_trees(
    sources: Sequence[Path],
    destination: Path,
    *,
    move: bool = False,
    on_progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    pump: Callable[[], None] | None = None,
) -> MergedSeason:
    """Merge single-title trees into ``destination/BDMV`` — the whole batch at once.

    ``sources`` are folders that contain a lowercase (pre-USB-rename) ``BDMV``
    tree as tsMuxeR writes it, one per episode, in episode order. ``destination``
    must be empty or missing; the merged tree is written below it.

    Validation happens for every source before anything is written, so a bad
    tree fails the merge with the destination untouched. ``move`` consumes the
    sources' clip files instead of copying them; ``on_progress`` receives the
    share of the episodes' stream bytes put in place (the streams dominate the
    data; the playlists and clip info files that ride along are ignored in both
    the numerator and the denominator).

    A caller that cannot stage the whole batch up front — a GUI season run of
    tens of GB, which must not hold the whole season in temp — uses
    :class:`SeasonBuilder` and adds each episode as it is ready instead.
    """
    sources = [Path(source) for source in sources]
    if not sources:
        raise SeasonMergeError("no episode trees to merge")
    episodes = [
        _validate_tree(source, number) for number, source in enumerate(sources, start=1)
    ]
    sizes = [float(episode.stream_bytes) for episode in episodes]
    total = sum(sizes)

    moved = 0.0
    current = 0.0

    def report(fraction: float) -> None:
        if on_progress is not None:
            on_progress(min((moved + fraction * current) / total, 1.0) if total else 1.0)

    builder = SeasonBuilder(
        destination,
        move=move,
        should_cancel=should_cancel,
        pump=pump,
    )
    for episode, size in zip(episodes, sizes, strict=True):
        current = size
        builder._add_validated(episode, on_progress=report)
        moved += size
    return builder.finish()


class SeasonBuilder:
    """Build one multi-title tree out of single-title episode trees, one at a time.

    ``backend.execute_season`` adds each episode right after its own tree was
    muxed and verified, so a season run stages about one episode instead of the
    whole season (D29) — and each episode is validated when it is added, so a
    bad tree still fails before *its* bytes are written. ``finish`` writes the
    season's navigation files (``index.bdmv``/``MovieObject.bdmv``, root and
    ``BACKUP``) and returns the merged tree.

    ``on_progress`` (given per :meth:`add`) receives the share of that episode's
    stream bytes put in place, so a caller can map it onto per-episode progress;
    ``should_cancel`` is asked between transfer blocks and ``pump`` is called
    after each one. With ``move`` the episode's files are consumed as they land.
    """

    def __init__(
        self,
        destination: Path,
        *,
        move: bool = False,
        should_cancel: Callable[[], bool] | None = None,
        pump: Callable[[], None] | None = None,
    ) -> None:
        self._destination = Path(destination)
        if self._destination.exists() and any(self._destination.iterdir()):
            raise SeasonMergeError(
                f"{self._destination} is not empty — merging would mix two trees together"
            )
        self._move = move
        self._should_cancel = should_cancel
        self._pump = pump
        self._episodes: list[_EpisodeTree] = []
        self._playlists: list[int] = []

    def add(
        self, tree: Path, *, on_progress: Callable[[float], None] | None = None
    ) -> int:
        """Validate one episode tree and transfer it; returns its playlist number.

        ``tree`` is the folder tsMuxeR wrote ``BDMV/`` into, as
        :func:`merge_avchd_trees` takes it. The episode gets the next clip
        numbers, and its playlist references are retargeted to them.
        """
        source = Path(tree)
        return self._add_validated(
            _validate_tree(source, len(self._playlists) + 1), on_progress=on_progress
        )

    def _add_validated(
        self,
        episode: _EpisodeTree,
        *,
        on_progress: Callable[[float], None] | None = None,
    ) -> int:
        """Transfer an already validated episode tree; returns its playlist number."""
        # A playlist's number is the number of the episode's first clip, which is
        # also the playlist tsMuxeR itself wrote for the episode (one per mux).
        playlist = sum(len(episode.stems) for episode in self._episodes)
        # The episode's own stream bytes set the progress scale; measure them
        # before the transfer — ``move`` takes the sources away.
        total = episode.stream_bytes
        done = 0

        def report(amount: int) -> None:
            nonlocal done
            done += amount
            if on_progress is not None:
                on_progress(min(done / total, 1.0) if total else 1.0)

        self._episodes.append(episode)
        self._playlists.append(playlist)
        _copy_renumbered(
            episode.bdmv,
            self._destination / BDMV_DIR,
            offset=playlist,
            stems=episode.stems,
            move=self._move,
            on_stream=report,
            should_cancel=self._should_cancel,
            pump=self._pump,
        )
        return playlist

    def finish(self) -> MergedSeason:
        """Write the season's navigation files and return the merged tree."""
        if not self._playlists:
            raise SeasonMergeError("no episode trees to merge")
        _write_navigation(self._destination / BDMV_DIR, self._playlists)
        return MergedSeason(root=self._destination, playlists=tuple(self._playlists))


def _validate_tree(tree: Path, episode: int) -> _EpisodeTree:
    """Check that ``tree`` is exactly what tsMuxeR writes for one episode."""
    bdmv = tree / BDMV_DIR
    _check_single_title_tree(bdmv, tree, episode)
    stems = _clip_stems(bdmv, tree)
    _check_playlist_references(bdmv, stems)
    return _EpisodeTree(bdmv=bdmv, stems=stems)


@dataclass(frozen=True)
class _EpisodeTree:
    """One validated episode source: where its tree is and what numbers it uses."""

    bdmv: Path
    stems: tuple[int, ...]

    @property
    def streams(self) -> tuple[Path, ...]:
        return tuple(self.bdmv / STREAM_DIR / f"{stem:05d}.m2ts" for stem in self.stems)

    @property
    def stream_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.streams)


def _check_single_title_tree(bdmv: Path, source: Path, episode: int) -> None:
    """Refuse anything that is not exactly what tsMuxeR writes for one episode."""
    for directory in (
        PLAYLIST_DIR,
        CLIPINF_DIR,
        STREAM_DIR,
        f"{BACKUP_DIR}/{PLAYLIST_DIR}",
        f"{BACKUP_DIR}/{CLIPINF_DIR}",
    ):
        if not (bdmv / directory).is_dir():
            raise SeasonMergeError(
                f"episode {episode} ({source}) is not a single-title AVCHD tree: "
                f"{bdmv / directory} is missing"
            )
    playlists = _numbered(bdmv / PLAYLIST_DIR, ".mpls")
    backup_playlists = _numbered(bdmv / BACKUP_DIR / PLAYLIST_DIR, ".mpls")
    if sorted(playlists) != [0] or sorted(backup_playlists) != [0]:
        raise SeasonMergeError(
            f"episode {episode} ({source}): expected exactly one playlist "
            f"00000.mpls, found {sorted(playlists)} (backup {sorted(backup_playlists)})"
        )


def _numbered(directory: Path, suffix: str) -> dict[int, Path]:
    """Files named ``<digits>{suffix}``, keyed by their number."""
    found: dict[int, Path] = {}
    for path in directory.iterdir():
        stem = path.name[: -len(suffix)]
        if path.name.endswith(suffix) and stem.isdigit():
            found[int(stem)] = path
    return found


def _clip_stems(bdmv: Path, source: Path) -> tuple[int, ...]:
    """The clip numbers of one episode tree; must be exactly ``0..n-1``."""
    clips = _numbered(bdmv / CLIPINF_DIR, ".clpi")
    streams = _numbered(bdmv / STREAM_DIR, ".m2ts")
    if not clips:
        raise SeasonMergeError(f"episode {source}: no clip information files")
    if set(clips) != set(streams):
        raise SeasonMergeError(
            f"episode {source}: CLIPINF and STREAM do not match "
            f"(clips {sorted(clips)}, streams {sorted(streams)})"
        )
    if sorted(clips) != list(range(len(clips))):
        raise SeasonMergeError(
            f"episode {source}: clip numbers are not 0..{len(clips) - 1} (found {sorted(clips)})"
        )
    backup_clips = _numbered(bdmv / BACKUP_DIR / CLIPINF_DIR, ".clpi")
    if set(backup_clips) != set(clips):
        raise SeasonMergeError(
            f"episode {source}: BACKUP/CLIPINF does not match CLIPINF "
            f"({sorted(backup_clips)} vs {sorted(clips)})"
        )
    return tuple(sorted(clips))


def _check_playlist_references(bdmv: Path, stems: Sequence[int]) -> None:
    """Every playlist may only name the episode's own clips — also the first one.

    The first episode is copied verbatim, so this is the only check that its
    playlists really belong to its clips.
    """
    identity = {stem: stem for stem in stems}
    for path in (
        bdmv / PLAYLIST_DIR / "00000.mpls",
        bdmv / BACKUP_DIR / PLAYLIST_DIR / "00000.mpls",
    ):
        _map_playlist(path, identity)


def _copy_renumbered(
    source: Path,
    destination: Path,
    *,
    offset: int,
    stems: Sequence[int],
    move: bool = False,
    on_stream: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    pump: Callable[[], None] | None = None,
) -> None:
    """Copy one episode's tree, shifting every number by ``offset``."""
    mapping = {stem: stem + offset for stem in stems}
    for directory in (
        PLAYLIST_DIR,
        CLIPINF_DIR,
        STREAM_DIR,
        f"{BACKUP_DIR}/{PLAYLIST_DIR}",
        f"{BACKUP_DIR}/{CLIPINF_DIR}",
    ):
        (destination / directory).mkdir(parents=True, exist_ok=True)
    for stem, path in _numbered(source / CLIPINF_DIR, ".clpi").items():
        _transfer(path, destination / CLIPINF_DIR / f"{mapping[stem]:05d}.clpi", move=move)
    for stem, path in _numbered(source / STREAM_DIR, ".m2ts").items():
        _transfer(
            path,
            destination / STREAM_DIR / f"{mapping[stem]:05d}.m2ts",
            move=move,
            on_block=on_stream,
            should_cancel=should_cancel,
            pump=pump,
        )
    for stem, path in _numbered(source / PLAYLIST_DIR, ".mpls").items():
        (destination / PLAYLIST_DIR / f"{mapping[stem]:05d}.mpls").write_bytes(
            _map_playlist(path, mapping)
        )
        if move:
            path.unlink()
    for stem, path in _numbered(source / BACKUP_DIR / CLIPINF_DIR, ".clpi").items():
        _transfer(
            path,
            destination / BACKUP_DIR / CLIPINF_DIR / f"{mapping[stem]:05d}.clpi",
            move=move,
        )
    for stem, path in _numbered(source / BACKUP_DIR / PLAYLIST_DIR, ".mpls").items():
        (destination / BACKUP_DIR / PLAYLIST_DIR / f"{mapping[stem]:05d}.mpls").write_bytes(
            _map_playlist(path, mapping)
        )
        if move:
            path.unlink()


def _transfer(
    source: Path,
    destination: Path,
    *,
    move: bool,
    on_block: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    pump: Callable[[], None] | None = None,
) -> None:
    """Put a clip file in place byte for byte, reporting its bytes as they land.

    Clip files reference no names of their own, so they need no rewriting; a
    move is a rename on the same filesystem and a copy plus a delete across
    filesystems, which is exactly ``shutil.move``'s contract — except that the
    copy runs in blocks so a caller can follow it and interrupt it instead of
    blocking for minutes on a multi-GB file (D29).
    """
    if should_cancel is not None and should_cancel():
        raise JobCancelled("copying to the season folder")
    try:
        if move:
            _move_file(
                source,
                destination,
                on_block=on_block,
                should_cancel=should_cancel,
                pump=pump,
            )
        else:
            _copy_file(
                source,
                destination,
                on_block=on_block,
                should_cancel=should_cancel,
                pump=pump,
            )
    except (OutOfSpaceError, JobCancelled):
        raise
    except OSError as exc:
        raise SeasonMergeError(
            f"could not put {source.name} into {destination}: {exc}"
        ) from exc


def _move_file(
    source: Path,
    destination: Path,
    *,
    on_block: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    pump: Callable[[], None] | None = None,
) -> None:
    """Move one file: a rename when the filesystems match, a block copy otherwise.

    ``os.replace`` is tried first on purpose — on one filesystem the season
    transfer is then a rename per file, however large the clips — and only a
    genuine cross-device error falls back to the blockwise copy plus delete.
    """
    size = source.stat().st_size
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_file(
            source,
            destination,
            on_block=on_block,
            should_cancel=should_cancel,
            pump=pump,
        )
        source.unlink()
        return
    if on_block is not None:
        on_block(size)
    if pump is not None:
        pump()


def _copy_file(
    source: Path,
    destination: Path,
    *,
    on_block: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    pump: Callable[[], None] | None = None,
) -> None:
    """Copy one file in blocks, giving the caller's loop a turn between them."""
    try:
        with source.open("rb") as src, destination.open("wb") as dst:
            while True:
                if should_cancel is not None and should_cancel():
                    raise JobCancelled("copying to the season folder")
                block = src.read(TRANSFER_BLOCK)
                if not block:
                    break
                dst.write(block)
                if on_block is not None:
                    on_block(len(block))
                if pump is not None:
                    pump()
        shutil.copystat(source, destination)
    except OSError as exc:
        # A half-written file is not evidence, it is a trap (and on a full disk
        # the space is better handed back); the source is complete either way.
        destination.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            raise OutOfSpaceError(
                destination.parent, free_bytes=free_bytes(destination.parent)
            ) from exc
        raise
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _map_playlist(path: Path, mapping: dict[int, int]) -> bytes:
    """Every clip reference in a playlist, mapped to its merged number.

    A reference the episode does not contain would mean the tree was already
    mixed with something else; that is a hard failure, not a guess.
    """

    def replace(match: re.Match[bytes]) -> bytes:
        stem = int(match.group(1))
        if stem not in mapping:
            raise SeasonMergeError(
                f"{path}: playlist names clip {stem:05d}, which the episode tree does not contain"
            )
        return f"{mapping[stem]:05d}".encode("ascii") + b"M2TS"

    patched, count = _CLIP_REFERENCE.subn(replace, path.read_bytes())
    if count == 0:
        raise SeasonMergeError(f"{path}: playlist names no clips at all")
    return patched


def _write_navigation(bdmv: Path, playlists: Sequence[int]) -> None:
    """Write the season's index and movie objects, plus their BACKUP copies."""
    navigation = season_navigation(playlists)
    for directory in (bdmv, bdmv / BACKUP_DIR):
        (directory / "index.bdmv").write_bytes(navigation.index)
        (directory / "MovieObject.bdmv").write_bytes(navigation.movie_objects)
