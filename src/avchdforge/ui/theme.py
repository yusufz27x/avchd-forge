"""Palette-aware colours for the parts of the UI Qt cannot colour itself.

Ordinary widgets follow the application palette. Three things do not:

* style sheets (``color: #…``) used for the status badges and the Preferences
  tool column,
* table-item brushes, where a fixed pale red/orange row background meets the
  dark palette's near-white text, and
* rich-text spans in the video summary, where the level/bitrate/issue colours
  are written into the HTML.

Those were all chosen against a light desktop. On a dark one the dark red and
brown text vanishes into the window, and the pale warning rows leave light text
nearly invisible — the exact problem this module exists to prevent.

The light palette keeps the established colours; the dark variants are brighter
versions of the same hues, and :func:`_ensure_contrast` nudges any variant that
is still weak on an unusual theme (a mid-grey window, a custom high-contrast
scheme) until it reaches :data:`MIN_CONTRAST`. All colours are derived from the
widget's own palette, so a theme change at runtime is handled by the widgets'
``changeEvent`` hooks.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtGui

#: WCAG AA for normal text. Used to validate the dark variants and the tints.
MIN_CONTRAST = 4.5

#: The light palette's colours, unchanged from the original design.
LIGHT_ERROR_TEXT = "#c0392b"
LIGHT_WARNING_TEXT = "#9a6b00"
LIGHT_OK_TEXT = "#1e8449"
LIGHT_MUTED_TEXT = "#6c757d"
LIGHT_ERROR_BACKGROUND = QtGui.QColor(255, 232, 232)
LIGHT_WARNING_BACKGROUND = QtGui.QColor(255, 246, 224)

#: Bright equivalents for dark palettes: readable without being neon.
DARK_ERROR_TEXT = "#ff7b72"
DARK_WARNING_TEXT = "#e3b341"
DARK_OK_TEXT = "#56d364"
DARK_MUTED_TEXT = "#9aa4ae"

#: How strongly a dark row background is tinted with its severity hue. Subtle
#: enough that the palette's own text stays readable, distinct enough that an
#: error and a warning do not look alike.
DARK_ERROR_TINT = 0.18
DARK_WARNING_TINT = 0.16


@dataclass(frozen=True, slots=True)
class SeverityColors:
    """Every semantic colour one palette needs, as stylesheet strings/brushes."""

    error: str
    warning: str
    ok: str
    muted: str
    error_background: QtGui.QColor
    warning_background: QtGui.QColor


#: The light default, also the fallback for purely rendered code (``video_html``
#: called without a palette in tests).
LIGHT = SeverityColors(
    error=LIGHT_ERROR_TEXT,
    warning=LIGHT_WARNING_TEXT,
    ok=LIGHT_OK_TEXT,
    muted=LIGHT_MUTED_TEXT,
    error_background=LIGHT_ERROR_BACKGROUND,
    warning_background=LIGHT_WARNING_BACKGROUND,
)


def _resolve(palette: QtGui.QPalette | None) -> QtGui.QPalette:
    if palette is not None:
        return palette
    app = QtGui.QGuiApplication.instance()
    return app.palette() if app is not None else QtGui.QPalette()


def is_dark(palette: QtGui.QPalette | None = None) -> bool:
    """Whether ``palette`` is a dark scheme.

    ``Window`` is the surface every label sits on; its lightness is the same
    signal the platform themes themselves use. (``Mid`` is deliberately not
    consulted: dark themes set it to the window colour, which is why using it
    for text made labels disappear.)
    """
    window = _resolve(palette).color(QtGui.QPalette.ColorRole.Window)
    return window.lightness() < 128


def contrast_ratio(first: QtGui.QColor, second: QtGui.QColor) -> float:
    """WCAG relative-contrast ratio between two colours (1.0 … 21.0)."""
    lighter = max(_luminance(first), _luminance(second))
    darker = min(_luminance(first), _luminance(second))
    return (lighter + 0.05) / (darker + 0.05)


def severity_colors(palette: QtGui.QPalette | None = None) -> SeverityColors:
    """The semantic colours for ``palette`` (light or dark)."""
    resolved = _resolve(palette)
    if not is_dark(resolved):
        return LIGHT

    window = resolved.color(QtGui.QPalette.ColorRole.Window)
    base = resolved.color(QtGui.QPalette.ColorRole.Base)
    return SeverityColors(
        error=_readable(DARK_ERROR_TEXT, window),
        warning=_readable(DARK_WARNING_TEXT, window),
        ok=_readable(DARK_OK_TEXT, window),
        muted=_readable(DARK_MUTED_TEXT, window),
        error_background=_blend(base, QtGui.QColor(DARK_ERROR_TEXT), DARK_ERROR_TINT),
        warning_background=_blend(
            base, QtGui.QColor(DARK_WARNING_TEXT), DARK_WARNING_TINT
        ),
    )


def badge_color(is_fat32: bool, palette: QtGui.QPalette | None = None) -> str:
    """Green for a FAT32 target, muted grey for anything else."""
    colors = severity_colors(palette)
    return colors.ok if is_fat32 else colors.muted


def _readable(color: str, background: QtGui.QColor) -> str:
    """Return ``color``, lightened/darkened until it is readable on ``background``."""
    return _ensure_contrast(QtGui.QColor(color), background).name()


def _ensure_contrast(color: QtGui.QColor, background: QtGui.QColor) -> QtGui.QColor:
    """Blend ``color`` towards white/black until it reaches :data:`MIN_CONTRAST`."""
    if contrast_ratio(color, background) >= MIN_CONTRAST:
        return QtGui.QColor(color)
    target = (
        QtGui.QColor(255, 255, 255)
        if background.lightness() < 128
        else QtGui.QColor(0, 0, 0)
    )
    low, high = 0.0, 1.0
    for _ in range(20):
        middle = (low + high) / 2
        if contrast_ratio(_blend(color, target, middle), background) >= MIN_CONTRAST:
            high = middle
        else:
            low = middle
    return _blend(color, target, high)


def _blend(first: QtGui.QColor, second: QtGui.QColor, amount: float) -> QtGui.QColor:
    """``amount`` of ``second`` over ``first`` (both opaque)."""
    amount = min(1.0, max(0.0, amount))
    return QtGui.QColor(
        round(first.red() * (1 - amount) + second.red() * amount),
        round(first.green() * (1 - amount) + second.green() * amount),
        round(first.blue() * (1 - amount) + second.blue() * amount),
    )


def _luminance(color: QtGui.QColor) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.redF())
        + 0.7152 * channel(color.greenF())
        + 0.0722 * channel(color.blueF())
    )


__all__ = [
    "DARK_ERROR_TEXT",
    "DARK_MUTED_TEXT",
    "DARK_OK_TEXT",
    "DARK_WARNING_TEXT",
    "LIGHT",
    "LIGHT_ERROR_BACKGROUND",
    "LIGHT_ERROR_TEXT",
    "LIGHT_MUTED_TEXT",
    "LIGHT_OK_TEXT",
    "LIGHT_WARNING_BACKGROUND",
    "LIGHT_WARNING_TEXT",
    "MIN_CONTRAST",
    "SeverityColors",
    "badge_color",
    "contrast_ratio",
    "is_dark",
    "severity_colors",
]
