"""Unit tests for the track panes.

Two halves, matching the module: the pure row/video builders are asserted
directly on plain data, and the widget is checked for the things a user can
actually do — see the defaults, notice a disabled row, tick a checkbox and get
a new :class:`~avchdforge.selection.TrackSelection` back.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="ui/track_view.py is Qt")
pytest.importorskip("pytestqt", reason="the ui tests use pytest-qt")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from avchdforge.selection import TrackSelection, default_selection
from avchdforge.ui.track_view import (
    ERROR_BACKGROUND,
    ERROR_COLOR,
    WARNING_BACKGROUND,
    WARNING_COLOR,
    TrackRow,
    TrackView,
    audio_rows,
    subtitle_rows,
    video_html,
)
from avchdforge.validate_types import ValidationReport, error, warning

from .conftest import astream, build_media, sstream, vstream

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def mixed_media(**overrides: object):
    """1080p H.264 + AC-3 + DTS-HD + PGS + SubRip, the typical BDRemux shape."""
    return build_media(
        [
            vstream(0),
            astream(1, "ac3", channels=6, tags={"language": "eng"}),
            astream(2, "dts", profile="DTS-HD MA", channels=6, tags={"language": "jpn"}),
            sstream(3, "hdmv_pgs_subtitle", tags={"language": "eng"}),
            sstream(4, "subrip", tags={"language": "ger"}),
        ],
        **overrides,
    )


# --------------------------------------------------------------------------
# pure row builders
# --------------------------------------------------------------------------


def test_audio_rows_say_how_each_track_muxes() -> None:
    rows = audio_rows(mixed_media())

    assert [row.index for row in rows] == [1, 2]
    assert rows[0].codec == "AC-3"
    assert rows[0].action == "copy (AC-3)"
    assert rows[0].selectable
    assert "5.1" in rows[0].details
    # DTS-HD loses its extension layer before the mux
    assert rows[1].codec == "DTS (DTS-HD MA)"
    assert rows[1].action == "DTS core → DTS"
    assert rows[1].selectable


def test_truehd_without_a_core_is_disabled_with_an_explanation() -> None:
    media = build_media([vstream(0), astream(1, "truehd", profile="TrueHD")])

    row = audio_rows(media)[0]

    assert not row.selectable
    assert "AC-3 core" in row.note
    assert row.action == row.note


def test_truehd_with_an_embedded_core_is_selectable() -> None:
    media = build_media(
        [vstream(0), astream(1, "truehd", profile="TrueHD", channels=6),
         astream(2, "ac3", channels=6)]
    )

    row = audio_rows(media)[0]

    assert row.selectable
    assert row.action == "embedded AC-3 core → AC-3"
    assert "stream 2" in row.note


def test_audio_row_tooltip_merges_the_note_with_the_findings() -> None:
    row = TrackRow(
        index=1, codec="AAC", details="stereo", language="eng", flags="",
        action="copy (AAC)", selectable=True, note="",
    )

    tooltip = row.tooltip((warning("V10", "AAC is not part of AVCHD"),))

    assert tooltip == "V10: AAC is not part of AVCHD"


def test_subtitle_rows_mark_the_conversion_and_refuse_the_rest() -> None:
    media = build_media(
        [vstream(0), sstream(1, "hdmv_pgs_subtitle"), sstream(2, "subrip"),
         sstream(3, "dvd_subtitle")]
    )

    rows = subtitle_rows(media)

    assert [row.action for row in rows] == [
        "copy as PGS",
        "→ PGS (converted)",
        "not supported",
    ]
    assert [row.selectable for row in rows] == [True, True, False]
    assert "re-encoding" in rows[2].note
    assert rows[1].codec == "SubRip"


def test_subtitle_rows_surface_forced_and_default_flags() -> None:
    media = build_media(
        [vstream(0), sstream(1, "hdmv_pgs_subtitle", disposition={"forced": 1, "default": 1})]
    )

    assert subtitle_rows(media)[0].flags == "forced · default"


# --------------------------------------------------------------------------
# video pane
# --------------------------------------------------------------------------


def test_video_html_summarises_the_source() -> None:
    html = video_html(mixed_media())

    assert "<b>movie.mkv</b>" in html
    assert "Matroska / WebM" in html
    assert "H264 High 4.1 1920x1080p 23.976 fps · 9.0 Mbps" in html


def test_video_html_colours_the_level_and_bitrate_flags() -> None:
    media = build_media(
        [vstream(0, level=42, bit_rate="30000000")]
    )

    html = video_html(media)

    assert WARNING_COLOR in html
    assert html.count(f'color:{WARNING_COLOR}') == 2


def test_video_html_colours_interlaced_input() -> None:
    media = build_media([vstream(0, field_order="tt")])

    html = video_html(media)

    assert f'<span style="color:{WARNING_COLOR};">1920x1080i</span>' in html


def test_video_html_renders_findings_with_severity_colours() -> None:
    report = ValidationReport(
        (error("V3", "Not H.264"), warning("V8", "High bitrate"))
    )

    html = video_html(mixed_media(), report)

    assert f'color:{ERROR_COLOR};' in html and "V3: Not H.264" in html
    assert f'color:{WARNING_COLOR};' in html and "V8: High bitrate" in html


def test_video_html_escapes_the_file_name() -> None:
    media = build_media([vstream(0)], path="odd & <name>.mkv")

    assert "odd &amp; &lt;name&gt;.mkv" in video_html(media)


def test_video_html_of_no_media_is_empty() -> None:
    assert video_html(None) == ""


# --------------------------------------------------------------------------
# the widget
# --------------------------------------------------------------------------


def test_set_media_checks_the_smart_defaults() -> None:
    media = mixed_media()
    view = TrackView()

    view.set_media(media)

    assert view.media is media
    # first compatible audio on, PGS on, text subtitles off
    assert view.selection() == default_selection(media)
    assert view.selection().audio == (1,)
    assert view.selection().subtitles == (3,)
    assert view.audio_table.rowCount() == 2
    assert view.subtitle_table.rowCount() == 2


def test_set_media_with_an_explicit_selection_checks_exactly_that() -> None:
    view = TrackView()

    view.set_media(mixed_media(), TrackSelection(audio=(2,), subtitles=(4,)))

    assert view.selection() == TrackSelection(audio=(2,), subtitles=(4,))


def test_rows_show_stream_numbers_and_actions() -> None:
    view = TrackView()
    view.set_media(mixed_media())

    row = view.row_for("audio", 2)
    assert row is not None
    assert view.audio_table.item(row, 1).text() == "2"
    assert view.audio_table.item(row, 2).text() == "DTS (DTS-HD MA)"
    actions = [
        view.audio_table.item(view.row_for("audio", index), 6).text()
        for index in (1, 2)
    ]
    assert actions == ["copy (AC-3)", "DTS core → DTS"]


def test_unsupported_rows_cannot_be_toggled_and_explain_themselves() -> None:
    media = build_media([vstream(0), astream(1, "truehd", profile="TrueHD")])
    view = TrackView()

    view.set_media(media)

    assert not view.is_selectable("audio", 1)
    assert view.selection() == TrackSelection()
    row = view.row_for("audio", 1)
    assert row is not None
    tooltip = view.audio_table.item(row, 0).toolTip()
    assert "AC-3 core" in tooltip


def test_toggling_a_row_emits_a_new_selection() -> None:
    view = TrackView()
    view.set_media(mixed_media())
    seen: list[TrackSelection] = []
    view.selection_changed.connect(seen.append)

    view.set_checked("audio", 2, True)
    view.set_checked("audio", 1, False)

    assert seen == [
        TrackSelection(audio=(1, 2), subtitles=(3,)),
        TrackSelection(audio=(2,), subtitles=(3,)),
    ]
    assert view.selection() == TrackSelection(audio=(2,), subtitles=(3,))


def test_a_disabled_row_never_emits_anything() -> None:
    media = build_media([vstream(0), astream(1, "truehd", profile="TrueHD")])
    view = TrackView()
    view.set_media(media)
    seen: list[TrackSelection] = []
    view.selection_changed.connect(seen.append)

    view.set_checked("audio", 1, True)

    assert seen == []


def test_populating_the_view_emits_nothing() -> None:
    view = TrackView()
    seen: list[TrackSelection] = []
    view.selection_changed.connect(seen.append)

    view.set_media(mixed_media())

    assert seen == []


def test_set_report_colours_rows_by_severity() -> None:
    view = TrackView()
    view.set_media(mixed_media())
    report = ValidationReport(
        (warning("V10", "AAC warning", 1), error("V12", "TrueHD core missing", 2))
    )

    view.set_report(report)

    audio_two = view.row_for("audio", 2)
    assert audio_two is not None
    background = view.audio_table.item(audio_two, 2).background().color()
    assert background == ERROR_BACKGROUND
    tooltip = view.audio_table.item(audio_two, 2).toolTip()
    assert "V12: TrueHD core missing" in tooltip
    # earlier rows stay untouched
    audio_one = view.row_for("audio", 1)
    assert audio_one is not None
    other = view.audio_table.item(audio_one, 2).background().color()
    assert other != ERROR_BACKGROUND


def test_set_report_paints_warnings_differently() -> None:
    view = TrackView()
    view.set_media(mixed_media())

    view.set_report(ValidationReport((warning("V6", "Frame rate high", 3),)))

    row = view.row_for("subtitle", 3)
    assert row is not None
    assert view.subtitle_table.item(row, 2).background().color() == WARNING_BACKGROUND


def test_set_media_none_clears_everything() -> None:
    view = TrackView()
    view.set_media(mixed_media())

    view.set_media(None)

    assert view.media is None
    assert view.audio_table.rowCount() == 0
    assert view.subtitle_table.rowCount() == 0
    assert view.video_label.text() == ""
    assert view.selection() == TrackSelection()


def test_table_for_rejects_unknown_kinds() -> None:
    view = TrackView()

    with pytest.raises(ValueError, match="video"):
        view.table_for("video")
