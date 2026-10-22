"""Unit tests for the palette-aware colours.

The hand-coloured parts of the UI — style sheets, table-item brushes and rich
text — cannot follow the palette by themselves, so ``ui/theme.py`` derives them
from it. These tests pin the property that matters: text stays readable and the
warning/error surfaces stay recognisable on both a light and a dark scheme.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="ui/theme.py is Qt")
pytest.importorskip("pytestqt", reason="the ui tests use pytest-qt")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtGui

from avchdforge.ui import theme

from .conftest import dark_palette

pytestmark = pytest.mark.usefixtures("qapp")


def test_contrast_ratio_matches_the_wcag_extremes() -> None:
    black = QtGui.QColor("#000000")
    white = QtGui.QColor("#ffffff")

    assert theme.contrast_ratio(black, black) == pytest.approx(1.0)
    assert theme.contrast_ratio(black, white) == pytest.approx(21.0)


def test_the_default_palette_keeps_the_established_light_colours() -> None:
    palette = QtGui.QPalette()

    assert not theme.is_dark(palette)
    assert theme.severity_colors(palette) == theme.LIGHT


def test_dark_text_colours_are_readable_on_the_window_and_the_tables(qapp) -> None:
    palette = dark_palette()
    colors = theme.severity_colors(palette)

    assert theme.is_dark(palette)
    for color in (colors.error, colors.warning, colors.ok, colors.muted):
        foreground = QtGui.QColor(color)
        for role in (QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Base):
            assert theme.contrast_ratio(
                foreground, palette.color(role)
            ) >= theme.MIN_CONTRAST


def test_dark_row_backgrounds_are_dark_tints_of_their_severity(qapp) -> None:
    palette = dark_palette()
    colors = theme.severity_colors(palette)

    assert colors.error_background.lightness() < 128
    assert colors.warning_background.lightness() < 128
    assert colors.error_background != theme.LIGHT_ERROR_BACKGROUND
    assert colors.warning_background != theme.LIGHT_WARNING_BACKGROUND
    assert colors.error_background != colors.warning_background
    text = palette.color(QtGui.QPalette.ColorRole.Text)
    for background in (colors.error_background, colors.warning_background):
        assert theme.contrast_ratio(text, background) >= theme.MIN_CONTRAST


def test_a_custom_mid_tone_palette_still_gets_readable_text() -> None:
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#606060"))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("#606060"))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("#f0f0f0"))

    colors = theme.severity_colors(palette)

    for color in (colors.error, colors.warning, colors.ok, colors.muted):
        assert theme.contrast_ratio(
            QtGui.QColor(color), palette.color(QtGui.QPalette.ColorRole.Window)
        ) >= theme.MIN_CONTRAST


def test_the_badge_is_green_only_for_a_fat32_target() -> None:
    palette = dark_palette()
    colors = theme.severity_colors(palette)

    assert theme.badge_color(True, palette) == colors.ok
    assert theme.badge_color(False, palette) == colors.muted
