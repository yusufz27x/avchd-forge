"""Unit tests for :mod:`avchdforge.validate`.

Rule codes match ``docs/IMPLEMENTATION_PLAN.md`` (V1 … V20).
"""

from __future__ import annotations

import pytest

from avchdforge.probe import parse_ffprobe_json
from avchdforge.selection import TrackSelection
from avchdforge.validate import validate_all, validate_container, validate_selection
from avchdforge.validate_types import Issue, ValidationReport, error, warning

from .conftest import astream, build_media, load_json, sstream, vstream


def codes(report: ValidationReport, severity: str | None = None) -> set[str]:
    if severity == "error":
        issues = report.errors
    elif severity == "warning":
        issues = report.warnings
    else:
        issues = report.issues
    return {issue.code for issue in issues}


def issue(report: ValidationReport, code: str) -> Issue:
    matches = [item for item in report.issues if item.code == code]
    assert matches, f"expected an issue with code {code}, got {[i.code for i in report.issues]}"
    return matches[0]


@pytest.fixture()
def ac3_media():
    return parse_ffprobe_json(load_json("mkv_1080p_h264_ac3x2_srt.json"))


# --------------------------------------------------------------------------
# container rules
# --------------------------------------------------------------------------


def test_clean_1080p_container_is_error_free(ac3_media):
    report = validate_container(ac3_media)

    assert report.ok is True
    assert codes(report, "error") == set()
    assert report.warnings == ()
    assert bool(report) is True


def test_v1_rejects_unknown_container():
    media = build_media([vstream(), astream()], path="release.mp4")
    report = validate_container(media)

    assert "V1" in codes(report, "error")
    assert ".mp4" in issue(report, "V1").message


def test_v1_accepts_all_documented_containers():
    for suffix in (".mkv", ".ts", ".m2ts", ".mpg", ".mpeg"):
        media = build_media([vstream(), astream()], path=f"release{suffix}")
        assert validate_container(media).ok, suffix


def test_v2_requires_exactly_one_video_stream():
    none = validate_container(build_media([astream()]))
    assert "V2" in codes(none, "error")
    assert "No video stream" in issue(none, "V2").message

    many = parse_ffprobe_json(load_json("mkv_two_video_streams.json"))
    report = validate_container(many)
    assert "V2" in codes(report, "error")
    assert "2 video streams" in issue(report, "V2").message


def test_v3_rejects_non_avc_video():
    media = parse_ffprobe_json(load_json("mkv_1080p_hevc.json"))
    report = validate_container(media)

    assert "V3" in codes(report, "error")
    message = issue(report, "V3").message
    assert "HEVC" in message
    assert "never re-encodes" in message
    assert issue(report, "V3").track_index == 0


@pytest.mark.parametrize("resolution", [(1440, 1080), (1918, 1080), (3840, 2160), (720, 576)])
def test_v4_rejects_illegal_resolutions(resolution):
    width, height = resolution
    media = build_media([vstream(width=width, height=height), astream()])
    report = validate_container(media)

    assert "V4" in codes(report, "error")
    assert "1920x1080 or 1280x720" in issue(report, "V4").message


@pytest.mark.parametrize("resolution", [(1920, 1080), (1280, 720)])
def test_v4_accepts_the_two_legal_resolutions(resolution):
    width, height = resolution
    media = build_media([vstream(width=width, height=height), astream()])
    assert "V4" not in codes(validate_container(media))


@pytest.mark.parametrize("resolution", [(1920, 1036), (1920, 1038), (1920, 800)])
def test_v4_becomes_a_warning_when_non_standard_resolutions_are_allowed(resolution):
    width, height = resolution
    media = build_media([vstream(width=width, height=height), astream()])

    report = validate_container(media, allow_nonstandard_resolution=True)

    assert report.ok is True
    assert "V4" in codes(report, "warning")
    assert "V4" not in codes(report, "error")
    assert "1920x1080 or 1280x720" in issue(report, "V4").message
    assert "not guaranteed" in issue(report, "V4").message


def test_the_opt_in_does_not_excuse_other_errors():
    media = build_media([vstream(width=1920, height=1036), vstream(index=2)])

    report = validate_container(media, allow_nonstandard_resolution=True)

    assert report.ok is False
    assert "V2" in codes(report, "error")


def test_v5_warns_above_level_41():
    media = build_media([vstream(level=51), astream()])
    report = validate_container(media)

    assert report.ok is True
    assert "V5" in codes(report, "warning")
    assert "level 5.1" in issue(report, "V5").message


def test_v5_warns_when_level_is_unknown():
    media = build_media([vstream(level=None), astream()])
    report = validate_container(media)

    assert "V5" in codes(report, "warning")
    assert "Could not determine" in issue(report, "V5").message


def test_v5_accepts_level_41_and_below():
    for level in (30, 31, 40, 41):
        media = build_media([vstream(level=level), astream()])
        assert "V5" not in codes(validate_container(media))


def test_v6_warns_above_the_frame_rate_ceiling():
    too_fast_1080 = build_media([vstream(avg_frame_rate="60000/1001"), astream()])
    assert "V6" in codes(validate_container(too_fast_1080), "warning")

    ok_720 = parse_ffprobe_json(load_json("mkv_720p_h264_aac_srt.json"))
    assert "V6" not in codes(validate_container(ok_720), "warning")

    too_fast_720 = build_media(
        [vstream(width=1280, height=720, avg_frame_rate="120000/1001"), astream()]
    )
    assert "V6" in codes(validate_container(too_fast_720), "warning")


def test_v7_warns_on_interlaced_video():
    media = build_media([vstream(field_order="tt"), astream()])
    report = validate_container(media)

    assert "V7" in codes(report, "warning")
    assert "interlaced" in issue(report, "V7").message


def test_v8_warns_above_24_mbps():
    media = build_media([vstream(bit_rate="30000000"), astream()])
    report = validate_container(media)

    assert "V8" in codes(report, "warning")
    assert "30.0 Mbps" in issue(report, "V8").message
    assert "V8" not in codes(validate_container(build_media([vstream(), astream()])))


def test_v10_warns_about_unsupported_audio_codecs():
    media = build_media([vstream(), astream(1, "flac")])
    report = validate_container(media)

    assert report.ok is True, "an unsupported track is only a problem if selected"
    assert "V10" in codes(report, "warning")


def test_v12_warns_about_truehd_without_core():
    media = build_media([vstream(), astream(1, "truehd", profile="Dolby TrueHD", channels=8)])
    report = validate_container(media)

    assert "V12" in codes(report, "warning")
    assert "TrueHD" in issue(report, "V12").message


def test_v17_warns_when_duration_is_missing():
    media = build_media([vstream(), astream()], duration=None)
    assert "V17" in codes(validate_container(media), "warning")


def test_v18_lists_unconvertible_subtitles():
    media = parse_ffprobe_json(load_json("mkv_1440x1080i_h264_level51.json"))
    report = validate_container(media)

    assert "V18" in codes(report, "warning")
    assert "VobSub" in issue(report, "V18").message


def test_v20_warns_about_channel_less_audio_streams():
    media = build_media([vstream(), astream(1, "ac3", channels=0)])
    report = validate_container(media)

    assert "V20" in codes(report, "warning")


# --------------------------------------------------------------------------
# selection rules
# --------------------------------------------------------------------------


def test_v9_requires_audio(ac3_media):
    report = validate_selection(ac3_media, TrackSelection(audio=(), subtitles=()))

    assert "V9" in codes(report, "error")
    assert report.ok is False


def test_v9_reports_missing_audio_index(ac3_media):
    report = validate_selection(ac3_media, TrackSelection(audio=(99,)))
    assert "V9" in codes(report, "error")


def test_v10_warns_when_aac_is_selected():
    media = parse_ffprobe_json(load_json("mkv_720p_h264_aac_srt.json"))
    report = validate_selection(media, TrackSelection(audio=(1,)))

    assert report.ok is True
    assert "V10" in codes(report, "warning")
    assert "outside the AVCHD specification" in issue(report, "V10").message


def test_v11_warns_about_lpcm_96khz_and_8_channels():
    media = parse_ffprobe_json(load_json("mkv_720p_lpcm_multi.json"))

    report_96k = validate_selection(media, TrackSelection(audio=(1,)))
    assert "V11" in codes(report_96k, "warning")
    assert "96 kHz" in issue(report_96k, "V11").message

    report_71 = validate_selection(media, TrackSelection(audio=(3,)))
    assert "V11" in codes(report_71, "warning")
    assert "8 channels" in issue(report_71, "V11").message

    report_ok = validate_selection(media, TrackSelection(audio=(1,), subtitles=(4,)))
    assert report_ok.ok is True


def test_v12_blocks_coreless_truehd_selection():
    media = parse_ffprobe_json(load_json("mkv_1080p_dtshd_truehd_ac3core.json"))
    report = validate_selection(media, TrackSelection(audio=(2,)))

    assert "V12" in codes(report, "error")
    assert report.ok is False


def test_truehd_with_core_can_be_selected():
    media = parse_ffprobe_json(load_json("mkv_1080p_dtshd_truehd_ac3core.json"))
    report = validate_selection(media, TrackSelection(audio=(3,)))

    assert report.ok is True
    assert codes(report, "error") == set()


def test_v13_warns_when_pgs_is_larger_than_the_video():
    media = build_media(
        [
            vstream(width=1280, height=720),
            astream(1),
            sstream(2, "hdmv_pgs_subtitle", width=1920, height=1080),
        ]
    )
    report = validate_selection(media, TrackSelection(audio=(1,), subtitles=(2,)))

    assert "V13" in codes(report, "warning")


def test_v14_warns_when_subtitles_exist_but_none_is_selected(ac3_media):
    report = validate_selection(ac3_media, TrackSelection(audio=(1,)))

    assert "V14" in codes(report, "warning")

    no_subs = build_media([vstream(), astream(1)])
    assert "V14" not in codes(validate_selection(no_subs, TrackSelection(audio=(1,))))


def test_v19_blocks_unusable_subtitle_selections():
    media = parse_ffprobe_json(load_json("mkv_1440x1080i_h264_level51.json"))

    missing = validate_selection(media, TrackSelection(audio=(1,), subtitles=(77,)))
    assert "V19" in codes(missing, "error")

    vobsub = validate_selection(media, TrackSelection(audio=(1,), subtitles=(3,)))
    assert "V19" in codes(vobsub, "error")
    assert "cannot be converted to PGS" in issue(vobsub, "V19").message

    pgs = validate_selection(media, TrackSelection(audio=(1,), subtitles=(2,)))
    assert "V19" not in codes(pgs, "error")


def test_v20_blocks_selection_of_a_channel_less_stream():
    media = build_media([vstream(), astream(1, "ac3", channels=0)])
    report = validate_selection(media, TrackSelection(audio=(1,)))

    assert "V9" in codes(report, "error")
    assert "no usable channels" in issue(report, "V9").message


# --------------------------------------------------------------------------
# combined report
# --------------------------------------------------------------------------


def test_validate_all_merges_every_source(ac3_media):
    report = validate_all(
        ac3_media,
        TrackSelection(audio=(1,), subtitles=(3,)),
        extra_issues=[warning("V15", "target is exfat, not FAT32")],
    )

    assert report.ok is True
    assert codes(report, "warning") == {"V15"}


def test_validate_all_reports_container_errors(ac3_media):
    report = validate_all(ac3_media, TrackSelection(audio=(1,)), check_extension=False)
    assert report.ok is True

    bad = build_media([vstream(), astream()], path="movie.avi")
    assert validate_all(bad, TrackSelection(audio=(1,))).ok is False


def test_validate_all_passes_the_non_standard_resolution_opt_in():
    media = build_media([vstream(width=1920, height=1036), astream()])

    assert validate_all(media, TrackSelection(audio=(1,))).ok is False
    allowed = validate_all(
        media, TrackSelection(audio=(1,)), allow_nonstandard_resolution=True
    )
    assert allowed.ok is True
    assert "V4" in {item.code for item in allowed.warnings}


def test_issue_and_report_helpers(ac3_media):
    report = validate_all(ac3_media, TrackSelection(audio=()))

    assert report.ok is False
    assert bool(report) is False
    assert report.errors
    assert "V9" in {item.code for item in report.errors}
    assert report.messages("error") == [item.message for item in report.errors]

    merged = report.merged(ValidationReport((error("X1", "boom"),)))
    assert "X1" in {item.code for item in merged.errors}
    assert "ERROR" in str(merged.errors[0])
    assert "WARN" in str(warning("W1", "careful"))
