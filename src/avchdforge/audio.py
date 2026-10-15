"""What has to happen to an audio track before it can go into an AVCHD.

AVCHD allows AC-3, DTS and LPCM. Sources may carry flavours that need their
extension layer removed first:

* **DTS-HD MA / HRA / DTS:X** → keep the DTS core (``-bsf:a dca_core``).
* **E-AC-3 (Dolby Digital Plus / Atmos)** → keep the AC-3 core
  (``-bsf:a eac3_core``).
* **TrueHD** → there is no FFmpeg bitstream filter that yields an AC-3 core
  (``truehd_core`` only drops Atmos data), so we use the AC-3 stream that the
  container exposes next to the TrueHD track when it exists, and refuse the track
  with an explicit reason when it does not.

Nothing here re-encodes: every action is either a stream copy or a stream copy
through a lossless bitstream filter, and :mod:`avchdforge.prep` verifies the
resulting codec with ffprobe before it is handed to tsMuxeR.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .constants import AUDIO_CODEC_LABELS
from .probe import AudioTrack, MediaInfo


class AudioAction(str, Enum):
    """How a selected audio track reaches the muxer."""

    PASSTHROUGH = "passthrough"
    DTS_CORE = "extract-dts-core"
    EAC3_CORE = "extract-eac3-core"
    AC3_CORE_DEMUX = "demux-ac3-core"
    UNSUPPORTED = "unsupported"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value

    @property
    def needs_ffmpeg(self) -> bool:
        return self in {AudioAction.DTS_CORE, AudioAction.EAC3_CORE, AudioAction.AC3_CORE_DEMUX}


@dataclass(frozen=True, slots=True)
class AudioDecision:
    """The verdict for one audio track, plus everything the plan needs."""

    source_index: int
    action: AudioAction
    codec: str
    resulting_codec: str
    mux_codec_tag: str | None = None
    #: ffmpeg ``-bsf:a`` value (``dca_core``/``eac3_core``) or ``None``.
    bsf: str | None = None
    #: ffmpeg ``-f`` value for the extracted elementary stream.
    container: str | None = None
    output_suffix: str | None = None
    #: Codec the extracted file must report (ffprobe verification).
    expected_codec: str | None = None
    #: Short user-facing explanation, shown as a tooltip / log line.
    note: str = ""

    @property
    def supported(self) -> bool:
        return self.action is not AudioAction.UNSUPPORTED

    @property
    def needs_extraction(self) -> bool:
        return self.action.needs_ffmpeg

    @property
    def is_passthrough(self) -> bool:
        return self.action is AudioAction.PASSTHROUGH

    def describe(self) -> str:
        if self.action is AudioAction.PASSTHROUGH:
            return f"copy ({self.resulting_label})"
        if self.action is AudioAction.UNSUPPORTED:
            return self.note or "unsupported"
        names = {
            AudioAction.DTS_CORE: "DTS core",
            AudioAction.EAC3_CORE: "AC-3 core",
            AudioAction.AC3_CORE_DEMUX: "embedded AC-3 core",
        }
        return f"{names[self.action]} → {self.resulting_label}"

    @property
    def resulting_label(self) -> str:
        """Display name of the codec that ends up in the AVCHD structure."""
        return AUDIO_CODEC_LABELS.get(self.resulting_codec, self.resulting_codec.upper())


def embedded_ac3_core_index(media: MediaInfo, track: AudioTrack) -> int | None:
    """Index of the AC-3 stream that belongs to ``track`` (TrueHD core), if any.

    Dolby TrueHD on Blu-ray normally carries an AC-3 core; FFmpeg exposes it as a
    separate ``ac3`` stream following the TrueHD stream. We only trust it when the
    channel count matches, and we never look backwards in the stream order.
    """
    if track.codec not in {"truehd", "mlp"}:
        return None
    candidates = [
        other
        for other in media.audio
        if other.codec == "ac3"
        and other.index > track.index
        and (track.channels is None or other.channels == track.channels)
    ]
    if not candidates:
        return None
    return min(candidate.index for candidate in candidates)


def decide_audio(media: MediaInfo, track: AudioTrack) -> AudioDecision:
    """Decide how ``track`` is muxed. Pure function of the probed media."""
    codec = track.codec

    if codec in {"truehd", "mlp"}:
        core_index = embedded_ac3_core_index(media, track)
        if core_index is None:
            return AudioDecision(
                source_index=track.index,
                action=AudioAction.UNSUPPORTED,
                codec=codec,
                resulting_codec=codec,
                note=(
                    "TrueHD without an embedded AC-3 core cannot be made "
                    "AVCHD-compatible without re-encoding, which avchd-forge "
                    "never does. Pick another audio track."
                ),
            )
        return AudioDecision(
            source_index=core_index,
            action=AudioAction.AC3_CORE_DEMUX,
            codec=codec,
            resulting_codec="ac3",
            mux_codec_tag="A_AC3",
            container="ac3",
            output_suffix=".ac3",
            expected_codec="ac3",
            note=f"TrueHD: using the embedded AC-3 core (stream {core_index})",
        )

    if codec == "eac3":
        return AudioDecision(
            source_index=track.index,
            action=AudioAction.EAC3_CORE,
            codec=codec,
            resulting_codec="ac3",
            mux_codec_tag="A_AC3",
            bsf="eac3_core",
            container="ac3",
            output_suffix=".ac3",
            expected_codec="ac3",
            note="E-AC-3: extracting the AC-3 core (eac3_core)",
        )

    if codec == "dts":
        if track.is_hd:
            return AudioDecision(
                source_index=track.index,
                action=AudioAction.DTS_CORE,
                codec=codec,
                resulting_codec="dts",
                mux_codec_tag="A_DTS",
                bsf="dca_core",
                container="dts",
                output_suffix=".dts",
                expected_codec="dts",
                note=f"DTS-HD ({track.profile}): extracting the DTS core (dca_core)",
            )
        return AudioDecision(
            source_index=track.index,
            action=AudioAction.PASSTHROUGH,
            codec=codec,
            resulting_codec="dts",
            mux_codec_tag="A_DTS",
            note="DTS core: copied unchanged",
        )

    if codec == "ac3":
        return AudioDecision(
            source_index=track.index,
            action=AudioAction.PASSTHROUGH,
            codec=codec,
            resulting_codec="ac3",
            mux_codec_tag="A_AC3",
            note="AC-3: copied unchanged",
        )

    if track.is_lpcm:
        return AudioDecision(
            source_index=track.index,
            action=AudioAction.PASSTHROUGH,
            codec=codec,
            resulting_codec="lpcm",
            mux_codec_tag="A_LPCM",
            note="LPCM: copied unchanged",
        )

    if codec == "aac":
        return AudioDecision(
            source_index=track.index,
            action=AudioAction.PASSTHROUGH,
            codec=codec,
            resulting_codec="aac",
            mux_codec_tag="A_AAC",
            note="AAC is not part of the AVCHD specification (PS3 usually plays it)",
        )

    return AudioDecision(
        source_index=track.index,
        action=AudioAction.UNSUPPORTED,
        codec=codec,
        resulting_codec=codec,
        note=f"{codec.upper()} audio cannot be carried in an AVCHD structure",
    )


def decide_audio_all(media: MediaInfo) -> dict[int, AudioDecision]:
    """Decisions for every audio stream, keyed by ffprobe stream index."""
    return {track.index: decide_audio(media, track) for track in media.audio}


__all__ = [
    "AudioAction",
    "AudioDecision",
    "decide_audio",
    "decide_audio_all",
    "embedded_ac3_core_index",
]
