"""The track panes: what the source holds, and how each track will be muxed.

Layout: the video summary on top (codec, level, resolution, frame rate, bitrate,
with the level, interlacing and bitrate flags coloured), then the audio table,
then the subtitle table. Audio and subtitle rows carry a checkbox in the first
column; rows the pipeline cannot use are *disabled* and explain themselves in a
tooltip (TrueHD without an embedded AC-3 core and E-AC-3/DD+ are the interesting
ones), and text subtitles say ``→ PGS (converted)`` so nobody expects a
passthrough.

Two kinds of code meet here, deliberately separated:

* the row builders (:func:`audio_rows`, :func:`subtitle_rows`) and
  :func:`video_html` are pure functions of :class:`~avchdforge.probe.MediaInfo`
  and a :class:`~avchdforge.validate_types.ValidationReport` — no widget, no
  state, directly testable;
* :class:`TrackView` renders them and reports checkbox changes back as an
  immutable :class:`~avchdforge.selection.TrackSelection`.

Validation stays the caller's job: the window re-runs
:func:`~avchdforge.validate.validate_all` on every change and hands the fresh
report to :meth:`TrackView.set_report`, which is what colours the rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape

from PySide6 import QtCore, QtGui, QtWidgets

from ..audio import AudioDecision, decide_audio_all
from ..constants import MAX_H264_LEVEL, VIDEO_BITRATE_WARN_BPS
from ..probe import AudioTrack, MediaInfo, SubtitleTrack, VideoTrack
from ..selection import TrackSelection, default_selection
from ..validate_types import Issue, ValidationReport
from . import theme

#: Row backgrounds for the light palette. The widget derives dark ones from its
#: own palette: pale tints under light text are what made warning rows illegible.
ERROR_BACKGROUND = theme.LIGHT_ERROR_BACKGROUND
WARNING_BACKGROUND = theme.LIGHT_WARNING_BACKGROUND
#: Text colours for the light palette; :func:`video_html` also accepts the
#: palette-aware set from :func:`theme.severity_colors`.
ERROR_COLOR = theme.LIGHT_ERROR_TEXT
WARNING_COLOR = theme.LIGHT_WARNING_TEXT

#: The checkbox lives in the first column of both tables.
CHECK_COLUMN = 0
CHECK_COLUMN_WIDTH = 30

#: Where a disabled row keeps the reason the user gets as a tooltip.
ROW_REASON_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

AUDIO_HEADERS = ("", "#", "Codec", "Channels", "Language", "Flags", "How it muxes")
SUBTITLE_HEADERS = ("", "#", "Format", "Language", "Flags", "How it muxes")


# --------------------------------------------------------------------------
# pure row builders
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackRow:
    """One table row as plain data, before any widget sees it."""

    index: int
    codec: str
    details: str
    language: str
    flags: str
    action: str
    selectable: bool = True
    #: Why the row cannot be used, or what the mux will do to it (tooltip).
    note: str = ""

    def tooltip(self, issues: tuple[Issue, ...] = ()) -> str:
        parts = [part for part in (self.note,) if part]
        parts.extend(f"{issue.code}: {issue.message}" for issue in issues)
        return "\n".join(parts)


def audio_rows(
    media: MediaInfo, decisions: dict[int, AudioDecision] | None = None
) -> tuple[TrackRow, ...]:
    """One row per audio stream, saying exactly how it reaches tsMuxeR."""
    decisions = decisions if decisions is not None else decide_audio_all(media)
    rows: list[TrackRow] = []
    for track in media.audio:
        decision = decisions.get(track.index)
        supported = bool(decision and decision.supported)
        rows.append(
            TrackRow(
                index=track.index,
                codec=track.codec_label,
                details=track.channel_label,
                language=track.language,
                flags=_flags(track),
                action=decision.describe() if decision else "unknown",
                selectable=supported,
                note=decision.note if decision else "",
            )
        )
    return tuple(rows)


def subtitle_rows(media: MediaInfo) -> tuple[TrackRow, ...]:
    """One row per subtitle stream; text streams announce the conversion."""
    rows: list[TrackRow] = []
    for track in media.subtitles:
        if track.is_pgs:
            action, selectable, note = "copy as PGS", True, ""
        elif track.is_text:
            action, selectable, note = "→ PGS (converted)", True, ""
        else:
            action = "not supported"
            selectable = False
            note = (
                f"{track.codec_label} subtitles cannot be turned into PGS without "
                f"re-encoding, which avchd-forge never does. Leave this track off."
            )
        rows.append(
            TrackRow(
                index=track.index,
                codec=track.codec_label,
                details="",
                language=track.language,
                flags=_flags(track),
                action=action,
                selectable=selectable,
                note=note,
            )
        )
    return tuple(rows)


def video_html(
    media: MediaInfo | None,
    report: ValidationReport | None = None,
    *,
    colors: theme.SeverityColors | None = None,
) -> str:
    """Rich-text summary of the source plus the validation findings.

    The colours are the point: a level above 4.1, an interlaced field order and
    a bitrate above what AVCHD likes are rendered in the warning colour right
    next to the figure that caused them. ``colors`` lets the widget pass the
    set derived from its palette; without one the established light colours are
    used, which keeps the pure renderer testable on its own.
    """
    if media is None:
        return ""
    colors = colors or theme.LIGHT
    format_label = media.format_label or media.format_name or "unknown format"
    header = (
        f"<b>{escape(media.path.name)}</b> — {escape(format_label)}, "
        f"{media.duration_label}, {media.size_label}"
    )
    lines = [header]
    if media.videos:
        lines.extend(_video_line(track, colors) for track in media.videos)
    else:
        lines.append("<i>no video stream</i>")
    if report is not None:
        lines.extend(_issue_lines(report, colors))
    return "<br>".join(lines)


def _video_line(track: VideoTrack, colors: theme.SeverityColors) -> str:
    codec = " ".join(part for part in (track.codec.upper(), track.profile or "") if part)
    level = track.level_label
    if track.level is not None and track.level > MAX_H264_LEVEL:
        level = _span(level, colors.warning)
    resolution = track.resolution_label
    if not track.is_progressive:
        resolution = _span(resolution, colors.warning)
    fps = f"{track.fps:.3f} fps" if track.fps else "unknown fps"
    if track.bitrate:
        bitrate = f"{track.bitrate / 1_000_000:.1f} Mbps"
        if track.bitrate > VIDEO_BITRATE_WARN_BPS:
            bitrate = _span(bitrate, colors.warning)
    else:
        bitrate = "unknown bitrate"
    return f"{codec} {level} {resolution} {fps} · {bitrate}"


def _issue_lines(report: ValidationReport, colors: theme.SeverityColors) -> list[str]:
    return [
        _span(f"{issue.code}: {escape(issue.message)}", _severity_color(issue, colors))
        for issue in report.issues
    ]


def _severity_color(issue: Issue, colors: theme.SeverityColors) -> str:
    return colors.error if issue.is_error else colors.warning


def _span(text: str, color: str) -> str:
    return f'<span style="color:{color};">{text}</span>'


def _flags(track: AudioTrack | SubtitleTrack) -> str:
    parts = []
    if track.is_forced:
        parts.append("forced")
    if track.is_default:
        parts.append("default")
    return " · ".join(parts)


# --------------------------------------------------------------------------
# the widget
# --------------------------------------------------------------------------


class TrackView(QtWidgets.QWidget):
    """Video summary plus the audio and subtitle checkbox tables."""

    #: Emitted with a fresh :class:`~avchdforge.selection.TrackSelection` whenever
    #: the user checks or unchecks a row.
    selection_changed = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._media: MediaInfo | None = None
        self._report = ValidationReport()
        self._updating = False

        self.video_label = QtWidgets.QLabel(self)
        self.video_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.video_label.setWordWrap(True)
        self.video_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.video_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self.video_label.setContentsMargins(6, 4, 6, 4)
        self.video_label.setMinimumHeight(52)

        self.audio_table = self._make_table(AUDIO_HEADERS)
        self.subtitle_table = self._make_table(SUBTITLE_HEADERS)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.video_label)
        layout.addWidget(_group("Audio", self.audio_table), 3)
        layout.addWidget(_group("Subtitles", self.subtitle_table), 2)
        self.set_media(None)

    # -- content ---------------------------------------------------------

    @property
    def media(self) -> MediaInfo | None:
        return self._media

    @property
    def report(self) -> ValidationReport:
        return self._report

    def set_media(
        self,
        media: MediaInfo | None,
        selection: TrackSelection | None = None,
        report: ValidationReport | None = None,
    ) -> None:
        """Show ``media`` with ``selection`` checked (smart defaults when ``None``)."""
        self._media = media
        self._report = report if report is not None else ValidationReport()
        with _frozen(self):
            if media is None:
                self.audio_table.setRowCount(0)
                self.subtitle_table.setRowCount(0)
                self.video_label.clear()
                return
            chosen = selection if selection is not None else default_selection(media)
            self._populate(self.audio_table, audio_rows(media), chosen.audio)
            self._populate(self.subtitle_table, subtitle_rows(media), chosen.subtitles)
            self.video_label.setText(
                video_html(media, self._report, colors=self._colors())
            )

    def set_report(self, report: ValidationReport) -> None:
        """Recolour the rows and re-render the findings after a re-validation."""
        self._report = report
        if self._media is None:
            return
        with _frozen(self):
            self.video_label.setText(
                video_html(self._media, report, colors=self._colors())
            )
            for table in (self.audio_table, self.subtitle_table):
                self._paint(table)

    def changeEvent(self, event: QtCore.QEvent) -> None:
        """Re-derive the explicit colours when the application palette changes."""
        super().changeEvent(event)
        if event.type() not in (
            QtCore.QEvent.Type.PaletteChange,
            QtCore.QEvent.Type.StyleChange,
        ):
            return
        if not hasattr(self, "audio_table"):  # pragma: no cover - during construction
            return
        if self._media is not None:
            self.video_label.setText(
                video_html(self._media, self._report, colors=self._colors())
            )
        for table in (self.audio_table, self.subtitle_table):
            self._paint(table)

    def _colors(self) -> theme.SeverityColors:
        return theme.severity_colors(self.palette())

    def selection(self) -> TrackSelection:
        """The current checkbox state, as the core expects it."""
        return TrackSelection(
            audio=_checked(self.audio_table),
            subtitles=_checked(self.subtitle_table),
        )

    def table_for(self, kind: str) -> QtWidgets.QTableWidget:
        """The table holding ``"audio"`` or ``"subtitle"`` rows."""
        if kind == "audio":
            return self.audio_table
        if kind == "subtitle":
            return self.subtitle_table
        raise ValueError(f"unknown track kind {kind!r}")

    def row_for(self, kind: str, index: int) -> int | None:
        """Table row showing stream ``index`` of ``kind``, if any."""
        table = self.table_for(kind)
        for row in range(table.rowCount()):
            item = table.item(row, CHECK_COLUMN)
            if item is not None and item.data(QtCore.Qt.ItemDataRole.UserRole) == index:
                return row
        return None

    def is_selectable(self, kind: str, index: int) -> bool:
        """Whether the checkbox of that stream can be toggled at all."""
        row = self.row_for(kind, index)
        if row is None:
            return False
        item = self.table_for(kind).item(row, CHECK_COLUMN)
        return bool(item is not None and item.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled)

    def set_checked(self, kind: str, index: int, checked: bool) -> None:
        """Check or uncheck a row (used by tests and future "select all" actions)."""
        row = self.row_for(kind, index)
        if row is None:  # pragma: no cover - callers pass known streams
            return
        item = self.table_for(kind).item(row, CHECK_COLUMN)
        assert item is not None  # narrowed by row_for
        item.setCheckState(
            QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        )

    # -- building --------------------------------------------------------

    def _make_table(self, headers: tuple[str, ...]) -> QtWidgets.QTableWidget:
        table = QtWidgets.QTableWidget(0, len(headers), self)
        table.setHorizontalHeaderLabels(list(headers))
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setMinimumHeight(96)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(CHECK_COLUMN, QtWidgets.QHeaderView.ResizeMode.Fixed)
        header.setStretchLastSection(True)
        table.setColumnWidth(CHECK_COLUMN, CHECK_COLUMN_WIDTH)
        table.itemChanged.connect(self._on_item_changed)
        return table

    def _populate(
        self, table: QtWidgets.QTableWidget, rows: tuple[TrackRow, ...], checked: tuple[int, ...]
    ) -> None:
        table.setRowCount(len(rows))
        for row, data in enumerate(rows):
            box = QtWidgets.QTableWidgetItem()
            flags = QtCore.Qt.ItemFlag.ItemIsUserCheckable | QtCore.Qt.ItemFlag.ItemIsSelectable
            if data.selectable:
                flags |= QtCore.Qt.ItemFlag.ItemIsEnabled
            box.setFlags(flags)
            box.setCheckState(
                QtCore.Qt.CheckState.Checked
                if data.index in checked
                else QtCore.Qt.CheckState.Unchecked
            )
            box.setData(QtCore.Qt.ItemDataRole.UserRole, data.index)
            box.setData(ROW_REASON_ROLE, data.note)
            table.setItem(row, CHECK_COLUMN, box)
            cells = (
                str(data.index),
                data.codec,
                *((data.details,) if data.details else ()),
                data.language,
                data.flags,
                data.action,
            )
            for column, text in enumerate(cells, start=1):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                table.setItem(row, column, item)
        self._paint(table)

    def _paint(self, table: QtWidgets.QTableWidget) -> None:
        """Row colour + tooltip from the report and the row's own reason."""
        colors = self._colors()
        for row in range(table.rowCount()):
            box = table.item(row, CHECK_COLUMN)
            if box is None:  # pragma: no cover - every row gets one in _populate
                continue
            issues = self._issues_for(box.data(QtCore.Qt.ItemDataRole.UserRole))
            tooltip = "\n".join(
                part
                for part in (
                    box.data(ROW_REASON_ROLE) or "",
                    *(f"{issue.code}: {issue.message}" for issue in issues),
                )
                if part
            )
            background = _row_background(issues, colors)
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is None:  # pragma: no cover - defensive
                    continue
                item.setBackground(background)
                item.setToolTip(tooltip)

    def _issues_for(self, index: object) -> tuple[Issue, ...]:
        if index is None:  # pragma: no cover - defensive
            return ()
        return tuple(issue for issue in self._report.issues if issue.track_index == index)

    def _on_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if self._updating or self._media is None:
            return
        if item.data(QtCore.Qt.ItemDataRole.UserRole) is None:
            return
        if not item.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled:
            return
        self.selection_changed.emit(self.selection())


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@contextmanager
def _frozen(view: TrackView) -> Iterator[None]:
    """Suppress ``selection_changed`` while the widget is being rebuilt."""
    previous = view._updating
    view._updating = True
    try:
        yield
    finally:
        view._updating = previous


def _group(title: str, widget: QtWidgets.QWidget) -> QtWidgets.QGroupBox:
    box = QtWidgets.QGroupBox(title)
    layout = QtWidgets.QVBoxLayout(box)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.setSpacing(2)
    layout.addWidget(widget)
    return box


def _checked(table: QtWidgets.QTableWidget) -> tuple[int, ...]:
    found: list[int] = []
    for row in range(table.rowCount()):
        item = table.item(row, CHECK_COLUMN)
        if item is not None and item.checkState() == QtCore.Qt.CheckState.Checked:
            index = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if index is not None:
                found.append(int(index))
    return tuple(sorted(found))


def _row_background(
    issues: tuple[Issue, ...], colors: theme.SeverityColors
) -> QtGui.QBrush:
    if any(issue.is_error for issue in issues):
        return QtGui.QBrush(colors.error_background)
    if issues:
        return QtGui.QBrush(colors.warning_background)
    return QtGui.QBrush()


__all__ = [
    "AUDIO_HEADERS",
    "CHECK_COLUMN",
    "CHECK_COLUMN_WIDTH",
    "ERROR_BACKGROUND",
    "ERROR_COLOR",
    "ROW_REASON_ROLE",
    "SUBTITLE_HEADERS",
    "WARNING_BACKGROUND",
    "WARNING_COLOR",
    "TrackRow",
    "TrackView",
    "audio_rows",
    "subtitle_rows",
    "video_html",
]
