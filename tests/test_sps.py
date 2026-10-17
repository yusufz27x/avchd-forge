"""Unit tests for :mod:`avchdforge.sps` (SPS geometry and the window repair).

The fixtures in ``tests/data/sps`` are real ``ffprobe -show_data`` extradata
hexdumps:

* ``avcc_*`` — Matroska ``AVCDecoderConfigurationRecord`` (``avcC``);
* ``annexb_*`` — MPEG-TS extradata, start-code prefixed NAL units;
* ``avcc_window_crop_1036`` / ``annexb_window_crop_1036`` — 1920x1088 coded,
  window at 1920x1036 (the *fixable* case: the bars are still coded);
* ``avcc_hard_crop_1036`` — a real BDRemux re-encode, 1920x1040 coded with a
  1920x1036 window (the *unfixable* case: the rows were never encoded);
* ``avcc_plain_1080p`` — stock x264 1080p, 1920x1088 coded with the usual
  8-pixel bottom window.
"""

from __future__ import annotations

import pytest

from avchdforge.sps import (
    SpsError,
    SpsGeometry,
    geometry_from_extradata,
    geometry_from_hex_dump,
    parse_hex_dump,
    parse_sps,
    window_restore,
)

from .conftest import DATA_DIR

SPS_DATA = DATA_DIR / "sps"

WINDOW_AVCC = "avcc_window_crop_1036.txt"
WINDOW_ANNEX_B = "annexb_window_crop_1036.txt"
PLAIN_AVCC = "avcc_plain_1080p.txt"
HARD_CROP_AVCC = "avcc_hard_crop_1036.txt"


def load_dump(name: str) -> str:
    return (SPS_DATA / name).read_text(encoding="utf-8")


def load_geometry(name: str) -> SpsGeometry:
    geometry = geometry_from_hex_dump(load_dump(name))
    assert geometry is not None, f"{name} did not parse"
    return geometry


# --------------------------------------------------------------------------
# the ffprobe hexdump
# --------------------------------------------------------------------------


def test_parse_hex_dump_ignores_address_and_ascii_columns():
    dump = "00000000: 0164 0029 ff          .d.)\n00000005: abcd        ..\n"
    assert parse_hex_dump(dump) == bytes.fromhex("01640029ffabcd")


def test_parse_hex_dump_tolerates_blank_and_banner_lines():
    dump = "extradata:\n\n00000000: 6764 0029 acd9 4078 0227 c70e c044 0000  gd.)..@x.'...D..\n"
    assert parse_hex_dump(dump) == bytes.fromhex("67640029acd940780227c70ec0440000")


def test_parse_hex_dump_returns_empty_for_garbage():
    assert parse_hex_dump("no hex here at all") == b""
    assert parse_hex_dump("") == b""


def test_real_avcc_dump_starts_with_the_configuration_record():
    blob = parse_hex_dump(load_dump(WINDOW_AVCC))
    assert blob[0] == 1  # configurationVersion
    assert blob[1] == 100  # AVCProfileIndication
    assert blob[4] & 0x03 == 3  # lengthSizeMinusOne: 4-byte NAL lengths
    assert blob[5] & 0x1F == 1  # numOfSequenceParameterSets


# --------------------------------------------------------------------------
# parsing the geometry
# --------------------------------------------------------------------------


def test_window_crop_avcc_geometry():
    geometry = load_geometry(WINDOW_AVCC)
    assert geometry.profile_idc == 100
    assert geometry.coded == (1920, 1088)
    # 13 crop units of 2 pixels on each side of a 4:2:0 progressive stream.
    assert (geometry.crop_top, geometry.crop_bottom) == (26, 26)
    assert geometry.display == (1920, 1036)
    assert geometry.crop_unit_y == 2


def test_window_crop_annexb_geometry_matches_avcc():
    assert load_geometry(WINDOW_ANNEX_B) == load_geometry(WINDOW_AVCC)


def test_plain_1080p_geometry():
    geometry = load_geometry(PLAIN_AVCC)
    assert geometry.coded == (1920, 1088)
    assert geometry.display == (1920, 1080)
    assert (geometry.crop_left, geometry.crop_right, geometry.crop_top) == (0, 0, 0)
    assert geometry.crop_bottom == 8


def test_hard_crop_geometry_of_the_real_remux():
    geometry = load_geometry(HARD_CROP_AVCC)
    assert geometry.coded == (1920, 1040)
    assert geometry.display == (1920, 1036)
    assert geometry.crop_bottom == 4  # 2 crop units, progressive 4:2:0


def test_geometry_from_extradata_returns_none_without_an_sps():
    assert geometry_from_extradata(b"") is None
    assert geometry_from_extradata(b"\x01\x02\x03") is None
    assert geometry_from_extradata(b"\x00\x00\x01\x68\xee\x3c\x80") is None  # PPS only


def test_parse_sps_rejects_non_sps_nals():
    with pytest.raises(SpsError, match="not a sequence parameter set"):
        parse_sps(b"\x68\xee\x3c\x80")


def test_parse_sps_rejects_truncated_data():
    blob = parse_hex_dump(load_dump(WINDOW_AVCC))
    nal = blob[8:34]  # the SPS inside the avcC record
    with pytest.raises(SpsError, match="truncated"):
        parse_sps(nal[:6])


def test_parse_sps_removes_emulation_prevention_bytes():
    # The stream "00 00 03 0f" stands for the RBSP "00 00 0f": the parser has to
    # drop the 0x03 or the geometry fields behind it shift by a byte.
    plain = bytes.fromhex("67640029acd940780227c70ec04400000fa40002ee003c60c658")
    escaped = plain[:16] + b"\x03" + plain[16:]
    assert b"\x00\x00\x03" in escaped
    assert parse_sps(escaped) == parse_sps(plain)


# --------------------------------------------------------------------------
# the repair decision
# --------------------------------------------------------------------------


def geometry(**overrides) -> SpsGeometry:
    values = {
        "profile_idc": 100,
        "level_idc": 41,
        "chroma_format_idc": 1,
        "frame_mbs_only": True,
        "coded_width": 1920,
        "coded_height": 1088,
        "crop_left": 0,
        "crop_right": 0,
        "crop_top": 0,
        "crop_bottom": 0,
    }
    values.update(overrides)
    return SpsGeometry(**values)


def test_window_crop_can_be_restored():
    restore = window_restore(geometry(crop_top=26, crop_bottom=26))
    assert restore is not None
    assert restore.target == (1920, 1080)
    assert restore.crop_bottom == 8  # coded height minus target height
    assert restore.exposed_rows == ((0, 26), (1062, 1080))


def test_bottom_only_crop_can_be_restored():
    restore = window_restore(geometry(crop_bottom=26))
    assert restore is not None
    assert restore.target == (1920, 1080)
    assert restore.exposed_rows == ((1062, 1080),)


def test_width_only_crop_can_be_restored_without_exposing_rows():
    restore = window_restore(geometry(crop_left=26, crop_bottom=8))
    assert restore is not None
    assert restore.target == (1920, 1080)
    assert restore.exposed_rows == ()


def test_hard_crop_has_no_repair():
    assert window_restore(geometry(coded_height=1040, crop_bottom=4)) is None


def test_width_mismatch_has_no_repair():
    assert window_restore(geometry(coded_width=1440)) is None


def test_crop_window_smaller_than_needed_has_no_repair():
    # Opening the window would cut off 4 rows that are currently visible.
    assert window_restore(geometry(crop_bottom=4)) is None


def test_already_correct_display_needs_no_repair():
    assert window_restore(geometry(crop_bottom=8)) is None
    assert window_restore(geometry(crop_left=0, crop_right=0, crop_top=0, crop_bottom=8)) is None


def test_720p_crop_can_be_restored():
    restore = window_restore(geometry(coded_width=1280, coded_height=720, crop_top=8))
    assert restore is not None
    assert restore.target == (1280, 720)
    assert restore.crop_bottom == 0
    assert restore.exposed_rows == ((0, 8),)


def test_interlaced_crop_units_are_four_pixels():
    # frame_mbs_only_flag = 0: CropUnitY is 4 for 4:2:0, so a 1088 -> 1080
    # repair is two crop units and still lands on the grid.
    restore = window_restore(
        geometry(frame_mbs_only=False, crop_top=28, crop_bottom=28),
    )
    assert restore is not None
    assert restore.crop_bottom == 8
    assert restore.exposed_rows == ((0, 28), (1060, 1080))
    assert geometry(frame_mbs_only=False).crop_unit_y == 4


def test_interlaced_slack_not_divisible_by_four_is_refused():
    # 1088 - 1086 = 2 is not expressible in interlaced crop units of four.
    assert window_restore(geometry(frame_mbs_only=False), targets={(1920, 1086)}) is None


def test_monochrome_crop_units_are_one_pixel():
    monochrome = geometry(chroma_format_idc=0)
    assert monochrome.crop_unit_x == 1
    assert monochrome.crop_unit_y == 1
    # Monochrome interlaced is still two pixels vertically.
    assert geometry(chroma_format_idc=0, frame_mbs_only=False).crop_unit_y == 2
