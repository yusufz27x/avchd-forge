"""Unit tests for :mod:`avchdforge.selection` (defaults and selection algebra)."""

from __future__ import annotations

import pytest

from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import (
    TrackSelection,
    compatible_audio_indices,
    default_audio_index,
    default_selection,
    describe_selection,
    sanitize_selection,
    selectable_subtitle_indices,
    selection_from_indices,
)

from .conftest import astream, build_media, load_json, sstream, vstream


@pytest.fixture()
def ac3_media():
    """1080p H.264 + AC-3 5.1 (eng, default) + AC-3 2.0 (jpn) + SubRip (eng)."""
    return parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))


@pytest.fixture()
def pgs_media():
    """720p H.264 + LPCM + AAC + LPCM 7.1 + PGS."""
    return parse_ffprobe_json(load_json("mkv_720p_lpcm_multi.json"))


@pytest.fixture()
def hd_media():
    return parse_ffprobe_json(load_json("mkv_1080p_dtshd_truehd_ac3core.json"))


# --------------------------------------------------------------------------
# algebra
# --------------------------------------------------------------------------


def test_selection_is_immutable_and_toggles():
    selection = TrackSelection(audio=(1,), subtitles=())

    toggled = selection.toggle_audio(3)
    assert toggled.audio == (1, 3)
    assert selection.audio == (1,), "the original must not change"

    untoggled = toggled.toggle_audio(1)
    assert untoggled.audio == (3,)
    assert untoggled.toggle_audio(3).audio == ()


def test_with_helpers_and_sorting():
    selection = TrackSelection(audio=(5, 1, 3), subtitles=(4, 2))
    assert selection.sorted_audio() == (1, 3, 5)
    assert selection.sorted_subtitles() == (2, 4)
    assert selection.with_audio(7, 8).audio == (7, 8)
    assert selection.with_subtitles().subtitles == ()
    assert TrackSelection().is_empty is True
    assert selection.is_empty is False
    assert selection_from_indices([2, 1], [9]).audio == (2, 1)


# --------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------


def test_default_selection_picks_first_compatible_audio_and_no_text_subs(ac3_media):
    selection = default_selection(ac3_media)

    assert selection.audio == (1,), "AC-3 5.1 is the flagged default and compatible"
    assert selection.subtitles == (), "SubRip is not switched on automatically"


def test_default_selection_turns_on_all_pgs(pgs_media):
    selection = default_selection(pgs_media)

    assert selection.audio == (1,), "LPCM 2.0 comes first and is supported"
    assert selection.subtitles == (4,)


def test_default_audio_respects_language_priority(ac3_media):
    assert default_audio_index(ac3_media, language_priority=["jpn"]) == 2
    assert default_audio_index(ac3_media, language_priority=["jpn", "eng"]) == 2
    assert default_audio_index(ac3_media, language_priority=["eng", "jpn"]) == 1


def test_default_audio_ignores_priority_that_matches_nothing(ac3_media):
    assert default_audio_index(ac3_media, language_priority=["ger"]) == 1


def test_default_audio_skips_unsupported_tracks():
    media = build_media(
        [
            vstream(),
            astream(1, "truehd", channels=8, profile="Dolby TrueHD", tags={"language": "eng"}),
            astream(2, "ac3", channels=6, tags={"language": "eng"}),
        ]
    )
    assert default_audio_index(media) == 2
    assert default_audio_index(media, language_priority=["eng"]) == 2


def test_default_audio_prefers_default_flag_within_a_language():
    media = build_media(
        [
            vstream(),
            astream(1, "ac3", channels=2, tags={"language": "eng"}),
            astream(2, "ac3", channels=6, disposition={"default": 1}, tags={"language": "eng"}),
        ]
    )
    assert default_audio_index(media) == 2


def test_default_audio_is_none_without_audio():
    media = build_media([vstream(), sstream(1, "hdmv_pgs_subtitle")])
    assert default_audio_index(media) is None
    assert default_selection(media).audio == ()


def test_default_audio_from_hd_fixture():
    """DTS-HD MA is usable (core extraction), the coreless TrueHD is not."""
    media = parse_ffprobe_json(load_json("mkv_1080p_dtshd_truehd_ac3core.json"))
    assert default_audio_index(media) == 1


def test_default_subtitles_follow_language_priority():
    media = build_media(
        [
            vstream(),
            astream(1, "ac3"),
            sstream(2, "hdmv_pgs_subtitle", tags={"language": "eng"}),
            sstream(3, "hdmv_pgs_subtitle", tags={"language": "jpn"}),
            sstream(4, "hdmv_pgs_subtitle", tags={"language": "jpn"}),
        ]
    )
    assert default_selection(media).subtitles == (2, 3, 4)
    assert default_selection(media, subtitle_language_priority=["jpn"]).subtitles == (3, 4, 2)


def test_language_priority_alias_still_works(ac3_media):
    assert default_selection(ac3_media, language_priority=["jpn"]).audio == (2,)


# --------------------------------------------------------------------------
# compatibility helpers
# --------------------------------------------------------------------------


def test_compatible_audio_indices_excludes_unsupported(hd_media):
    compatible = compatible_audio_indices(hd_media)
    assert 1 in compatible, "DTS-HD MA is usable via dca_core"
    assert 2 not in compatible, "TrueHD without a core is not"
    assert 3 in compatible
    assert 5 not in compatible, "E-AC-3 cannot become AC-3 without a re-encode"


def test_selectable_subtitles_exclude_unsupported_codecs():
    media = parse_ffprobe_json(load_json("mkv_1440x1080i_h264_level51.json"))
    selectable = selectable_subtitle_indices(media)
    assert 2 in selectable and 4 in selectable
    assert 3 not in selectable, "dvd_subtitle cannot be converted"


def test_sanitize_selection_drops_dead_indices(ac3_media):
    selection = TrackSelection(audio=(1, 99), subtitles=(3, 42))
    cleaned = sanitize_selection(ac3_media, selection)
    assert cleaned.audio == (1,)
    assert cleaned.subtitles == (3,)


def test_sanitize_selection_drops_unsupported_audio(hd_media):
    selection = TrackSelection(audio=(2, 1))
    cleaned = sanitize_selection(hd_media, selection)
    assert cleaned.audio == (1,)


def test_describe_selection_mentions_the_conversion(pgs_media):
    lines = describe_selection(pgs_media, default_selection(pgs_media))
    assert any("-> copy (LPCM)" in line for line in lines)
    assert any("copy as PGS" in line for line in lines)

    text_media = parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))
    lines = describe_selection(text_media, TrackSelection(audio=(1,), subtitles=(3,)))
    assert any("convert SubRip to PGS" in line for line in lines)
