"""Unit tests for :mod:`avchdforge.probe` (ffprobe parsing)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avchdforge import probe as probe_module
from avchdforge.errors import ProbeError
from avchdforge.probe import (
    AudioTrack,
    MediaInfo,
    build_ffprobe_argv,
    normalize_language,
    parse_ffprobe_json,
    parse_frame_rate,
    probe,
)

from .conftest import FakeRunner, FakeResponse, load_json


# --------------------------------------------------------------------------
# argv contract
# --------------------------------------------------------------------------


def test_build_ffprobe_argv_is_exactly_as_documented():
    argv = build_ffprobe_argv("/usr/bin/ffprobe", Path("/media/My Movie.mkv"))
    assert argv == [
        "/usr/bin/ffprobe",
        "-hide_banner",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "/media/My Movie.mkv",
    ]


def test_build_ffprobe_argv_accepts_str_and_path():
    assert build_ffprobe_argv("ffprobe", "a.mkv") == build_ffprobe_argv("ffprobe", Path("a.mkv"))


# --------------------------------------------------------------------------
# scalar parsers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("24000/1001", 23.976023976023978),
        ("30000/1001", 29.97002997002997),
        ("25/1", 25.0),
        ("59.94", 59.94),
        ("0/0", None),
        ("N/A", None),
        ("", None),
        (None, None),
        ("-1", None),
        ("garbage", None),
    ],
)
def test_parse_frame_rate(value, expected):
    result = parse_frame_rate(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("eng", "eng"),
        ("ENG", "eng"),
        ("en", "eng"),
        ("en-US", "eng"),
        ("jpn,eng", "jpn"),
        ("deu", "ger"),  # ISO 639-2/T -> 639-2/B, what tsMuxeR expects
        ("fra", "fre"),
        ("zho", "chi"),
        ("und", "und"),
        ("Unicode", "und"),
        ("", "und"),
        (None, "und"),
        ("e", "und"),
        ("english", "und"),
        ("12", "und"),
    ],
)
def test_normalize_language(value, expected):
    assert normalize_language(value) == expected


# --------------------------------------------------------------------------
# real fixtures
# --------------------------------------------------------------------------


def test_parse_1080p_mkv_fixture():
    media = parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))

    # ffprobe reports whatever path it was handed; the fixture was generated with
    # an absolute path, so only the file name is stable.
    assert media.path.name == "fixture_1080p.mkv"
    assert media.format_name == "matroska,webm"
    assert media.duration == pytest.approx(4.0, abs=0.1)
    assert media.size_bytes and media.size_bytes > 0

    assert len(media.videos) == 1
    video = media.video
    assert video is not None
    assert video.index == 0
    assert video.codec == "h264"
    assert video.is_avc is True
    assert video.profile == "High"
    assert video.level == pytest.approx(4.1)
    assert video.resolution == (1920, 1080)
    assert video.fps == pytest.approx(23.976, abs=0.001)
    assert video.is_progressive is True
    assert video.resolution_label == "1920x1080p"
    assert "High@4.1" in video.summary()

    assert [track.language for track in media.audio] == ["eng", "jpn"]
    english, japanese = media.audio
    assert english.codec == "ac3"
    assert english.channels == 6
    assert english.channel_label == "5.1(side)"
    assert english.sample_rate_label == "48 kHz"
    assert english.bitrate_label == "448 kbps"
    assert english.is_default is True
    assert english.title == "English 5.1"
    assert english.is_hd is False
    assert english.is_lpcm is False
    assert japanese.channels == 2
    assert japanese.is_default is False

    assert len(media.subtitles) == 1
    subtitle = media.subtitles[0]
    assert subtitle.codec == "subrip"
    assert subtitle.is_text is True
    assert subtitle.is_pgs is False
    assert subtitle.is_supported is True
    assert subtitle.language == "eng"

    assert media.raw["streams"], "raw payload must be preserved for the log pane"


def test_parse_720p_fixture_keeps_aac_and_reports_fps():
    media = parse_ffprobe_json(load_json("mkv_720p_h264_aac_srt.json"))

    video = media.video
    assert video is not None
    assert video.resolution == (1280, 720)
    assert video.fps == pytest.approx(59.94, abs=0.01)
    assert media.audio[0].codec == "aac"
    assert media.audio[0].is_hd is False
    assert media.audio[0].is_lpcm is False


def test_parse_hevc_fixture_is_not_avc():
    media = parse_ffprobe_json(load_json("mkv_1080p_hevc.json"))

    video = media.video
    assert video is not None
    assert video.codec == "hevc"
    assert video.is_avc is False


def test_parse_transport_stream_fixture():
    media = parse_ffprobe_json(load_json("ts_1080p_h264_dts.json"))

    assert media.format_name.startswith("mpegts")
    assert media.video is not None and media.video.is_avc
    dts = media.audio[0]
    assert dts.codec == "dts"
    assert dts.channels == 6
    assert dts.is_hd is False, "a plain DTS core track is not HD"


# --------------------------------------------------------------------------
# handcrafted fixtures
# --------------------------------------------------------------------------


def test_hd_audio_flavours_fixture():
    media = parse_ffprobe_json(load_json("mkv_1080p_dtshd_truehd_ac3core.json"))

    dts_hd, truehd_no_core, truehd_with_core, ac3_core, eac3 = media.audio
    assert dts_hd.profile == "DTS-HD MA"
    assert dts_hd.is_hd is True
    assert dts_hd.codec_label == "DTS (DTS-HD MA)"

    assert truehd_no_core.codec == "truehd"
    assert truehd_no_core.is_hd is True
    assert truehd_no_core.channels == 8

    assert truehd_with_core.codec == "truehd"
    # "fra" is normalised to the 639-2/B spelling tsMuxeR accepts
    assert truehd_with_core.language == "fre"
    assert ac3_core.codec == "ac3"
    assert ac3_core.language == "fre"

    assert eac3.codec == "eac3"
    assert eac3.is_hd is True
    assert eac3.codec_label == "E-AC-3"


def test_two_video_streams_are_both_parsed():
    media = parse_ffprobe_json(load_json("mkv_two_video_streams.json"))
    assert len(media.videos) == 2
    assert media.video is media.videos[0]
    assert [track.index for track in media.streams] == [0, 1, 2]


def test_lpcm_and_subtitle_flags_fixture():
    media = parse_ffprobe_json(load_json("mkv_720p_lpcm_multi.json"))

    lpcm_96, aac, lpcm_71 = media.audio
    assert lpcm_96.is_lpcm is True
    assert lpcm_96.sample_rate == 96_000
    assert lpcm_96.codec_label == "pcm_bluray".upper()  # not in the label table, uppercased
    assert aac.codec == "aac"
    assert lpcm_71.channels == 8
    assert media.subtitles[0].is_pgs is True
    assert media.subtitles[0].summary().startswith("PGS")


def test_empty_ffprobe_payload_is_tolerated():
    media = parse_ffprobe_json(load_json("ffprobe_empty.json"))

    assert isinstance(media, MediaInfo)
    assert media.video is None
    assert media.videos == ()
    assert media.audio == ()
    assert media.subtitles == ()
    assert media.duration is None
    assert media.duration_label == "unknown"
    assert media.size_label == "unknown"


def test_unexpected_stream_entries_are_skipped():
    payload = {
        "streams": [
            {"codec_type": "data", "index": 0},
            {"codec_type": "attachment", "index": 1},
            {"index": 2, "codec_type": "video"},  # no codec name at all
            {"codec_type": "video", "index": 3, "codec_name": "mjpeg",
             "disposition": {"attached_pic": 1}},  # cover art
            {"codec_type": "audio", "index": 4, "codec_name": "ac3", "channels": 2},
            {"codec_type": "audio"},  # no index -> unusable
        ],
        "format": {},
    }
    media = parse_ffprobe_json(payload)

    assert [track.index for track in media.videos] == [2]
    assert media.videos[0].codec == ""
    assert [track.index for track in media.audio] == [4]


def test_audio_lookup_helpers():
    media = parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))
    assert media.audio_track(2) is not None
    assert media.audio_track(2).language == "jpn"
    assert media.audio_track(99) is None
    assert media.subtitle_track(3) is not None
    assert media.subtitle_track(0) is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"codec": "dts", "profile": "DTS-HD HRA"}, True),
        ({"codec": "dts", "profile": "DTS:X"}, True),
        ({"codec": "dts", "profile": "DTS"}, False),
        ({"codec": "dts", "profile": None}, False),
        ({"codec": "ac3"}, False),
        ({"codec": "truehd"}, True),
        ({"codec": "eac3"}, True),
        ({"codec": "aac"}, False),
    ],
)
def test_audio_is_hd_matrix(kwargs, expected):
    track = AudioTrack(index=1, **kwargs)
    assert track.is_hd is expected


# --------------------------------------------------------------------------
# runner integration
# --------------------------------------------------------------------------


def test_probe_runs_ffprobe_and_parses_stdout():
    payload = load_json("mkv_1080p_h264_ac3x2_srt.json")
    runner = FakeRunner(
        responses=[FakeResponse(stdout=json.dumps(payload), match="ffprobe")],
    )

    media = probe(Path("samples/fixture_1080p.mkv"), runner, tool="/opt/ffprobe")

    assert runner.calls == [build_ffprobe_argv("/opt/ffprobe", "samples/fixture_1080p.mkv")]
    assert runner.phases == ["probe"]
    assert media.video is not None and media.video.is_avc


def test_probe_raises_probe_error_on_failure():
    runner = FakeRunner(responses=[FakeResponse(stderr="No such file\n", returncode=1)])

    with pytest.raises(ProbeError) as excinfo:
        probe("missing.mkv", runner)

    assert "exit code 1" in str(excinfo.value)
    assert "No such file" in str(excinfo.value)
    assert excinfo.value.returncode == 1


def test_probe_raises_probe_error_on_invalid_json():
    runner = FakeRunner(responses=[FakeResponse(stdout="not json at all")])

    with pytest.raises(ProbeError) as excinfo:
        probe("broken.mkv", runner)

    assert "not JSON" in str(excinfo.value)


def test_parse_ffprobe_json_rejects_non_mapping():
    with pytest.raises(ValueError):
        probe_module.parse_ffprobe_json(["not", "a", "mapping"])  # type: ignore[arg-type]
