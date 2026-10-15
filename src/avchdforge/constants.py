"""Constants and lookup tables shared across avchd-forge.

Everything here is pure data: no Qt, no I/O. Keep the AVCHD-relevant limits in
one place so validation, planning and the UI agree on them.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

#: Container extensions accepted as input (BDRemux-style sources).
ALLOWED_INPUT_SUFFIXES: Final[tuple[str, ...]] = (".mkv", ".ts", ".m2ts", ".mpg", ".mpeg")

#: Recognised in the file dialog and drag-and-drop.
INPUT_FILE_FILTER: Final[str] = (
    "Video sources (*.mkv *.ts *.m2ts *.mpg *.mpeg);;All files (*)"
)

# --------------------------------------------------------------------------
# Video / AVCHD limits
# --------------------------------------------------------------------------

#: ffprobe codec_name values accepted for video. Anything else is a hard error.
AVC_CODEC_NAMES: Final[frozenset[str]] = frozenset({"h264"})

#: Only these frame sizes are AVCHD-legal.
ALLOWED_RESOLUTIONS: Final[frozenset[tuple[int, int]]] = frozenset(
    {(1920, 1080), (1280, 720)}
)

#: Highest H.264 level that AVCHD/PS3 accepts cleanly; above this we only warn.
MAX_H264_LEVEL: Final[float] = 4.1

#: Frame-rate ceilings per frame height (AVCHD: 1080i/p <= 29.97, 720p <= 59.94).
MAX_FPS_BY_HEIGHT: Final[dict[int, float]] = {1080: 30.0, 720: 60.0}

#: Video bitrate above which we warn: AVCHD caps at 17 Mbps, PS3 copes with ~24.
VIDEO_BITRATE_WARN_BPS: Final[int] = 24_000_000

#: `field_order` values that mean the source is progressive (no warning).
PROGRESSIVE_FIELD_ORDERS: Final[frozenset[str]] = frozenset({"progressive", "unknown", ""})

# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

#: LPCM family: ffprobe reports these as `pcm_*`.
LPCM_CODEC_PREFIX: Final[str] = "pcm_"

#: Codecs tsMuxeR can take straight from the source, in AVCHD-legal form.
PASSTHROUGH_AUDIO_CODECS: Final[frozenset[str]] = frozenset({"ac3", "dts"})

#: Codecs that pass through but are not strictly AVCHD-legal (PS3 plays them).
TOLERATED_AUDIO_CODECS: Final[frozenset[str]] = frozenset({"aac"})

#: HD audio that must lose its extension layer before muxing.
DTS_HD_PROFILE_MARKERS: Final[tuple[str, ...]] = ("HD", "X")  # DTS-HD MA/HRA, DTS:X

#: LPCM constraints inside AVCHD.
AVCHD_LPCM_SAMPLE_RATE: Final[int] = 48_000
AVCHD_LPCM_MAX_CHANNELS: Final[int] = 6

#: Codec tags used by tsMuxeR meta files.
TSMUXER_AUDIO_TAGS: Final[dict[str, str]] = {
    "ac3": "A_AC3",
    "eac3": "A_AC3",  # only after eac3_core produced an AC-3 stream
    "dts": "A_DTS",
    "aac": "A_AAC",
    "lpcm": "A_LPCM",
}

#: Human-readable codec names for the UI.
AUDIO_CODEC_LABELS: Final[dict[str, str]] = {
    "ac3": "AC-3",
    "eac3": "E-AC-3",
    "dts": "DTS",
    "truehd": "TrueHD",
    "mlp": "MLP",
    "aac": "AAC",
    "mp3": "MP3",
    "flac": "FLAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
}

# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------

#: ffprobe codec names for PGS (Blu-ray bitmap subtitles) - copied 1:1.
PGS_SUBTITLE_CODECS: Final[frozenset[str]] = frozenset({"hdmv_pgs_subtitle", "pgssub"})

#: Text subtitle codecs we can convert to PGS.
TEXT_SUBTITLE_CODECS: Final[frozenset[str]] = frozenset(
    {"subrip", "srt", "ass", "ssa", "mov_text", "text", "webvtt"}
)

#: Bitmap subtitle formats that are neither PGS nor convertible: reject them.
UNSUPPORTED_SUBTITLE_CODECS: Final[frozenset[str]] = frozenset(
    {"dvd_subtitle", "dvb_subtitle", "xsub", "hdmv_text_subtitle"}
)

#: subtitle codec names used in tsMuxeR meta files
TSMUXER_PGS_TAG: Final[str] = "S_HDMV/PGS"
TSMUXER_SRT_TAG: Final[str] = "S_TEXT/UTF8"

#: Defaults for tsMuxeR's SRT -> PGS renderer (overridable in Preferences).
DEFAULT_FONT_NAME: Final[str] = "DejaVu Sans"
DEFAULT_FONT_SIZE: Final[int] = 65
DEFAULT_BOTTOM_OFFSET: Final[int] = 24
DEFAULT_FONT_COLOR: Final[str] = "0xEBFFFFFF"

# --------------------------------------------------------------------------
# Output / FAT32
# --------------------------------------------------------------------------

#: FAT32 cannot hold a file of 4 GiB or more; split comfortably below that.
FAT32_MAX_FILE_BYTES: Final[int] = 4 * 1024**3 - 1
DEFAULT_SPLIT_SIZE_MB: Final[int] = 4096

#: Filesystems that are plain FAT32 (or compatible enough for our purpose).
FAT32_FILESYSTEMS: Final[frozenset[str]] = frozenset({"vfat", "msdos", "fat", "fat32"})

#: Filesystems that are fine but bigger-file-capable, so splitting is optional.
LARGE_FILE_FILESYSTEMS: Final[frozenset[str]] = frozenset({"exfat", "ntfs", "ext4", "ext3"})

#: Relative paths that must exist inside a successful AVCHD output.
AVCHD_REQUIRED_PATHS: Final[tuple[str, ...]] = (
    "BDMV/index.bdmv",
    "BDMV/MovieObject.bdmv",
)

# --------------------------------------------------------------------------
# Process / progress
# --------------------------------------------------------------------------

#: Fraction of a mux during which ffmpeg/tsMuxeR progress is considered reliable.
PROGRESS_PHASE_WEIGHTS: Final[dict[str, float]] = {
    "probe": 0.03,
    "extract": 0.35,
    "subtitles": 0.07,
    "mux": 0.50,
    "verify": 0.05,
}

# --------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------

#: tsMuxeR insists on exactly three letters ("und" when unknown).
DEFAULT_LANGUAGE: Final[str] = "und"

#: Minimal ISO 639-1 -> 639-2/B map for normalising ffprobe metadata.
ISO639_1_TO_2: Final[dict[str, str]] = {
    "ar": "ara",
    "bg": "bul",
    "ca": "cat",
    "cs": "cze",
    "da": "dan",
    "de": "ger",
    "el": "gre",
    "en": "eng",
    "es": "spa",
    "et": "est",
    "fa": "per",
    "fi": "fin",
    "fr": "fre",
    "he": "heb",
    "hi": "hin",
    "hr": "hrv",
    "hu": "hun",
    "id": "ind",
    "is": "ice",
    "it": "ita",
    "ja": "jpn",
    "ko": "kor",
    "lt": "lit",
    "lv": "lav",
    "ms": "may",
    "nl": "dut",
    "no": "nor",
    "pl": "pol",
    "pt": "por",
    "ro": "rum",
    "ru": "rus",
    "sk": "slo",
    "sl": "slv",
    "sr": "srp",
    "sv": "swe",
    "th": "tha",
    "tr": "tur",
    "uk": "ukr",
    "vi": "vie",
    "zh": "chi",
}

#: 3-letter spot: things ffprobe emits that tsMuxeR does not accept as-is.
LANGUAGE_ALIASES: Final[dict[str, str]] = {
    "deu": "ger",
    "fra": "fre",
    "ces": "cze",
    "nld": "dut",
    "ron": "rum",
    "ell": "gre",
    "fas": "per",
    "zho": "chi",
    "slk": "slo",
    "hye": "arm",
    "mya": "bur",
    "unicode": DEFAULT_LANGUAGE,
}
