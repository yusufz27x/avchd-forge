"""Unit tests for :mod:`avchdforge.tracks` (detection parsing + Track ID mapping).

The fixtures in ``tests/data/tsmuxer`` are verbatim captures of
``tsMuxeR <file>`` output for the matching ffprobe fixtures, so the mapping
assertions below are what a real run produced (``docs/CONTEXT.md`` §2.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avchdforge.errors import ToolExecutionError, TrackMappingError
from avchdforge.probe import parse_ffprobe_json
from avchdforge.tracks import (
    RAW_STREAM_TRACK_ID,
    TrackIdMap,
    TsmuxerTrack,
    build_detect_argv,
    describe_detection,
    detect_tracks,
    parse_detect_output,
    raw_track_id,
    resolve_track_ids,
)

from .conftest import astream, build_media, load_json, load_text, sstream, vstream

TSMUXER = "/opt/tsMuxeR/tsmuxer"

RAW_DETECT_OUTPUT = """\
tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer
Stream type: DTS
Stream ID:   A_DTS
Stream info: Bitrate: 768Kbps  Sample Rate: 48KHz  Channels: 5.1
Stream lang: eng

"""


@pytest.fixture()
def mkv_media():
    return parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))


@pytest.fixture()
def ts_media():
    return parse_ffprobe_json(load_json("ts_1080p_h264_dts.json"))


@pytest.fixture()
def mkv_detection():
    return parse_detect_output(load_text("detect_mkv.txt"))


@pytest.fixture()
def ts_detection():
    return parse_detect_output(load_text("detect_ts.txt"))


# --------------------------------------------------------------------------
# detection parsing
# --------------------------------------------------------------------------


def test_detection_argv_is_the_bare_tsmuxer_invocation():
    argv = build_detect_argv(TSMUXER, Path("/in/movie.mkv"))
    assert argv == [TSMUXER, "/in/movie.mkv"]
    assert "--avchd" not in argv and "--split-size" not in argv, "detection muxes nothing"


def test_mkv_detection_parses_every_stream(mkv_detection):
    assert [track.track_id for track in mkv_detection] == [1, 2, 3, 4]
    assert [track.family for track in mkv_detection] == ["avc", "ac3", "ac3", "srt"]
    assert [track.media_kind for track in mkv_detection] == [
        "video",
        "audio",
        "audio",
        "subtitle",
    ]
    assert [track.language for track in mkv_detection] == ["und", "eng", "jpn", "eng"]
    assert mkv_detection[0].stream_type == "H.264"
    assert "1920:1080p" in mkv_detection[0].info
    assert [track.ordinal for track in mkv_detection] == [0, 1, 2, 3]


def test_ts_detection_ids_are_pids_not_ordinals(ts_detection):
    assert [track.track_id for track in ts_detection] == [256, 257]
    assert [track.family for track in ts_detection] == ["avc", "dts"]
    assert [track.language for track in ts_detection] == ["", ""], "TS carries no lang tag here"


def test_parser_tolerates_irregular_spacing_and_headers():
    text = (
        "tsMuxeR version 2.7.0. github.com/justdan96/tsMuxer\n"
        "Track ID: 5\n"
        "Stream type:  TrueHD\n"
        "Stream ID:\tA_AC3\n"
        "Noise that belongs to no entry\n"
        "Stream lang: fre\n"
        "\n"
        "Duration: 00:01:00.000\n"
    )

    tracks = parse_detect_output(text)

    assert len(tracks) == 1
    assert tracks[0].track_id == 5
    assert tracks[0].stream_type == "TrueHD", "TrueHD is reported under Stream ID A_AC3"
    assert tracks[0].family == "ac3"
    assert tracks[0].language == "fre"


def test_raw_elementary_streams_have_no_track_id():
    tracks = parse_detect_output(RAW_DETECT_OUTPUT)

    assert len(tracks) == 1
    assert tracks[0].track_id is None
    assert tracks[0].family == "dts"
    assert tracks[0].describe() == "track raw · DTS · A_DTS · eng"


def test_parser_ignores_output_without_any_stream_type():
    assert parse_detect_output("tsMuxeR version 2.7.0\nDuration: 00:00:00\n") == ()


def test_unknown_stream_ids_keep_their_name():
    tracks = parse_detect_output(
        "Track ID: 2\nStream type: Fancy\nStream ID: V_FANCY/CODEC\n"
    )
    assert tracks[0].family == "unknown:V_FANCY/CODEC"
    assert tracks[0].media_kind == "video"


@pytest.mark.parametrize(
    ("stream_id", "kind"),
    [("V_MPEG4/ISO/AVC", "video"), ("A_AC3", "audio"), ("S_HDMV/PGS", "subtitle")],
)
def test_media_kind_comes_from_the_stream_id_prefix(stream_id, kind):
    track = TsmuxerTrack(track_id=1, stream_type="x", stream_id=stream_id)
    assert track.media_kind == kind


# --------------------------------------------------------------------------
# detection run through the runner
# --------------------------------------------------------------------------


def test_detect_tracks_uses_the_runner_and_reports_the_phase(runner, tmp_media):
    runner.add_stdout(load_text("detect_mkv.txt"))
    source = tmp_media("movie.mkv")

    tracks = detect_tracks(TSMUXER, source, runner)

    assert [track.track_id for track in tracks] == [1, 2, 3, 4]
    assert runner.calls == [[TSMUXER, str(source)]]
    assert runner.phases == ["detect"]


def test_detect_tracks_raises_with_the_exact_command_on_failure(runner, tmp_media):
    runner.add_error("Can't open file: nope.mkv", returncode=255)
    source = tmp_media("nope.mkv")

    with pytest.raises(ToolExecutionError) as excinfo:
        detect_tracks(TSMUXER, source, runner)

    assert excinfo.value.returncode == 255
    assert excinfo.value.tool == "tsMuxeR"
    assert "Can't open file" in str(excinfo.value)
    assert str(source) in str(excinfo.value)


def test_describe_detection_reads_like_a_log_block(mkv_detection):
    lines = describe_detection(mkv_detection)
    assert lines[0] == "tsMuxeR sees track 1 · H.264 · V_MPEG4/ISO/AVC · und"
    assert len(lines) == 4


# --------------------------------------------------------------------------
# mapping: MKV (1-based file order)
# --------------------------------------------------------------------------


def test_mkv_streams_map_one_to_one(mkv_media, mkv_detection):
    resolved = resolve_track_ids(mkv_media, mkv_detection)

    assert resolved.ids == {0: 1, 1: 2, 2: 3, 3: 4}
    assert resolved.diagnostics == ()
    assert resolved.id_for(2) == 3
    assert resolved.id_for(99) is None
    assert len(resolved) == 4
    resolved.require([0, 1, 2, 3])


def test_ts_streams_map_to_pids(ts_media, ts_detection):
    resolved = resolve_track_ids(ts_media, ts_detection)

    assert resolved.ids == {0: 256, 1: 257}
    assert resolved.diagnostics == ()


# --------------------------------------------------------------------------
# mapping: alignment rules
# --------------------------------------------------------------------------


def test_repeated_codec_families_align_in_order():
    media = parse_ffprobe_json(load_json("mkv_720p_lpcm_multi.json"))
    detection = parse_detect_output(
        "Track ID:    1\n"
        "Stream type: H.264\n"
        "Stream ID:   V_MPEG4/ISO/AVC\n"
        "\n"
        "Track ID:    2\n"
        "Stream type: LPCM\n"
        "Stream ID:   A_LPCM\n"
        "\n"
        "Track ID:    3\n"
        "Stream type: AAC\n"
        "Stream ID:   A_AAC\n"
        "\n"
        "Track ID:    4\n"
        "Stream type: LPCM\n"
        "Stream ID:   A_LPCM\n"
        "\n"
        "Track ID:    5\n"
        "Stream type: PGS\n"
        "Stream ID:   S_HDMV/PGS\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert resolved.ids == {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}, "the AAC stream must not steal LPCM 4"
    assert resolved.diagnostics == ()


def test_extra_detection_entries_are_skipped_not_mismatched():
    media = build_media([vstream(0), astream(1, "dts")])
    detection = parse_detect_output(
        "Track ID:    1\n"  # a second video angle ffprobe did not report
        "Stream type: MPEG2\n"
        "Stream ID:   V_MPEG-2\n"
        "\n"
        "Track ID:    2\n"
        "Stream type: H.264\n"
        "Stream ID:   V_MPEG4/ISO/AVC\n"
        "\n"
        "Track ID:    3\n"
        "Stream type: DTS\n"
        "Stream ID:   A_DTS\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert resolved.ids == {0: 2, 1: 3}
    assert resolved.diagnostics == ()


def test_two_video_streams_map_to_distinct_ids():
    media = parse_ffprobe_json(load_json("mkv_two_video_streams.json"))
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    3\nStream type: AC3\nStream ID:   A_AC3\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert resolved.ids == {0: 1, 1: 2, 2: 3}


def test_truehd_stream_maps_onto_the_a_ac3_reporting():
    media = build_media([vstream(0), astream(1, "truehd", profile="Dolby TrueHD")])
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: TrueHD\nStream ID:   A_AC3\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert resolved.ids == {0: 1, 1: 2}, "tsMuxeR groups AC-3, AC-3+ and TrueHD under A_AC3"


# --------------------------------------------------------------------------
# mapping: refusals
# --------------------------------------------------------------------------


def test_a_subtitle_ffprobe_sees_but_tsmuxer_misses_is_refused(mkv_media, mkv_detection):
    trimmed = tuple(track for track in mkv_detection if track.media_kind != "subtitle")
    assert trimmed, "fixture sanity"

    resolved = resolve_track_ids(mkv_media, trimmed)

    assert 3 not in resolved.ids
    assert any("tsMuxeR reports no subtitle streams" in note for note in resolved.diagnostics)
    with pytest.raises(TrackMappingError) as excinfo:
        resolved.require([0, 1, 2, 3])
    message = str(excinfo.value)
    assert "stream 3" in message
    assert "Refusing to guess track numbers." in message


def test_a_subtitle_of_a_different_family_is_reported_per_stream():
    media = build_media([vstream(0), sstream(1, "ass")])
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: PGS\nStream ID:   S_HDMV/PGS\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert 1 not in resolved.ids
    assert any("subtitle stream 1 (ass)" in note for note in resolved.diagnostics)
    with pytest.raises(TrackMappingError):
        resolved.require([1])


def test_an_unrelated_codec_family_stays_unmapped():
    media = build_media([vstream(0), astream(1, "aac")])
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: DTS\nStream ID:   A_DTS\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert resolved.ids == {0: 1}
    assert any("audio stream 1 (aac)" in note for note in resolved.diagnostics)
    with pytest.raises(TrackMappingError):
        resolved.require([1])


def test_an_unknown_ffprobe_codec_never_matches_a_detection_entry():
    media = build_media([vstream(0), astream(1, "mystery")])
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: Mystery\nStream ID:   A_MYSTERY\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert 1 not in resolved.ids
    assert any("unknown:mystery" in note for note in resolved.diagnostics)


def test_media_streams_without_detection_entries_are_reported():
    media = build_media([vstream(0), astream(1, "ac3")])

    resolved = resolve_track_ids(media, parse_detect_output(""))

    assert resolved.ids == {}
    assert any("tsMuxeR reports no video streams" in note for note in resolved.diagnostics)
    assert any("tsMuxeR reports no audio streams" in note for note in resolved.diagnostics)
    assert any("ffprobe sees 1" in note for note in resolved.diagnostics)


def test_detection_entries_without_a_track_id_cannot_be_used():
    media = build_media([astream(0, "dts")])
    detection = parse_detect_output(RAW_DETECT_OUTPUT)

    resolved = resolve_track_ids(media, detection)

    assert resolved.ids == {}
    assert any("no Track ID in tsMuxeR's report" in note for note in resolved.diagnostics)


def test_stream_selection_need_not_include_the_video_for_validation():
    media = build_media([vstream(0), astream(1, "ac3")])
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: AC3\nStream ID:   A_AC3\n"
    )

    resolved = resolve_track_ids(media, detection)

    resolved.require([1])
    resolved.require([])


def test_require_error_mentions_alignment_when_there_is_nothing_else():
    with pytest.raises(TrackMappingError) as excinfo:
        TrackIdMap(ids={}).require([4])

    assert "no alignment information" in str(excinfo.value)
    assert "stream 4" in str(excinfo.value)


# --------------------------------------------------------------------------
# raw streams
# --------------------------------------------------------------------------


def test_raw_streams_always_use_track_zero():
    assert RAW_STREAM_TRACK_ID == 0
    assert raw_track_id() == 0


def test_a_subtitle_only_media_maps_to_its_own_kind():
    media = build_media([vstream(0), sstream(1, "subrip")])
    detection = parse_detect_output(
        "Track ID:    1\nStream type: H.264\nStream ID:   V_MPEG4/ISO/AVC\n"
        "\nTrack ID:    2\nStream type: SRT\nStream ID:   S_TEXT/UTF8\n"
    )

    resolved = resolve_track_ids(media, detection)

    assert resolved.ids == {0: 1, 1: 2}
