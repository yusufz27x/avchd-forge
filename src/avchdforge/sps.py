"""H.264 SPS geometry: the coded frame, the conformance window, and its repair.

ffprobe reports the *display* size of an AVC stream, which for container formats
is not the whole truth: the coded picture is always a multiple of 16 lines and a
stream can hide up to three of those lines per side behind the SPS conformance
window. Scene re-encodes use that window to present 1920x1036 while the coded
frame still carries the bars — and because the hidden lines are really there,
zeroing the window shows them again without touching a single coded pixel
(``docs/CONTEXT.md`` §2.12). That is the only lossless way a cropped 1080p
source can become the exact 1920x1080 AVCHD wants.

This module is pure: it turns ffprobe's ``-show_data`` extradata hexdump into an
:class:`SpsGeometry` and decides whether a window repair can reach a legal
target. It never runs a tool — ``probe.py`` does that and stores the geometry on
the :class:`~avchdforge.probe.VideoTrack`.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace

from .constants import ALLOWED_RESOLUTIONS

#: NAL unit type of a sequence parameter set (H.264 Table 7-1).
SPS_NAL_TYPE = 7

#: Profiles whose SPS carries chroma format, bit depths and scaling lists
#: (H.264 7.3.2.1.1; the list includes the constrained variants).
_HIGH_PROFILES = frozenset(
    {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
)

#: Prefix of an Annex B NAL unit. ``00 00 01`` also matches a 4-byte start code.
_START_CODE = b"\x00\x00\x01"


class SpsError(ValueError):
    """The bytes are not a parseable H.264 sequence parameter set."""


@dataclass(frozen=True, slots=True)
class SpsGeometry:
    """The size fields of one SPS, in pixels.

    ``crop_*`` are the conformance-window offsets converted from the crop units
    H.264 stores them in (which depend on the chroma format and on whether the
    stream is interlaced), so they can be compared with heights and handed to
    ``h264_metadata`` directly.
    """

    profile_idc: int
    level_idc: int
    chroma_format_idc: int
    frame_mbs_only: bool
    coded_width: int
    coded_height: int
    crop_left: int
    crop_right: int
    crop_top: int
    crop_bottom: int

    @property
    def crop_unit_x(self) -> int:
        """Horizontal crop unit (``SubWidthC``), 1 for monochrome and 4:4:4."""
        return 1 if self.chroma_format_idc in {0, 3} else 2

    @property
    def crop_unit_y(self) -> int:
        """Vertical crop unit (``SubHeightC * (2 - frame_mbs_only_flag)``)."""
        sub_height = 1 if self.chroma_format_idc in {0, 3} else 2
        return sub_height * (1 if self.frame_mbs_only else 2)

    @property
    def display_width(self) -> int:
        return self.coded_width - (self.crop_left + self.crop_right)

    @property
    def display_height(self) -> int:
        return self.coded_height - (self.crop_top + self.crop_bottom)

    @property
    def has_crop(self) -> bool:
        return bool(self.crop_left or self.crop_right or self.crop_top or self.crop_bottom)

    @property
    def display(self) -> tuple[int, int]:
        return (self.display_width, self.display_height)

    @property
    def coded(self) -> tuple[int, int]:
        return (self.coded_width, self.coded_height)

    def describe(self) -> str:
        if not self.has_crop:
            return f"{self.coded_width}x{self.coded_height} (no window)"
        return (
            f"{self.coded_width}x{self.coded_height} coded, "
            f"{self.display_width}x{self.display_height} shown "
            f"(crop {self.crop_left}/{self.crop_right}/{self.crop_top}/{self.crop_bottom})"
        )


@dataclass(frozen=True, slots=True)
class WindowRestore:
    """A conformance-window rewrite that shows coded rows hidden by the window.

    ``crop_bottom`` is what the repaired stream must keep hiding at the bottom
    (the difference between the coded height and the legal target height);
    everything else is opened up. ``exposed_rows`` lists the half-open row
    ranges of the repaired frame that were invisible before — useful for
    logging, and for checking that the rows really are bars and not picture.
    """

    target_width: int
    target_height: int
    crop_bottom: int
    exposed_rows: tuple[tuple[int, int], ...] = ()

    @property
    def target(self) -> tuple[int, int]:
        return (self.target_width, self.target_height)

    def __str__(self) -> str:
        return f"{self.target_width}x{self.target_height}"


# --------------------------------------------------------------------------
# bitstream reading
# --------------------------------------------------------------------------


class _Bits:
    """MSB-first bit reader over an RBSP."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) * 8 - self._pos

    def u(self, count: int) -> int:
        if count < 0 or count > self.remaining:
            raise SpsError("truncated SPS")
        value = 0
        for _ in range(count):
            byte = self._data[self._pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return value

    def ue(self) -> int:
        """Unsigned exp-Golomb code (0 -> ``1``, 1 -> ``010``, …)."""
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise SpsError("invalid exp-Golomb code")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        """Signed exp-Golomb code, as the scaling-list deltas use."""
        code = self.ue()
        if code % 2:
            return (code + 1) // 2
        return -(code // 2)


def _unescape(data: bytes) -> bytes:
    """Drop the emulation-prevention bytes (``00 00 03`` -> ``00 00``).

    A skipped ``03`` does not end the zero run: the bytes it was inserted
    between are still ``00 00``, so a following byte must be judged against
    them, not against the ``03``.
    """
    if b"\x00\x00\x03" not in data:
        return data
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _skip_scaling_list(bits: _Bits, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            next_scale = (last_scale + bits.se() + 256) % 256
        if next_scale != 0:
            last_scale = next_scale


def parse_sps(nal: bytes) -> SpsGeometry:
    """Parse the geometry fields of one SPS NAL unit.

    ``nal`` is the NAL unit *including* its header byte, in the escaped form
    containers and transport streams carry (emulation prevention is removed
    here). Everything after the conformance window — VUI included — is skipped;
    the geometry is complete without it.
    """
    if not nal:
        raise SpsError("empty NAL unit")
    if (nal[0] & 0x1F) != SPS_NAL_TYPE:
        raise SpsError(f"not a sequence parameter set (NAL type {nal[0] & 0x1F})")

    bits = _Bits(_unescape(nal[1:]))
    profile_idc = bits.u(8)
    bits.u(8)  # constraint_set flags + reserved bits
    level_idc = bits.u(8)
    bits.ue()  # seq_parameter_set_id

    chroma_format_idc = 1
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = bits.ue()
        if chroma_format_idc == 3:
            bits.u(1)  # separate_colour_plane_flag
        bits.ue()  # bit_depth_luma_minus8
        bits.ue()  # bit_depth_chroma_minus8
        bits.u(1)  # qpprime_y_zero_transform_bypass_flag
        if bits.u(1):  # seq_scaling_matrix_present_flag
            for index in range(8 if chroma_format_idc != 3 else 12):
                if bits.u(1):  # seq_scaling_list_present_flag
                    _skip_scaling_list(bits, 16 if index < 6 else 64)

    bits.ue()  # log2_max_frame_num_minus4
    poc_type = bits.ue()
    if poc_type == 0:
        bits.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif poc_type == 1:
        bits.u(1)  # delta_pic_order_always_zero_flag
        bits.se()  # offset_for_non_ref_pic
        bits.se()  # offset_for_top_to_bottom_field
        for _ in range(bits.ue()):  # num_ref_frames_in_pic_order_cnt_cycle
            bits.se()
    bits.ue()  # max_num_ref_frames
    bits.u(1)  # gaps_in_frame_num_value_allowed_flag
    coded_width = (bits.ue() + 1) * 16
    coded_height = (bits.ue() + 1) * 16
    frame_mbs_only = bool(bits.u(1))
    if not frame_mbs_only:
        bits.u(1)  # mb_adaptive_frame_field_flag
    bits.u(1)  # direct_8x8_inference_flag
    crop = (0, 0, 0, 0)
    if bits.u(1):  # frame_cropping_flag
        crop = (bits.ue(), bits.ue(), bits.ue(), bits.ue())

    geometry = SpsGeometry(
        profile_idc=profile_idc,
        level_idc=level_idc,
        chroma_format_idc=chroma_format_idc,
        frame_mbs_only=frame_mbs_only,
        coded_width=coded_width,
        coded_height=coded_height,
        crop_left=0,
        crop_right=0,
        crop_top=0,
        crop_bottom=0,
    )
    # Crop offsets are stored in units; normalise to pixels once, so every
    # caller (validation, the repair, h264_metadata) speaks pixels.
    unit_x = geometry.crop_unit_x
    unit_y = geometry.crop_unit_y
    return replace(
        geometry,
        crop_left=crop[0] * unit_x,
        crop_right=crop[1] * unit_x,
        crop_top=crop[2] * unit_y,
        crop_bottom=crop[3] * unit_y,
    )


# --------------------------------------------------------------------------
# ffprobe extradata
# --------------------------------------------------------------------------


def parse_hex_dump(dump: str) -> bytes:
    """Decode the hexdump ``ffprobe -show_data`` prints for ``extradata``.

    Each line is ``<address>: <hex column>  <ASCII column>``; only complete
    even-length hex lines are accepted, so the address and ASCII columns (and
    any banner text) are ignored rather than parsed as bytes.
    """
    out = bytearray()
    for line in dump.strip().splitlines():
        if ":" not in line:
            continue
        body = line.split(":", 1)[1].split("  ", 1)[0]
        digits = "".join(body.split())
        if (
            digits
            and len(digits) % 2 == 0
            and all(char in "0123456789abcdefABCDEF" for char in digits)
        ):
            out.extend(bytes.fromhex(digits))
    return bytes(out)


def _find_annex_b_sps(data: bytes) -> bytes | None:
    position = data.find(_START_CODE)
    while position >= 0:
        start = position + 3
        if start < len(data) and (data[start] & 0x1F) == SPS_NAL_TYPE:
            return data[start:]
        position = data.find(_START_CODE, position + 1)
    return None


def _find_avcc_sps(data: bytes) -> bytes | None:
    """The first SPS of an ``AVCDecoderConfigurationRecord`` (MKV/MP4)."""
    num_sps = data[5] & 0x1F
    position = 6
    for _ in range(num_sps):
        if position + 2 > len(data):
            return None
        length = int.from_bytes(data[position : position + 2], "big")
        position += 2
        if length <= 0 or position + length > len(data):
            return None
        if (data[position] & 0x1F) == SPS_NAL_TYPE:
            return data[position : position + length]
        position += length
    return None


def find_sps(extradata: bytes) -> bytes | None:
    """The first SPS NAL unit in ffprobe extradata, Annex B or AVCC style."""
    if not extradata:
        return None
    if extradata[:3] == _START_CODE:
        return _find_annex_b_sps(extradata)
    if len(extradata) >= 7 and extradata[0] == 1:
        return _find_avcc_sps(extradata)
    # Not a standard configuration record: scan anyway, start codes can only
    # appear in Annex B data.
    return _find_annex_b_sps(extradata)


def geometry_from_extradata(extradata: bytes) -> SpsGeometry | None:
    """Parse the SPS geometry from extradata, or ``None`` when unreadable."""
    nal = find_sps(extradata)
    if nal is None:
        return None
    try:
        return parse_sps(nal)
    except SpsError:
        return None


def geometry_from_hex_dump(dump: str) -> SpsGeometry | None:
    """Convenience for the ``extradata`` string ffprobe's JSON carries."""
    return geometry_from_extradata(parse_hex_dump(dump))


# --------------------------------------------------------------------------
# the repair
# --------------------------------------------------------------------------


def window_restore(
    geometry: SpsGeometry,
    targets: Collection[tuple[int, int]] = ALLOWED_RESOLUTIONS,
) -> WindowRestore | None:
    """How to make ``geometry`` display exactly one of ``targets`` losslessly.

    Possible only when the coded frame is as wide as the target and at least as
    tall, and the rows the source hides at the *bottom* cover the difference
    between the coded height and the target. Zeroing the window then shows rows
    that are already coded — nothing is scaled and nothing is invented. When the
    coded frame is too short (a hard crop: the bars were never encoded), there is
    no repair and ``None`` is returned.
    """
    for target_width, target_height in sorted(targets, key=lambda size: -size[1]):
        if geometry.coded_width != target_width:
            continue
        slack = geometry.coded_height - target_height
        if slack < 0 or slack % geometry.crop_unit_y:
            continue
        # The visible area must survive the rewrite: if the source already hides
        # fewer lines than the slack, opening the window would *cut* visible rows.
        if geometry.crop_bottom < slack:
            continue
        if (geometry.display_width, geometry.display_height) == (target_width, target_height):
            continue  # already exactly the target; nothing to repair

        visible_top = geometry.crop_top
        visible_bottom = geometry.coded_height - geometry.crop_bottom
        exposed: list[tuple[int, int]] = []
        if visible_top > 0:
            exposed.append((0, min(visible_top, target_height)))
        if visible_bottom < target_height:
            exposed.append((visible_bottom, target_height))
        return WindowRestore(
            target_width=target_width,
            target_height=target_height,
            crop_bottom=slack,
            exposed_rows=tuple(exposed),
        )
    return None


def window_restore_for(
    geometries: Iterable[SpsGeometry], targets: Collection[tuple[int, int]] = ALLOWED_RESOLUTIONS
) -> WindowRestore | None:
    """The first repair any of ``geometries`` allows (streams can be re-signalled)."""
    for geometry in geometries:
        if (restore := window_restore(geometry, targets)) is not None:
            return restore
    return None


__all__ = [
    "SPS_NAL_TYPE",
    "SpsError",
    "SpsGeometry",
    "WindowRestore",
    "find_sps",
    "geometry_from_extradata",
    "geometry_from_hex_dump",
    "parse_hex_dump",
    "parse_sps",
    "window_restore",
    "window_restore_for",
]
