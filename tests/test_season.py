"""Unit tests for :mod:`avchdforge.season` — merging episode trees into a season.

The trees here are hand-built stand-ins in the shape tsMuxeR writes (one
playlist, ``0..n-1`` clips, clips named ``00000M2TS`` inside the playlist), so
the merge logic can be pinned down without real media. The real thing — two
tsMuxeR muxes through :func:`avchdforge.season.merge_avchd_trees` and then
libbluray's title table — is covered by ``tests/test_e2e.py``.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from avchdforge.bdmv import parse_index, parse_movie_objects, play_playlist
from avchdforge.errors import SeasonMergeError
from avchdforge.season import merge_avchd_trees

TREE_DIRS = (
    "PLAYLIST",
    "CLIPINF",
    "STREAM",
    "BACKUP/PLAYLIST",
    "BACKUP/CLIPINF",
)


def make_episode_tree(root: Path, *, parts: int = 1) -> Path:
    """A minimal single-title tree shaped exactly like tsMuxeR's output."""
    bdmv = root / "BDMV"
    for directory in TREE_DIRS:
        (bdmv / directory).mkdir(parents=True)
    (bdmv / "index.bdmv").write_bytes(b"placeholder index")
    (bdmv / "MovieObject.bdmv").write_bytes(b"placeholder objects")
    (bdmv / "BACKUP/index.bdmv").write_bytes(b"placeholder backup index")
    (bdmv / "BACKUP/MovieObject.bdmv").write_bytes(b"placeholder backup objects")
    playlist = bytearray(b"MPLS0100")
    playlist += b"\x00" * 0x3C
    for stem in range(parts):
        playlist += b"\x00\x50" + f"{stem:05d}M2TS".encode("ascii")
    for target in (bdmv / "PLAYLIST/00000.mpls", bdmv / "BACKUP/PLAYLIST/00000.mpls"):
        target.write_bytes(bytes(playlist))
    for stem in range(parts):
        name = f"{stem:05d}"
        (bdmv / f"CLIPINF/{name}.clpi").write_bytes(b"HDMV0100" + b"\x00" * 16 + b"eng0000000000")
        (bdmv / f"BACKUP/CLIPINF/{name}.clpi").write_bytes(
            b"HDMV0100" + b"\x00" * 16 + b"eng0000000000"
        )
        (bdmv / f"STREAM/{name}.m2ts").write_bytes(b"\x47" * 64 + name.encode("ascii"))
    return root


def make_season(tmp_path: Path, parts: list[int]) -> Path:
    sources = [
        make_episode_tree(tmp_path / f"episode{number}", parts=count)
        for number, count in enumerate(parts, start=1)
    ]
    return merge_avchd_trees(sources, tmp_path / "season").root


def test_merge_keeps_every_clip_byte_for_byte(tmp_path):
    season = make_season(tmp_path, [2, 1])
    for stem, source in ((0, 0), (1, 1), (2, 0)):
        episode = 1 if stem < 2 else 2
        stream = tmp_path / f"episode{episode}" / "BDMV/STREAM"
        assert (season / "BDMV/STREAM" / f"{stem:05d}.m2ts").read_bytes() == (
            stream / f"{source:05d}.m2ts"
        ).read_bytes()


def test_merge_renumbers_playlists_and_retargets_clip_references(tmp_path):
    season = make_season(tmp_path, [2, 1])
    playlists = sorted(path.name for path in (season / "BDMV/PLAYLIST").iterdir())
    assert playlists == ["00000.mpls", "00002.mpls"]
    second = (season / "BDMV/PLAYLIST/00002.mpls").read_bytes()
    assert b"00002M2TS" in second
    assert b"00000M2TS" not in second
    assert b"00001M2TS" not in second


def test_merge_keeps_the_backup_tree_in_sync(tmp_path):
    season = make_season(tmp_path, [1, 1])
    backup = season / "BDMV/BACKUP"
    assert (backup / "index.bdmv").read_bytes() == (season / "BDMV/index.bdmv").read_bytes()
    assert (backup / "MovieObject.bdmv").read_bytes() == (
        season / "BDMV/MovieObject.bdmv"
    ).read_bytes()
    assert sorted(path.name for path in (backup / "PLAYLIST").iterdir()) == [
        "00000.mpls",
        "00001.mpls",
    ]
    assert (backup / "PLAYLIST/00001.mpls").read_bytes() == (
        season / "BDMV/PLAYLIST/00001.mpls"
    ).read_bytes()


def test_merge_writes_one_title_per_episode(tmp_path):
    season = make_season(tmp_path, [2, 1, 1])
    index = parse_index((season / "BDMV/index.bdmv").read_bytes())
    assert [title.id_ref for title in index.titles] == [0, 1, 2]
    assert index.top_menu.id_ref == 3
    assert index.first_play.id_ref == 4

    objects = parse_movie_objects((season / "BDMV/MovieObject.bdmv").read_bytes())
    assert len(objects) == 5
    # Episodes 1..3 play playlists 0, 2 and 3 (the playlists their clips start at).
    assert objects[0].commands[3] == play_playlist(0)
    assert objects[1].commands[3] == play_playlist(2)
    assert objects[2].commands[3] == play_playlist(3)
    assert objects[1].commands[3][4:8] == struct.pack(">I", 2)


def test_merge_reports_the_playlist_numbers(tmp_path):
    sources = [
        make_episode_tree(tmp_path / "one", parts=3),
        make_episode_tree(tmp_path / "two", parts=1),
    ]
    merged = merge_avchd_trees(sources, tmp_path / "season")
    assert merged.playlists == (0, 3)
    assert merged.title_count == 2


def test_merge_refuses_a_non_empty_destination(tmp_path):
    source = make_episode_tree(tmp_path / "episode", parts=1)
    destination = tmp_path / "season"
    destination.mkdir()
    (destination / "keep").write_text("user data")
    with pytest.raises(SeasonMergeError, match="not empty"):
        merge_avchd_trees([source], destination)


def test_merge_refuses_a_tree_without_clips(tmp_path):
    source = make_episode_tree(tmp_path / "episode", parts=1)
    (source / "BDMV/CLIPINF/00000.clpi").unlink()
    with pytest.raises(SeasonMergeError, match="no clip information files"):
        merge_avchd_trees([source], tmp_path / "season")


def test_merge_refuses_a_tree_with_mismatched_clips(tmp_path):
    source = make_episode_tree(tmp_path / "episode", parts=2)
    (source / "BDMV/STREAM/00001.m2ts").unlink()
    with pytest.raises(SeasonMergeError, match="CLIPINF and STREAM do not match"):
        merge_avchd_trees([source], tmp_path / "season")


def test_merge_refuses_a_playlist_that_names_a_foreign_clip(tmp_path):
    source = make_episode_tree(tmp_path / "episode", parts=2)
    (source / "BDMV/PLAYLIST/00000.mpls").write_bytes(b"MPLS0100" + b"\x00" * 16 + b"00007M2TS")
    with pytest.raises(SeasonMergeError, match="00007"):
        merge_avchd_trees([source], tmp_path / "season")
