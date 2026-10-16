"""The main window: source, output, tracks, run bar and the log pane.

Everything the user does before pressing Start is pure UI state — a source path,
an output folder, a :class:`~avchdforge.selection.TrackSelection` — and pressing
Start turns exactly that state into a :class:`~avchdforge.jobs.JobRequest`. The
window never builds argv and never touches a tool directly: the probes, the mux
and the tool checks all go through :class:`~avchdforge.jobs.JobRunner` and the
injected ``runner_factory``, which is why the whole window is testable on a
``FakeRunner``.

Three deliberate choices worth remembering:

* **No modal dialogs while working.** A finished, failed or cancelled run is
  reported in the run bar (one line, full text in the tooltip) and in the log
  pane; only user-triggered menu actions open dialogs — and the startup tool gate
  (:meth:`MainWindow.ensure_tools`), which runs before the window is shown and
  before any job exists.
* **The log is a collapsible bottom pane**, not a dock: its own toggle button
  already carries the state, and the splitter keeps it resizable.
* **Target warnings never block.** ``assess_target`` warnings (V15/V16) are shown
  inline; only a read-only target or a missing toolchain actually disables Start.

The window owns exactly one tool state: :meth:`check_tools` adopts a report (and
with it the toolchain and Start's enabled-ness), while ``adopt=False`` answers the
same question for the Preferences dialog without touching anything — an unsaved
path in a dialog must not disable Start.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .. import __version__
from ..capabilities import Toolchain
from ..constants import ALLOWED_INPUT_SUFFIXES, INPUT_FILE_FILTER
from ..drives import Drive, TargetReport, assess_target, list_drives
from ..errors import AvchdForgeError
from ..jobs import (
    JobCancellation,
    JobFailure,
    JobOutcome,
    JobRequest,
    JobRunner,
    ProbeOutcome,
    RunnerFactory,
)
from ..plan import OUTPUT_OVERHEAD, PlanOptions
from ..probe import MediaInfo
from ..process import QProcessRunner
from ..selection import TrackSelection, default_selection
from ..settings import AppSettings
from ..tooling import FFPROBE, ToolReport, check_toolchain
from ..validate import validate_all
from ..validate_types import ValidationReport
from .log_pane import LogPane
from .preferences import PreferencesDialog
from .tool_gate import CONTINUE, PREFERENCES, QUIT, ToolGateDialog
from .track_view import ERROR_COLOR, WARNING_COLOR, TrackView

#: Badge colour for a FAT32 target (green), and for everything else.
BADGE_OK_COLOR = "#1e8449"
BADGE_UNKNOWN_COLOR = "#6c757d"

#: Placeholder entry at the top of the drive combo (no drive selected).
DRIVE_PLACEHOLDER = "Detected drives…"


# --------------------------------------------------------------------------
# drag and drop (pure so the window does not have to exist in the test)
# --------------------------------------------------------------------------


def dropped_file(mime: QtCore.QMimeData) -> Path | None:
    """The single local file a drop carries, or ``None`` for zero or several."""
    if not mime.hasUrls():
        return None
    locals_ = [
        Path(url.toLocalFile())
        for url in mime.urls()
        if url.isLocalFile() and url.toLocalFile()
    ]
    if len(locals_) != 1:
        return None
    return locals_[0]


def drop_accepts(mime: QtCore.QMimeData) -> bool:
    """True when the drop is exactly one existing file with an allowed suffix."""
    path = dropped_file(mime)
    return (
        path is not None
        and path.suffix.lower() in ALLOWED_INPUT_SUFFIXES
        and path.is_file()
    )


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------


class MainWindow(QtWidgets.QMainWindow):
    """The one window the application has."""

    def __init__(
        self,
        settings: AppSettings | QtCore.QSettings | None = None,
        runner_factory: RunnerFactory | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if settings is None:
            self._settings = AppSettings()
        elif isinstance(settings, AppSettings):
            self._settings = settings
        else:
            # A raw QSettings (what ``settings_for`` returns) is convenient in tests.
            self._settings = AppSettings(settings)
        self._runner_factory: RunnerFactory = runner_factory or (
            lambda: QProcessRunner(parent=self)
        )
        self._job = JobRunner(self, runner_factory=self._runner_factory)

        self._tool_report: ToolReport | None = None
        self._toolchain: Toolchain | None = None
        self._drives: tuple[Drive, ...] = ()
        self._outcome: ProbeOutcome | None = None
        self._report: ValidationReport | None = None
        self._target: TargetReport | None = None
        self._temp_dir: Path | None = None
        self._busy = False
        self._progress: float | None = None
        self._started_at: float | None = None
        self._syncing_combo = False

        self.setWindowTitle("avchd-forge")
        self.setAcceptDrops(True)
        self.setStatusBar(QtWidgets.QStatusBar(self))

        self._elapsed_timer = QtCore.QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self._build_ui()
        self._build_menus()
        self._wire_signals()
        self._restore_state()
        self._assess_output()
        self._update_actions()
        self._update_status()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._source_row(central))
        layout.addWidget(self._output_row(central))

        self.track_view = TrackView(central)
        self.log_pane = LogPane(central)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical, central)
        splitter.addWidget(self.track_view)
        splitter.addWidget(self.log_pane)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        layout.addWidget(self._run_bar(central))
        self.setCentralWidget(central)

    def _source_row(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget(parent)
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QtWidgets.QLabel("Source", row))
        self.source_edit = QtWidgets.QLineEdit(row)
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText(
            "Drop a .mkv/.ts file here, or press “Open file…”"
        )
        self.open_button = QtWidgets.QPushButton("Open file…", row)
        layout.addWidget(self.source_edit, 1)
        layout.addWidget(self.open_button)
        return row

    def _output_row(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QtWidgets.QLabel("Output", row))
        self.drive_combo = QtWidgets.QComboBox(row)
        self.drive_combo.setMinimumWidth(240)
        self.drive_combo.setToolTip("Mounted drives, removable ones first (Tools ▸ Re-check)")
        self.output_edit = QtWidgets.QLineEdit(row)
        self.output_edit.setPlaceholderText("Folder that will receive the BDMV directory")
        self.browse_button = QtWidgets.QPushButton("Browse…", row)
        self.badge_label = QtWidgets.QLabel(row)
        self.badge_label.setMinimumWidth(120)
        top.addWidget(self.drive_combo)
        top.addWidget(self.output_edit, 1)
        top.addWidget(self.browse_button)
        top.addWidget(self.badge_label)

        self.output_warning = QtWidgets.QLabel(row)
        self.output_warning.setWordWrap(True)
        self.output_warning.setVisible(False)

        layout.addLayout(top)
        layout.addWidget(self.output_warning)
        return row

    def _run_bar(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget(parent)
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self.stage_label = QtWidgets.QLabel("Ready", bar)
        self.stage_label.setMinimumWidth(150)
        self.stage_label.setStyleSheet("color: palette(mid);")

        self.status_label = QtWidgets.QLabel(bar)
        self.status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred
        )

        self.progress_bar = QtWidgets.QProgressBar(bar)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(220)
        self.progress_bar.setTextVisible(True)

        self.elapsed_label = QtWidgets.QLabel("", bar)
        self.elapsed_label.setFixedWidth(52)
        self.elapsed_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )

        self.open_output_button = QtWidgets.QPushButton("Open output folder", bar)
        self.open_temp_button = QtWidgets.QPushButton("Open temp dir", bar)
        self.cancel_button = QtWidgets.QPushButton("Cancel", bar)
        self.start_button = QtWidgets.QPushButton("Start", bar)
        self.start_button.setDefault(True)

        layout.addWidget(self.stage_label)
        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.elapsed_label)
        layout.addWidget(self.open_output_button)
        layout.addWidget(self.open_temp_button)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.start_button)
        return bar

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.open_action = file_menu.addAction("&Open file…")
        self.open_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        self.open_output_action = file_menu.addAction("Open output folder")
        file_menu.addSeparator()
        quit_action = file_menu.addAction("&Quit")
        quit_action.setShortcut(QtGui.QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)

        tools_menu = self.menuBar().addMenu("&Tools")
        self.preferences_action = tools_menu.addAction("&Preferences…")
        self.preferences_action.setShortcut(QtGui.QKeySequence.StandardKey.Preferences)
        tools_menu.addSeparator()
        self.recheck_action = tools_menu.addAction("Re-check external tools")
        self.versions_action = tools_menu.addAction("Show tool versions…")

        help_menu = self.menuBar().addMenu("&Help")
        about_action = help_menu.addAction("&About avchd-forge")
        temp_action = help_menu.addAction("Open temp directory")

        self.open_action.triggered.connect(self.choose_source)
        self.open_output_action.triggered.connect(self.open_output_folder)
        # ``triggered`` carries a ``checked`` bool: never let it become the first
        # argument of ``check_tools``/``open_preferences``.
        self.preferences_action.triggered.connect(lambda _checked=False: self.open_preferences())
        self.recheck_action.triggered.connect(lambda _checked=False: self.check_tools())
        self.versions_action.triggered.connect(self.show_tool_versions)
        about_action.triggered.connect(self.show_about)
        temp_action.triggered.connect(self.open_temp_dir)

    def _wire_signals(self) -> None:
        self.open_button.clicked.connect(self.choose_source)
        self.browse_button.clicked.connect(self.choose_output)
        self.start_button.clicked.connect(self.start)
        self.cancel_button.clicked.connect(self.cancel)
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_temp_button.clicked.connect(self.open_temp_dir)
        self.output_edit.textChanged.connect(self._on_output_changed)
        self.drive_combo.currentIndexChanged.connect(self._on_drive_selected)
        self.log_pane.toggle_button.toggled.connect(self._on_log_toggled)
        self.track_view.selection_changed.connect(self._on_selection_changed)

        self._job.probed.connect(self._on_probed)
        self._job.finished.connect(self._on_finished)
        self._job.failed.connect(self._on_failed)
        self._job.cancelled.connect(self._on_cancelled)
        self._job.line.connect(self._on_line)
        self._job.progress.connect(self._on_progress)
        self._job.stage_changed.connect(self._on_stage)

    # ------------------------------------------------------------------
    # tools and drives
    # ------------------------------------------------------------------

    def check_tools(
        self,
        overrides: Mapping[str, Path] | None = None,
        *,
        adopt: bool = True,
    ) -> ToolReport:
        """Locate tsMuxeR/ffmpeg/ffprobe, and (by default) adopt what was found.

        ``overrides`` replaces the stored preferences for this one check: the
        Preferences dialog's "Check now" passes the paths currently typed in, so a
        typo is visible before anything is saved. ``adopt=False`` only answers the
        question — Start keeps working off the *saved* report, because an unsaved
        path in an open dialog is not a decision yet.
        """
        if overrides is None:
            overrides = self._settings.tool_overrides()
        report = self._query_tools(overrides)
        if adopt:
            self.set_tool_report(report)
        return report

    def _query_tools(self, overrides: Mapping[str, Path]) -> ToolReport:
        """Run the tool check without changing the window's tool state."""
        runner = self._runner_factory()
        try:
            return check_toolchain(runner, overrides=overrides)
        except AvchdForgeError as exc:
            # A broken ffmpeg capability query must not swallow the tool report.
            self._on_line(f"[avchd-forge] capability probe failed: {exc}\n")
            return check_toolchain(runner, overrides=overrides, probe_capabilities=False)

    def ensure_tools(self) -> bool:
        """The startup gate: check, offer a way out, say whether to keep going.

        Returns ``False`` only when the user chose *Quit*. A usable-but-warned
        report (an old version) is offered *Continue*, because continuing is a real
        option there; a missing tool is not.
        """
        while True:
            report = self.check_tools()
            if report.ok and not report.warnings:
                return True
            choice = ToolGateDialog.ask(report, self)
            if choice == CONTINUE:
                return True
            if choice == QUIT:
                return False
            if choice == PREFERENCES:
                self.open_preferences(tab="tools")
            # ``recheck`` — and anything else — loops back to the check, which is
            # also what makes a repaired path take effect immediately.

    def set_tool_report(self, report: ToolReport) -> None:
        """Adopt a tool report (also the seam tests use instead of check_tools)."""
        self._tool_report = report
        try:
            self._toolchain = report.require_toolchain() if report.ok else None
        except AvchdForgeError:  # pragma: no cover - guarded by report.ok
            self._toolchain = None
        for line in report.messages():
            self._on_line(f"[avchd-forge] {line}\n")
        if not report.ok:
            missing = ", ".join(tool.name for tool in report.missing)
            self._on_line(f"[avchd-forge] missing external tools: {missing}\n")
        self._update_actions()
        self._update_status()

    @property
    def tool_report(self) -> ToolReport | None:
        return self._tool_report

    @property
    def toolchain(self) -> Toolchain | None:
        return self._toolchain

    def refresh_drives(self) -> tuple[Drive, ...]:
        """Re-detect mounted drives through the injected runner."""
        runner = self._runner_factory()
        drives = list_drives(runner)
        self.set_drives(drives)
        return drives

    def set_drives(self, drives: Sequence[Drive]) -> None:
        """Adopt a drive list (the app passes what lsblk reported)."""
        self._drives = tuple(drives)
        previous = self.drive_combo.currentIndex()
        self._syncing_combo = True
        try:
            self.drive_combo.clear()
            self.drive_combo.addItem(DRIVE_PLACEHOLDER, None)
            for drive in self._drives:
                self.drive_combo.addItem(drive.describe(), str(drive.mountpoint))
            self.drive_combo.setCurrentIndex(0 if previous < 1 else previous)
        finally:
            self._syncing_combo = False
        self._sync_drive_selection()
        self._assess_output()

    # ------------------------------------------------------------------
    # source
    # ------------------------------------------------------------------

    @property
    def source(self) -> Path | None:
        text = self.source_edit.text().strip()
        return Path(text) if text else None

    def open_source(self, path: Path | str | None = None) -> bool:
        """Probe ``path`` (or whatever the picker returned) and fill the panes."""
        if self._job.busy:
            return False
        if self._toolchain is None:
            self.notify("External tools are missing — set their paths in Tools ▸ Preferences…")
            return False
        if path is None:
            path = self.choose_source()
        if path is None:
            return False
        target = Path(path).expanduser()
        if not target.is_file():
            self.notify(f"No such file: {target}")
            return False

        self.source_edit.setText(str(target))
        self.setWindowTitle(f"avchd-forge — {target.name}")
        self._outcome = None
        self._report = None
        self._temp_dir = None
        self.track_view.set_media(None)
        self._update_actions()

        self.stage_label.setText(f"Reading {target.name}")
        self._set_busy(True)
        self._job.start_probe(target, ffprobe=self._ffprobe_tool())
        return True

    def choose_source(self) -> Path | None:
        """Open the file picker; returns the chosen path or ``None``."""
        start = self._settings.last_input_dir()
        chosen, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open a media file", str(start) if start else "", INPUT_FILE_FILTER
        )
        if not chosen:
            return None
        self._settings.set_last_input_dir(Path(chosen).parent)
        self.open_source(chosen)
        return Path(chosen)

    def _default_selection(self, media: MediaInfo) -> TrackSelection:
        """Smart defaults honouring the language priorities from Preferences."""
        return default_selection(
            media,
            audio_language_priority=self._settings.audio_priority(),
            subtitle_language_priority=self._settings.subtitle_priority(),
        )

    def _validate(self, media: MediaInfo, selection: TrackSelection) -> ValidationReport:
        """``validate_all`` with the compatibility choices from Preferences."""
        return validate_all(
            media,
            selection,
            allow_nonstandard_resolution=self._settings.allow_nonstandard_resolution,
        )

    def _revalidate_report(self) -> None:
        """Re-check the current selection after a preference that changes V4's severity."""
        if self._outcome is None:
            return
        selection = self.track_view.selection()
        self._report = self._validate(self._outcome.media, selection)
        self.track_view.set_report(self._report)
        self._update_actions()
        self._update_status()
        self._on_line("[avchd-forge] resolution policy changed — re-checked\n")

    def _reselect_defaults(self) -> None:
        """Re-derive the checked tracks from the (possibly new) language priorities."""
        if self._outcome is None:
            return
        selection = self._default_selection(self._outcome.media)
        self._report = self._validate(self._outcome.media, selection)
        self.track_view.set_media(self._outcome.media, selection, self._report)
        self._update_actions()
        self._update_status()
        self._on_line("[avchd-forge] language priorities changed — defaults re-derived\n")

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------

    def output_dir(self) -> Path | None:
        text = self.output_edit.text().strip()
        return Path(text).expanduser() if text else None

    def set_output_dir(self, path: Path | str) -> None:
        """Choose an output folder (re-assessment happens either way)."""
        text = str(Path(path).expanduser())
        if text == self.output_edit.text():
            # Same folder: ``textChanged`` will not fire, but the caller may be
            # re-checking after a preferences change, so assess explicitly.
            self._on_output_changed()
        else:
            self.output_edit.setText(text)

    def choose_output(self) -> Path | None:
        """Open the folder picker; returns the chosen path or ``None``."""
        start = self.output_dir() or self._settings.last_output_dir()
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose the output folder", str(start) if start else ""
        )
        if not chosen:
            return None
        self.set_output_dir(chosen)
        return Path(chosen)

    def target_report(self) -> TargetReport | None:
        return self._target

    def _on_drive_selected(self, index: int) -> None:
        if self._syncing_combo:
            return
        data = self.drive_combo.itemData(index)
        if data:
            self.set_output_dir(Path(data))

    def _on_output_changed(self) -> None:
        self._sync_drive_selection()
        self._assess_output()
        self._update_actions()
        self._update_status()

    def _sync_drive_selection(self) -> None:
        """Point the combo at the drive that contains the current output path."""
        target = self.output_dir()
        index = 0
        if target is not None:
            for position in range(1, self.drive_combo.count()):
                mount = Path(str(self.drive_combo.itemData(position)))
                if target == mount or mount in target.parents:
                    index = position
        if self.drive_combo.currentIndex() == index:
            return
        self._syncing_combo = True
        try:
            self.drive_combo.setCurrentIndex(index)
        finally:
            self._syncing_combo = False

    def _assess_output(self) -> None:
        """Badge + inline warnings for the current output directory."""
        target = self.output_dir()
        if target is None:
            self._target = None
            self.badge_label.setText("no output chosen")
            self.badge_label.setStyleSheet(f"color: {BADGE_UNKNOWN_COLOR};")
            self.output_warning.setVisible(False)
            return
        report = assess_target(
            target,
            drives=list(self._drives),
            needed_bytes=self._estimate_bytes(),
            splitting=self._settings.split_enabled,
        )
        self._target = report
        self.badge_label.setText(report.badge)
        color = BADGE_OK_COLOR if report.is_fat32 else BADGE_UNKNOWN_COLOR
        self.badge_label.setStyleSheet(f"color: {color};")
        messages = report.messages()
        self.output_warning.setText("\n".join(messages))
        self.output_warning.setStyleSheet(
            f"color: {ERROR_COLOR if report.has_error else WARNING_COLOR};"
        )
        self.output_warning.setVisible(bool(messages))

    def _estimate_bytes(self) -> int | None:
        """Rough output size: the mux adds little to the source streams."""
        if self._outcome is None or not self._outcome.media.size_bytes:
            return None
        return int(self._outcome.media.size_bytes * OUTPUT_OVERHEAD)

    # ------------------------------------------------------------------
    # job control
    # ------------------------------------------------------------------

    def plan_options(self) -> PlanOptions:
        """Preferences + the capabilities of the resolved ffmpeg."""
        chain = self._toolchain
        return self._settings.plan_options(
            chain.capabilities if chain else None,
            tsmuxer=chain.tsmuxer if chain else None,
            ffmpeg=chain.ffmpeg if chain else None,
        )

    def _ffprobe_tool(self) -> str:
        return str(self._toolchain.ffprobe) if self._toolchain else FFPROBE

    def job_request(self) -> JobRequest | None:
        """The :class:`JobRequest` Start would run, or ``None`` when not ready."""
        if self._outcome is None:
            return None
        output_dir = self.output_dir()
        if output_dir is None:
            return None
        return JobRequest(
            source=self._outcome.source,
            output_dir=output_dir,
            selection=self.track_view.selection(),
            options=self.plan_options(),
            temp_dir=self._settings.temp_dir(),
            ffprobe=self._ffprobe_tool(),
            keep_temp_on_success=self._settings.keep_temp_on_success,
            keep_temp_on_failure=self._settings.keep_temp_on_failure,
        )

    def start(self) -> bool:
        """Run the mux job; returns whether it was started."""
        if self._job.busy:
            return False
        request = self.job_request()
        if request is None or self._toolchain is None:
            return False
        if self._report is not None and not self._report.ok:
            return False

        self.log_pane.set_collapsed(False)
        self._on_line(
            f"[avchd-forge] starting job: {request.source.name} → {request.output_dir}\n"
        )
        self.stage_label.setText("Starting")
        self._settings.set_last_output_dir(request.output_dir)
        self._set_busy(True)
        self._job.start_job(request)
        return True

    def cancel(self) -> None:
        if not self._job.busy:
            return
        self.stage_label.setText("Cancelling")
        self._job.cancel()

    # -- job signals ----------------------------------------------------

    def _on_line(self, text: str) -> None:
        self.log_pane.append(text)

    def _on_stage(self, stage: object) -> None:
        label = getattr(stage, "label", "")
        self.stage_label.setText(label)
        self.log_pane.set_stage(label)

    def _on_progress(self, value: float | None) -> None:
        self._progress = None if value is None else float(value)
        if self._progress is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(round(self._progress * 1000))

    def _on_probed(self, outcome: ProbeOutcome) -> None:
        self._outcome = outcome
        selection = self._default_selection(outcome.media)
        self._report = self._validate(outcome.media, selection)
        self.track_view.set_media(outcome.media, selection, self._report)
        self.stage_label.setText("Ready")
        self.log_pane.set_stage("")
        self._assess_output()
        self._set_busy(False)
        self._update_status()

    def _on_selection_changed(self, selection: TrackSelection) -> None:
        if self._outcome is None:
            return
        self._report = self._validate(self._outcome.media, selection)
        self.track_view.set_report(self._report)
        self._update_actions()
        self._update_status()
        rows = len(selection.audio) + len(selection.subtitles)
        self._on_line(f"[avchd-forge] selection: {rows} track(s)\n")

    def _on_finished(self, outcome: JobOutcome) -> None:
        count = len(outcome.stream_files)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self._progress = 1.0
        self.stage_label.setText("Done")
        self.log_pane.set_stage("")
        self._temp_dir = outcome.plan.temp_dir if outcome.temp_dir_kept else None
        self._settings.set_last_output_dir(outcome.plan.output_dir)
        self._set_busy(False)
        self.status_label.setText(
            f"Done — {count} stream part{'s' if count != 1 else ''} written to "
            f"{outcome.plan.output_dir}"
        )
        self.notify(f"Mux finished: {count} stream part(s)")

    def _on_failed(self, failure: JobFailure) -> None:
        self.progress_bar.setRange(0, 1000)
        if self._progress is None:
            self.progress_bar.setValue(0)
        self.stage_label.setText("Failed")
        self.log_pane.set_stage("")
        self._temp_dir = failure.temp_dir
        self._set_busy(False)
        self.status_label.setText(failure.title)
        self.status_label.setToolTip(failure.detail)
        self.notify(failure.detail)

    def _on_cancelled(self, cancellation: JobCancellation) -> None:
        self.progress_bar.setRange(0, 1000)
        self.stage_label.setText("Cancelled")
        self.log_pane.set_stage("")
        self._temp_dir = cancellation.temp_dir
        self._set_busy(False)
        self.status_label.setText(f"Cancelled {cancellation.stage}".strip())
        self.notify("Job cancelled")

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def probe_outcome(self) -> ProbeOutcome | None:
        """The last successful probe, if any (the UI state Start works from)."""
        return self._outcome

    @property
    def temp_dir(self) -> Path | None:
        """Temp directory kept after a failure or cancellation, if any."""
        return self._temp_dir

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self._started_at = time.monotonic()
            self._progress = None
            self.progress_bar.setRange(0, 0)
            self.elapsed_label.setText("0:00")
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
            self._started_at = None
        self._update_actions()

    def _tick_elapsed(self) -> None:
        if self._started_at is None:  # pragma: no cover - timer only runs while busy
            return
        seconds = int(time.monotonic() - self._started_at)
        self.elapsed_label.setText(f"{seconds // 60}:{seconds % 60:02d}")

    def _update_actions(self) -> None:
        has_tools = self._toolchain is not None
        usable = (
            self._outcome is not None
            and self._report is not None
            and self._report.ok
        )
        output = self.output_dir() is not None
        self.start_button.setEnabled(not self._busy and usable and output and has_tools)
        self.start_button.setToolTip(self._start_hint(usable, output, has_tools))
        self.cancel_button.setEnabled(self._busy)
        self.open_output_button.setEnabled(output)
        self.open_output_action.setEnabled(output)
        self.open_temp_button.setEnabled(self._temp_dir is not None)
        for widget in (self.open_button, self.browse_button, self.drive_combo):
            widget.setEnabled(not self._busy)
        # Modals are menu-only (D15), so the menu is what closes while a job runs.
        for action in (
            self.open_action,
            self.preferences_action,
            self.recheck_action,
            self.versions_action,
        ):
            action.setEnabled(not self._busy)

    def _start_hint(self, usable: bool, output: bool, has_tools: bool) -> str:
        if not has_tools:
            return "External tools missing — set their paths in Tools ▸ Preferences…"
        if self._outcome is None:
            return "Open a media file first"
        if not usable:
            assert self._report is not None  # narrowed by the caller
            first = self._report.errors[0] if self._report.errors else None
            return f"This file cannot be used — {first.message}" if first else "Not ready"
        if not output:
            return "Choose an output folder"
        return "Start the mux"

    def _update_status(self) -> None:
        if self._busy:
            return
        if self._toolchain is None and self._tool_report is not None:
            self.status_label.setText("External tools missing")
            return
        if self._outcome is None:
            self.status_label.setText("Ready — open a .mkv or .ts file")
            return
        if self._report is not None and not self._report.ok:
            first = self._report.errors[0]
            self.status_label.setText(f"This file cannot be used — {first.message}")
            return
        media = self._outcome.media
        self.status_label.setText(
            f"{media.path.name} — {len(media.audio)} audio, "
            f"{len(media.subtitles)} subtitle track(s), {media.duration_label}"
        )

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def notify(self, text: str) -> None:
        """Transient message in the status bar."""
        self.statusBar().showMessage(text, 8000)

    def open_output_folder(self) -> None:
        target = self.output_dir()
        if target is not None:
            _open_path(target, self)

    def open_temp_dir(self) -> None:
        if self._temp_dir is not None:
            _open_path(self._temp_dir, self)

    def open_preferences(self, tab: str = "") -> bool:
        """Edit the preferences, then redo whatever the changes made stale.

        Returns whether OK was pressed. ``tab`` opens straight on one of the tabs
        (the startup gate asks for ``"tools"``; the menu opens on the first). Four
        preferences have visible consequences and are acted on here; everything
        else is read when it is needed (``plan_options`` builds a fresh plan per
        job):

        * **tool paths** → re-check and adopt, because a changed path is usually
          the repair of a missing tool;
        * **language priorities** → re-derive the default selection, which is
          exactly what a priority is for;
        * **splitting** → re-assess the target, because the badge and the V15
          warning depend on it;
        * **non-standard resolutions** → re-validate, because V4's severity (and
          with it Start's enabled-ness) depends on it.
        """
        before_tools = self._settings.tool_overrides()
        before_languages = (
            self._settings.audio_priority(),
            self._settings.subtitle_priority(),
        )
        before_split = (self._settings.split_enabled, self._settings.stored_split_size_mb())
        before_resolution = self._settings.allow_nonstandard_resolution
        dialog = PreferencesDialog(
            self._settings,
            self,
            tool_report=self._tool_report,
            tool_check=partial(self.check_tools, adopt=False),
            tab=tab,
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return False

        if self._settings.tool_overrides() != before_tools:
            self.check_tools()
        if (
            self._settings.audio_priority(),
            self._settings.subtitle_priority(),
        ) != before_languages:
            self._reselect_defaults()
        if (self._settings.split_enabled, self._settings.stored_split_size_mb()) != before_split:
            self._assess_output()
        if self._settings.allow_nonstandard_resolution != before_resolution:
            self._revalidate_report()
        return True

    def show_tool_versions(self) -> None:
        if self._tool_report is None:
            text = "External tools have not been checked yet."
            QtWidgets.QMessageBox.information(self, "External tools", text)
            return
        QtWidgets.QMessageBox.information(
            self, "External tools", "\n".join(self._tool_report.messages())
        )

    def show_about(self) -> None:
        paths = [
            f"log lines: {self.log_pane.text().count(chr(10))}",
            f"temp dir: {self._settings.temp_dir() or 'system temp'}",
            f"output: {self.output_dir() or 'not chosen'}",
        ]
        QtWidgets.QMessageBox.about(
            self,
            "About avchd-forge",
            f"<b>avchd-forge {__version__}</b><br>"
            "Remux AVC BDRemux releases into PS3-compatible AVCHD structures.<br><br>"
            + "<br>".join(paths),
        )

    # ------------------------------------------------------------------
    # window state
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        geometry = self._settings.geometry()
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self._settings.window_state()
        if state is not None:
            self.restoreState(state)
        self.log_pane.set_collapsed(not self._settings.log_visible)

    def _save_state(self) -> None:
        self._settings.set_geometry(self.saveGeometry())
        self._settings.set_window_state(self.saveState())
        self._settings.set_log_visible(not self.log_pane.collapsed)

    def _on_log_toggled(self, expanded: bool) -> None:
        self._settings.set_log_visible(expanded)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._save_state()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # drag and drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if drop_accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        if self.handle_drop(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def handle_drop(self, mime: QtCore.QMimeData) -> Path | None:
        """What a drop does, without the event: probe the file or refuse it.

        Split out from :meth:`dropEvent` because synthetic ``QDropEvent``s are
        exactly the kind of thing that crashes with the offscreen platform.
        """
        if not drop_accepts(mime):
            self.notify("Ignored the drop: expected exactly one .mkv/.ts file")
            return None
        path = dropped_file(mime)
        if path is None:  # pragma: no cover - drop_accepts already refused these
            return None
        self.open_source(path)
        return path


def _open_path(path: Path, parent: QtWidgets.QWidget) -> None:
    """Hand ``path`` to the desktop's file manager, or say why not."""
    if not path.exists():
        QtWidgets.QMessageBox.warning(parent, "Not found", f"{path} does not exist.")
        return
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))


__all__ = ["MainWindow", "drop_accepts", "dropped_file"]
