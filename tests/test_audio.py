"""Unit tests for :mod:`avchdforge.audio` (what happens to each audio track)."""

from __future__ import annotations

import pytest

from avchdforge.audio import (
    AudioAction,
    AudioDecision,
    decide_audio,
    decide_audio_all,
    embedded_ac3_core_index,
)
from avchdforge.probe import parse_ffprobe_json

from .conftest import astream, build_media, load_json, vstream


@pytest.fixture()
def ac3_media():
    return parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))


@pytest.fixture()
def hd_media():
    return parse_ffprobe_json(load_json("mkv_1080p_dtshd_truehd_ac3core.json"))


# --------------------------------------------------------------------------
# plain codecs
# --------------------------------------------------------------------------


def test_ac3_is_copied_unchanged(ac3_media):
    decision = decide_audio(ac3_media, ac3_media.audio[0])

    assert decision.action is AudioAction.PASSTHROUGH
    assert decision.mux_codec_tag == "A_AC3"
    assert decision.resulting_codec == "ac3"
    assert decision.needs_extraction is False
    assert decision.bsf is None
    assert decision.supported is True
    assert decision.describe() == "copy (AC-3)"


def test_dts_core_is_copied_unchanged():
    media = build_media([vstream(), astream(1, "dts", profile="DTS", channels=6)])
    decision = decide_audio(media, media.audio[0])

    assert decision.action is AudioAction.PASSTHROUGH
    assert decision.mux_codec_tag == "A_DTS"


def test_aac_passes_through_with_explanation():
    media = build_media([vstream(), astream(1, "aac", channels=2)])
    decision = decide_audio(media, media.audio[0])

    assert decision.action is AudioAction.PASSTHROUGH
    assert decision.mux_codec_tag == "A_AAC"
    assert "not part of the AVCHD specification" in decision.note
    assert decision.supported is True


@pytest.mark.parametrize(
    ("codec", "sample_rate", "channels"),
    [("pcm_bluray", "48000", 2), ("pcm_s16le", "48000", 6), ("pcm_s24le", "96000", 8)],
)
def test_lpcm_family_passes_through(codec, sample_rate, channels):
    media = build_media(
        [vstream(), astream(1, codec, sample_rate=sample_rate, channels=channels)]
    )
    decision = decide_audio(media, media.audio[0])

    assert decision.action is AudioAction.PASSTHROUGH
    assert decision.mux_codec_tag == "A_LPCM"
    assert decision.resulting_codec == "lpcm"


@pytest.mark.parametrize("codec", ["flac", "opus", "vorbis", "mp3"])
def test_codecs_avchd_cannot_carry_are_refused(codec):
    media = build_media([vstream(), astream(1, codec)])
    decision = decide_audio(media, media.audio[0])

    assert decision.action is AudioAction.UNSUPPORTED
    assert decision.supported is False
    assert codec.upper() in decision.note
    assert decision.mux_codec_tag is None


# --------------------------------------------------------------------------
# HD audio
# --------------------------------------------------------------------------


def test_dts_hd_ma_extracts_core_with_dca_core(hd_media):
    dts_hd = hd_media.audio[0]
    assert dts_hd.profile == "DTS-HD MA"

    decision = decide_audio(hd_media, dts_hd)

    assert decision.action is AudioAction.DTS_CORE
    assert decision.bsf == "dca_core"
    assert decision.container == "dts"
    assert decision.output_suffix == ".dts"
    assert decision.expected_codec == "dts"
    assert decision.mux_codec_tag == "A_DTS"
    assert decision.source_index == dts_hd.index
    assert decision.needs_extraction is True
    assert decision.describe() == "DTS core → DTS"


@pytest.mark.parametrize("profile", ["DTS-HD MA", "DTS-HD HRA", "DTS:X", "dts-hd ma"])
def test_any_dts_hd_profile_goes_through_dca_core(profile):
    media = build_media([vstream(), astream(1, "dts", profile=profile)])
    decision = decide_audio(media, media.audio[0])
    assert decision.action is AudioAction.DTS_CORE
    assert decision.bsf == "dca_core"


def test_eac3_is_refused_because_eac3_core_does_not_make_it_ac3(hd_media):
    eac3 = hd_media.audio[4]

    decision = decide_audio(hd_media, eac3)

    assert decision.action is AudioAction.UNSUPPORTED
    assert decision.supported is False
    assert decision.needs_extraction is False
    assert decision.bsf is None
    assert decision.container is None
    assert decision.mux_codec_tag is None
    assert decision.source_index == eac3.index
    assert "eac3_core" in decision.note
    assert "re-encode" in decision.note


def test_truehd_without_core_is_refused(hd_media):
    truehd = next(track for track in hd_media.audio if track.index == 2)

    decision = decide_audio(hd_media, truehd)

    assert decision.action is AudioAction.UNSUPPORTED
    assert "re-encoding" in decision.note
    assert decision.supported is False


def test_truehd_with_core_uses_the_core_stream(hd_media):
    truehd = next(track for track in hd_media.audio if track.index == 3)

    decision = decide_audio(hd_media, truehd)

    assert decision.action is AudioAction.AC3_CORE_DEMUX
    assert decision.source_index == 4, "the separate ac3 stream is the core"
    assert decision.container == "ac3"
    assert decision.expected_codec == "ac3"
    assert decision.mux_codec_tag == "A_AC3"
    assert decision.bsf is None, "demuxing a stream needs no bitstream filter"
    assert "embedded AC-3 core" in decision.describe()


# --------------------------------------------------------------------------
# core detection helper
# --------------------------------------------------------------------------


def test_embedded_core_requires_matching_channels(hd_media):
    eight_channel_truehd = next(track for track in hd_media.audio if track.index == 2)
    assert embedded_ac3_core_index(hd_media, eight_channel_truehd) is None


def test_embedded_core_ignores_non_truehd_tracks(hd_media):
    dts_hd = hd_media.audio[0]
    assert embedded_ac3_core_index(hd_media, dts_hd) is None


def test_embedded_core_ignores_streams_before_the_truehd_track():
    # ac3 core listed *before* the TrueHD track: never trust it.
    media = build_media(
        [
            vstream(),
            astream(1, "ac3", channels=6),
            astream(2, "truehd", channels=6, profile="Dolby TrueHD"),
        ]
    )
    truehd = media.audio_track(2)
    assert truehd is not None
    assert embedded_ac3_core_index(media, truehd) is None


def test_embedded_core_accepts_unknown_channel_count():
    media = build_media(
        [
            vstream(),
            astream(1, "truehd", channels=None, profile="Dolby TrueHD"),
            astream(2, "ac3", channels=6),
        ]
    )
    truehd = media.audio_track(1)
    assert truehd is not None
    assert embedded_ac3_core_index(media, truehd) == 2


# --------------------------------------------------------------------------
# batch + presentation
# --------------------------------------------------------------------------


def test_decide_audio_all_covers_every_stream(ac3_media):
    decisions = decide_audio_all(ac3_media)
    assert set(decisions) == {track.index for track in ac3_media.audio}
    assert all(isinstance(decision, AudioDecision) for decision in decisions.values())


def test_unsupported_describe_falls_back_to_the_note():
    media = build_media([vstream(), astream(1, "flac")])
    decision = decide_audio(media, media.audio[0])
    assert decision.describe() == decision.note
