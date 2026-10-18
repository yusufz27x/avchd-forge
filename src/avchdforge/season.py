"""Merging single-title AVCHD trees into one multi-title (season) tree.

tsMuxeR cannot write more than one title into a tree: every mux starts its
clip numbers at ``00000`` again and its ``index.bdmv`` carries a single title
table entry. A PS3, however, only mounts one AVCHD folder per USB drive
(``docs/CONTEXT.md`` §2.11), so the episodes have to live side by side in one
``BDMV`` folder. This module builds that folder:

1. the first episode's tree is copied as it is;
2. every later episode's clips, clip information and playlists are renamed
   above the numbers already used and the clip references inside its ``.mpls``
   files are retargeted — ``.clpi`` files carry no name of their own, and
   chapter marks keep pointing inside their own episode;
3. ``index.bdmv`` and ``MovieObject.bdmv`` are rebuilt for one title per
   episode by :func:`avchdforge.bdmv.season_navigation`, in ``BDMV/`` and in
   ``BDMV/BACKUP/``.

The result is what :func:`avchdforge.backend.rename_tree_for_usb` and
:func:`avchdforge.backend.check_output_tree` expect: a folder holding ``BDMV``.
Everything here is pure filesystem work — no subprocess, no Qt.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .bdmv import season_navigation
from .errors import SeasonMergeError

BDMV_DIR: Final[str] = "BDMV"
PLAYLIST_DIR: Final[str] = "PLAYLIST"
CLIPINF_DIR: Final[str] = "CLIPINF"
STREAM_DIR: Final[str] = "STREAM"
BACKUP_DIR: Final[str] = "BACKUP"

#: Clip names are five ASCII digits followed by the stream's codec identifier
#: (``00000M2TS``) wherever a playlist names a clip.
_CLIP_REFERENCE: Final[re.Pattern[bytes]] = re.compile(rb"(\d{5})M2TS")


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


def merge_avchd_trees(sources: Sequence[Path], destination: Path) -> MergedSeason:
    """Merge single-title trees into ``destination/BDMV``.

    ``sources`` are folders that contain a lowercase (pre-USB-rename) ``BDMV``
    tree as tsMuxeR writes it, one per episode, in episode order. ``destination``
    must be empty or missing; the merged tree is written below it.
    """
    sources = [Path(source) for source in sources]
    if not sources:
        raise SeasonMergeError("no episode trees to merge")
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise SeasonMergeError(f"{destination} is not empty — merging would mix two trees together")
    destination.mkdir(parents=True, exist_ok=True)

    target = destination / BDMV_DIR
    playlists: list[int] = []
    next_number = 0
    for number, source in enumerate(sources, start=1):
        bdmv = source / BDMV_DIR
        _check_single_title_tree(bdmv, source, number)
        stems = _clip_stems(bdmv, source)
        _check_playlist_references(bdmv, stems)
        playlists.append(next_number)
        if number == 1:
            shutil.copytree(bdmv, target)
        else:
            _copy_renumbered(bdmv, target, offset=next_number, stems=stems)
        next_number += len(stems)

    _write_navigation(target, playlists)
    return MergedSeason(root=destination, playlists=tuple(playlists))


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


def _copy_renumbered(source: Path, destination: Path, *, offset: int, stems: Sequence[int]) -> None:
    """Copy one episode's tree, shifting every number by ``offset``."""
    mapping = {stem: stem + offset for stem in stems}
    for stem, path in _numbered(source / CLIPINF_DIR, ".clpi").items():
        _copy_clip(path, destination / CLIPINF_DIR / f"{mapping[stem]:05d}.clpi")
    for stem, path in _numbered(source / STREAM_DIR, ".m2ts").items():
        _copy_clip(path, destination / STREAM_DIR / f"{mapping[stem]:05d}.m2ts")
    for stem, path in _numbered(source / PLAYLIST_DIR, ".mpls").items():
        (destination / PLAYLIST_DIR / f"{mapping[stem]:05d}.mpls").write_bytes(
            _map_playlist(path, mapping)
        )
    for stem, path in _numbered(source / BACKUP_DIR / CLIPINF_DIR, ".clpi").items():
        _copy_clip(
            path,
            destination / BACKUP_DIR / CLIPINF_DIR / f"{mapping[stem]:05d}.clpi",
        )
    for stem, path in _numbered(source / BACKUP_DIR / PLAYLIST_DIR, ".mpls").items():
        (destination / BACKUP_DIR / PLAYLIST_DIR / f"{mapping[stem]:05d}.mpls").write_bytes(
            _map_playlist(path, mapping)
        )


def _copy_clip(source: Path, destination: Path) -> None:
    """Copy a clip byte for byte — clip files reference no names of their own."""
    shutil.copy2(source, destination)


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
