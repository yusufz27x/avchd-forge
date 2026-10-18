"""Unit tests for :mod:`avchdforge.bdmv` — the BDMV navigation builders.

The golden fixtures in ``tests/data/bdmv`` are the ``index.bdmv`` and
``MovieObject.bdmv`` tsMuxeR 2.7.0 wrote for a real AVCHD run. The builders must
reproduce them byte for byte before the multi-title generalisation can be
trusted; the field layout itself was cross-checked against libbluray's parsers
(``index_parse.c``, ``mobj_parse.c``, ``hdmv_insn.h``), not against tsMuxeR
alone.
"""

from __future__ import annotations

import pytest

from avchdforge.bdmv import (
    OBJECT_TYPE_HDMV,
    RESUME_INTENTION,
    build_index,
    build_movie_objects,
    episode_object,
    first_play_object,
    parse_index,
    parse_movie_objects,
    play_playlist,
    season_navigation,
    top_menu_object,
)
from avchdforge.errors import NavigationDataError

from .conftest import DATA_DIR

BDMV_DATA = DATA_DIR / "bdmv"


def fixture_bytes(name: str) -> bytes:
    return (BDMV_DATA / name).read_bytes()


# --------------------------------------------------------------------------
# golden files: tsMuxeR's own output must come back byte for byte
# --------------------------------------------------------------------------


def test_index_builder_reproduces_tsmuxer_output():
    built = build_index(first_play=2, top_menu=1, titles=[0])
    assert built == fixture_bytes("avchd_index.bdmv")


def test_movie_object_builder_reproduces_tsmuxer_output():
    built = build_movie_objects([episode_object(0), top_menu_object(), first_play_object()])
    assert built == fixture_bytes("avchd_movieobject.bdmv")


def test_single_title_season_navigation_reproduces_both_files():
    navigation = season_navigation([0])
    assert navigation.index == fixture_bytes("avchd_index.bdmv")
    assert navigation.movie_objects == fixture_bytes("avchd_movieobject.bdmv")
    assert navigation.playlists == (0,)
    assert navigation.title_count == 1


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------


def test_parse_index_reads_the_golden_table():
    index = parse_index(fixture_bytes("avchd_index.bdmv"))
    assert index.first_play.id_ref == 2
    assert index.top_menu.id_ref == 1
    assert index.first_play.interactive is True
    assert index.top_menu.interactive is True
    assert index.first_play.object_type == OBJECT_TYPE_HDMV
    assert len(index.titles) == 1
    assert index.titles[0].id_ref == 0
    assert index.titles[0].interactive is False


def test_parse_movie_objects_reads_the_golden_objects():
    objects = parse_movie_objects(fixture_bytes("avchd_movieobject.bdmv"))
    assert [len(obj.commands) for obj in objects] == [5, 9, 5]
    assert all(obj.resume_intention for obj in objects)
    assert all(not obj.menu_call_mask and not obj.title_search_mask for obj in objects)
    assert all(len(command) == 12 for obj in objects for command in obj.commands)
    assert objects[0].commands[-1][:2] == b"\x00\x02"  # HDMV break ends the object


def test_parse_index_rejects_a_missing_signature():
    with pytest.raises(NavigationDataError):
        parse_index(b"not an index")


def test_parse_index_rejects_a_truncated_table():
    data = bytearray(fixture_bytes("avchd_index.bdmv"))
    data[8:12] = (len(data) - 2).to_bytes(4, "big")  # table starts past the end
    with pytest.raises(NavigationDataError):
        parse_index(bytes(data))


def test_parse_movie_objects_rejects_a_missing_signature():
    with pytest.raises(NavigationDataError):
        parse_movie_objects(b"\x00" * 64)


def test_parse_movie_objects_rejects_a_truncated_command_list():
    data = bytearray(fixture_bytes("avchd_movieobject.bdmv"))
    data[0x30:0x32] = (500).to_bytes(2, "big")  # claim 500 commands
    with pytest.raises(NavigationDataError):
        parse_movie_objects(bytes(data))


# --------------------------------------------------------------------------
# the season generalisation
# --------------------------------------------------------------------------


def test_season_navigation_gives_each_episode_a_title():
    navigation = season_navigation([0, 2, 5])
    index = parse_index(navigation.index)
    assert [title.id_ref for title in index.titles] == [0, 1, 2]
    assert index.top_menu.id_ref == 3
    assert index.first_play.id_ref == 4

    objects = parse_movie_objects(navigation.movie_objects)
    assert len(objects) == 5  # three episodes + top menu + first play
    assert objects[0].commands[3] == play_playlist(0)
    assert objects[1].commands[3] == play_playlist(2)
    assert objects[2].commands[3] == play_playlist(5)
    assert objects[3] == top_menu_object()
    assert objects[4] == first_play_object()


def test_index_grows_by_one_entry_per_episode():
    single = len(season_navigation([0]).index)
    triple = len(season_navigation([0, 1, 2]).index)
    assert triple == single + 2 * 12


def test_movie_objects_mark_resume_intention():
    objects = parse_movie_objects(season_navigation([0]).movie_objects)
    assert all(obj.resume_intention for obj in objects)
    # The flag word is what a player reads; check it end to end.
    assert int.from_bytes(objects[0].encode()[:2], "big") == RESUME_INTENTION


def test_season_navigation_needs_at_least_one_episode():
    with pytest.raises(NavigationDataError):
        season_navigation([])
