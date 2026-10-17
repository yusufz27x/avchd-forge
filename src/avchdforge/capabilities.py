"""What the locally installed FFmpeg can actually do.

The brief asks for ``-c:s pgssub`` when converting SRT to PGS, but stock FFmpeg
ships no PGS *encoder* (verified: 7.1.5 has the ``pgssub`` decoder and a ``sup``
muxer only). Rather than hard-coding that assumption, the plan asks
:class:`FfmpegCapabilities` and takes the ``sup`` route only when a build really
advertises an encoder.

:class:`Toolchain` is what the rest of the app passes around: tool paths plus
capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FfmpegCapabilities:
    """Capabilities parsed from ``ffmpeg -encoders``/``-bsfs``/``-muxers``."""

    #: True when a PGS encoder exists (``ffmpeg -encoders`` lists ``pgssub``).
    has_pgs_encoder: bool = False
    #: Subtitle encoders by ffmpeg name (``srt``, ``ass``, ``pgssub``, …).
    subtitle_encoders: frozenset[str] = frozenset()
    #: Bitstream filters by name (``dca_core``, ``eac3_core``, …).
    bitstream_filters: frozenset[str] = frozenset()
    #: Muxers by name, used to sanity-check raw output formats.
    muxers: frozenset[str] = frozenset()

    def has_bsf(self, name: str) -> bool:
        return name in self.bitstream_filters

    @property
    def is_empty(self) -> bool:
        """True for a default-constructed instance: nothing was probed yet.

        ``plan_for_source`` uses this to decide whether it has to ask the
        installed ffmpeg itself, instead of mistaking "nobody checked" for
        "this build cannot do it".
        """
        return not (
            self.has_pgs_encoder
            or self.subtitle_encoders
            or self.bitstream_filters
            or self.muxers
        )

    def has_subtitle_encoder(self, name: str) -> bool:
        return name in self.subtitle_encoders

    def has_muxer(self, name: str) -> bool:
        return name in self.muxers

    @property
    def dts_core_supported(self) -> bool:
        return self.has_bsf("dca_core")

    @property
    def eac3_core_supported(self) -> bool:
        return self.has_bsf("eac3_core")


@dataclass(frozen=True, slots=True)
class Toolchain:
    """Paths to the external tools plus what they can do."""

    tsmuxer: Path
    ffmpeg: Path
    ffprobe: Path
    capabilities: FfmpegCapabilities = field(default_factory=FfmpegCapabilities)

    def with_capabilities(self, capabilities: FfmpegCapabilities) -> Toolchain:
        return Toolchain(
            tsmuxer=self.tsmuxer,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            capabilities=capabilities,
        )


__all__ = ["FfmpegCapabilities", "Toolchain"]
