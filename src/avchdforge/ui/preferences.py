"""The Preferences dialog: every :class:`~avchdforge.settings.AppSettings` key.

The dialog is a straight editor — load on open, write on OK — and it persists
nothing itself; ``AppSettings`` is the only writer, which keeps "what is stored"
and "what the widgets show" in one place that the tests can drive without a
modal event loop (:meth:`PreferencesDialog.apply` and
:meth:`PreferencesDialog.reload` are public for exactly that reason).

Two consequences of "write on OK" that are worth keeping:

* ``Check now`` is the one live part: it re-runs the tool check with the paths
  *currently typed in the boxes* (not the saved ones) so a typo is visible before
  OK, and it does not persist anything — and because the check is handed to the
  window with ``adopt=False``, a typo cannot disable Start either.
* The split size is read and written through
  :meth:`~avchdforge.settings.AppSettings.stored_split_size_mb`, not
  ``split_size_mb()``: the box is disabled while splitting is off, and editing the
  *effective* value (which is ``None`` there) would reset a custom size on every
  open-and-OK.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6 import QtWidgets

from ..backend import FAT32_MAX_BYTES, SPLIT_MB_BYTES, SPLIT_OVERSHOOT_ALLOWANCE
from ..constants import DEFAULT_FONT_SIZE
from ..errors import AvchdForgeError
from ..prep import SubtitleStyle
from ..settings import AppSettings
from ..tooling import FFMPEG, FFPROBE, TSMUXER, ToolReport

#: Manager for a tool check with explicit overrides (``MainWindow.check_tools``).
ToolChecker = Callable[[Mapping[str, Path] | None], ToolReport]

STATUS_OK = "color: #1e8449;"
STATUS_WARN = "color: #9a6b00;"
STATUS_BAD = "color: #c0392b;"

#: Largest split target that still cannot produce an illegal chunk. tsMuxeR's "MB"
#: is 10^6 bytes and it splits at the first keyframe *after* the target, so a chunk
#: may overshoot by a GOP: what is left of the FAT32 ceiling is the real maximum,
#: and the 4096 MB default sits comfortably below it.
MAX_SPLIT_SIZE_MB = (FAT32_MAX_BYTES - SPLIT_OVERSHOOT_ALLOWANCE) // SPLIT_MB_BYTES

#: Tab order: ``(name for show_tab, label)``, so the two cannot drift apart.
TABS: tuple[tuple[str, str], ...] = (
    ("paths", "Paths"),
    ("tools", "External tools"),
    ("muxing", "Muxing"),
    ("languages", "Languages"),
)
TAB_INDEX = {name: index for index, (name, _label) in enumerate(TABS)}


class PreferencesDialog(QtWidgets.QDialog):
    """Modal editor for the user's preferences."""

    def __init__(
        self,
        settings: AppSettings,
        parent: QtWidgets.QWidget | None = None,
        *,
        tool_report: ToolReport | None = None,
        tool_check: ToolChecker | None = None,
        tab: str = "",
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._tool_report = tool_report
        self._tool_check = tool_check
        self.tool_edits: dict[str, QtWidgets.QLineEdit] = {}
        self.tool_status: dict[str, QtWidgets.QLabel] = {}

        self.setWindowTitle("Preferences")
        self.setModal(True)

        self.tabs = QtWidgets.QTabWidget(self)
        builders = (self._paths_tab, self._tools_tab, self._mux_tab, self._languages_tab)
        for (_name, label), builder in zip(TABS, builders, strict=True):
            self.tabs.addTab(builder(self.tabs), label)
        if tab:
            self.show_tab(tab)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.restore_button = buttons.addButton(
            "Restore defaults", QtWidgets.QDialogButtonBox.ButtonRole.ResetRole
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.restore_button.clicked.connect(self.restore_defaults)

        self.resize(720, 460)
        self.reload()
        self.set_tool_report(tool_report)

    # ------------------------------------------------------------------
    # tabs
    # ------------------------------------------------------------------

    def show_tab(self, name: str) -> None:
        """Bring one tab forward: ``"tools"`` is what the startup gate asks for."""
        index = TAB_INDEX.get(name)
        if index is None:
            raise KeyError(f"unknown tab {name!r}; expected one of {sorted(TAB_INDEX)}")
        self.tabs.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # tabs
    # ------------------------------------------------------------------

    def _paths_tab(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(tab)

        layout.addWidget(QtWidgets.QLabel("Temporary files", tab))
        row = QtWidgets.QHBoxLayout()
        self.temp_edit = QtWidgets.QLineEdit(tab)
        self.temp_edit.setPlaceholderText(tempfile.gettempdir())
        browse = QtWidgets.QPushButton("Browse…", tab)
        browse.clicked.connect(self.choose_temp_dir)
        row.addWidget(self.temp_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        layout.addWidget(
            QtWidgets.QLabel(
                f"Empty means the system temp directory ({tempfile.gettempdir()}). "
                "A fast local disk is best; the mux cleans up after itself.",
                tab,
            )
        )

        self.keep_success_check = QtWidgets.QCheckBox(
            "Keep temporary files after a successful run", tab
        )
        self.keep_failure_check = QtWidgets.QCheckBox(
            "Keep temporary files when a run fails (evidence for bug reports)", tab
        )
        layout.addWidget(self.keep_success_check)
        layout.addWidget(self.keep_failure_check)
        layout.addStretch(1)
        return tab

    def _tools_tab(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget(parent)
        layout = QtWidgets.QGridLayout(tab)
        layout.addWidget(
            QtWidgets.QLabel(
                "An explicit path wins over PATH and is never silently replaced. "
                "Leave a box empty to search as usual.",
                tab,
            ),
            0,
            0,
            1,
            3,
        )
        for row, name in enumerate((TSMUXER, FFMPEG, FFPROBE), start=1):
            layout.addWidget(QtWidgets.QLabel(name, tab), row, 0)
            edit = QtWidgets.QLineEdit(tab)
            edit.setPlaceholderText("found on PATH")
            browse = QtWidgets.QPushButton("Browse…", tab)
            browse.clicked.connect(lambda _checked=False, tool=name: self.choose_tool(tool))
            status = QtWidgets.QLabel(tab)
            layout.addWidget(edit, row, 1)
            layout.addWidget(browse, row, 2)
            layout.addWidget(status, row, 3)
            self.tool_edits[name] = edit
            self.tool_status[name] = status
        layout.setColumnStretch(1, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.check_button = QtWidgets.QPushButton("Check now", tab)
        self.check_button.clicked.connect(self.check_now)
        buttons.addWidget(self.check_button)
        buttons.addStretch(1)
        layout.addLayout(buttons, 4, 0, 1, 4)
        layout.setRowStretch(5, 1)
        return tab

    def _mux_tab(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(tab)

        self.split_check = QtWidgets.QCheckBox(
            "Split the output at the FAT32 size limit (recommended for USB sticks)", tab
        )
        self.split_check.toggled.connect(self._update_split_enabled)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Split size", tab))
        self.split_spin = QtWidgets.QSpinBox(tab)
        self.split_spin.setRange(1, MAX_SPLIT_SIZE_MB)
        self.split_spin.setSuffix(" MB")
        self.split_spin.setToolTip(
            f"Chunks are targets, not limits: tsMuxeR closes the chunk after the "
            f"next keyframe, so {MAX_SPLIT_SIZE_MB} MB is as high as this can go "
            f"and still stay under the 4 GiB a FAT32 file can hold."
        )
        row.addWidget(self.split_spin)
        row.addStretch(1)
        layout.addWidget(self.split_check)
        layout.addLayout(row)

        layout.addWidget(QtWidgets.QLabel("tsMuxeR subtitle renderer (SRT/ASS → PGS)", tab))
        form = QtWidgets.QFormLayout()
        self.font_name_edit = QtWidgets.QLineEdit(tab)
        self.font_size_spin = QtWidgets.QSpinBox(tab)
        self.font_size_spin.setRange(8, 200)
        self.font_color_edit = QtWidgets.QLineEdit(tab)
        self.font_color_edit.setPlaceholderText("0xEBFFFFFF")
        self.bottom_offset_spin = QtWidgets.QSpinBox(tab)
        self.bottom_offset_spin.setRange(0, 500)
        self.bottom_offset_spin.setSuffix(" px")
        form.addRow("Font", self.font_name_edit)
        form.addRow("Font size", self.font_size_spin)
        form.addRow("Font colour", self.font_color_edit)
        form.addRow("Bottom offset", self.bottom_offset_spin)
        layout.addLayout(form)
        layout.addWidget(
            QtWidgets.QLabel("Empty font or colour leaves the choice to tsMuxeR.", tab)
        )
        layout.addStretch(1)
        return tab

    def _languages_tab(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(tab)
        self.audio_priority_edit = QtWidgets.QLineEdit(tab)
        self.audio_priority_edit.setPlaceholderText("eng, jpn")
        self.subtitle_priority_edit = QtWidgets.QLineEdit(tab)
        self.subtitle_priority_edit.setPlaceholderText("eng")
        layout.addRow("Preferred audio languages", self.audio_priority_edit)
        layout.addRow("Preferred subtitle languages", self.subtitle_priority_edit)
        layout.addRow(
            QtWidgets.QLabel(
                "Comma-separated ISO 639-2 codes, most preferred first. They only "
                "decide which tracks start ticked; nothing is ever removed.",
                tab,
            )
        )
        return tab

    # ------------------------------------------------------------------
    # load / save
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Fill every widget from the stored preferences."""
        temp = self._settings.temp_dir()
        self.temp_edit.setText(str(temp) if temp else "")
        self.keep_success_check.setChecked(self._settings.keep_temp_on_success)
        self.keep_failure_check.setChecked(self._settings.keep_temp_on_failure)

        for name, edit in self.tool_edits.items():
            path = self._settings.tool_path(name)
            edit.setText(str(path) if path else "")

        self.split_check.setChecked(self._settings.split_enabled)
        self.split_spin.setValue(self._settings.stored_split_size_mb())
        self._update_split_enabled()

        style = self._settings.subtitle_style()
        self.font_name_edit.setText(style.font_name or "")
        self.font_size_spin.setValue(style.font_size or DEFAULT_FONT_SIZE)
        self.font_color_edit.setText(style.font_color or "")
        self.bottom_offset_spin.setValue(style.bottom_offset or 0)

        self.audio_priority_edit.setText(", ".join(self._settings.audio_priority()))
        self.subtitle_priority_edit.setText(", ".join(self._settings.subtitle_priority()))

    def apply(self) -> None:
        """Write every widget back into the stored preferences."""
        temp_text = self.temp_edit.text().strip()
        self._settings.set_temp_dir(Path(temp_text) if temp_text else None)
        self._settings.set_keep_temp_on_success(self.keep_success_check.isChecked())
        self._settings.set_keep_temp_on_failure(self.keep_failure_check.isChecked())

        for name, edit in self.tool_edits.items():
            text = edit.text().strip()
            self._settings.set_tool_path(name, Path(text) if text else None)

        self._settings.set_split_enabled(self.split_check.isChecked())
        self._settings.set_split_size_mb(self.split_spin.value())
        self._settings.set_subtitle_style(
            SubtitleStyle(
                font_name=self.font_name_edit.text().strip() or None,
                font_size=self.font_size_spin.value(),
                font_color=self.font_color_edit.text().strip() or None,
                bottom_offset=self.bottom_offset_spin.value(),
            )
        )
        self._settings.set_audio_priority(self.audio_priority_edit.text())
        self._settings.set_subtitle_priority(self.subtitle_priority_edit.text())

    def restore_defaults(self) -> None:
        """Forget every preference (geometry and log visibility included)."""
        answer = QtWidgets.QMessageBox.question(
            self,
            "Restore defaults",
            "Reset every preference, including tool paths and window geometry?",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._settings.clear()
        self.reload()
        self.set_tool_report(None)

    def accept(self) -> None:
        self.apply()
        super().accept()

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    def check_now(self) -> ToolReport | None:
        """Re-check the tools with the paths in the boxes (nothing is stored)."""
        if self._tool_check is None:
            return None
        overrides = {
            name: Path(text)
            for name, edit in self.tool_edits.items()
            if (text := edit.text().strip())
        }
        try:
            report = self._tool_check(overrides)
        except AvchdForgeError as exc:  # a broken query must not kill the dialog
            self._set_tool_status_message(str(exc))
            return None
        self.set_tool_report(report)
        return report

    def set_tool_report(self, report: ToolReport | None) -> None:
        """Show each tool's version and path (or why it was not found)."""
        self._tool_report = report
        if report is None:
            for label in self.tool_status.values():
                label.setText("not checked yet")
                label.setStyleSheet("")
            return
        for tool in report.tools:
            label = self.tool_status.get(tool.name)
            if label is None:  # pragma: no cover - the report uses our names
                continue
            if not tool.found:
                label.setText(tool.reason or "not found")
                label.setStyleSheet(STATUS_BAD)
            elif tool.warning:
                label.setText(tool.warning)
                label.setStyleSheet(STATUS_WARN)
            else:
                label.setText(f"{tool.version or 'version unknown'} — {tool.path}")
                label.setStyleSheet(STATUS_OK)

    def _set_tool_status_message(self, text: str) -> None:
        for label in self.tool_status.values():
            label.setText(text)
            label.setStyleSheet(STATUS_BAD)

    # ------------------------------------------------------------------
    # pickers
    # ------------------------------------------------------------------

    def choose_temp_dir(self) -> Path | None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose the temporary directory", self.temp_edit.text()
        )
        if chosen:
            self.temp_edit.setText(chosen)
            return Path(chosen)
        return None

    def choose_tool(self, name: str) -> Path | None:
        edit = self.tool_edits[name]
        chosen, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, f"Choose the {name} executable", edit.text()
        )
        if chosen:
            edit.setText(chosen)
            return Path(chosen)
        return None

    def _update_split_enabled(self) -> None:
        self.split_spin.setEnabled(self.split_check.isChecked())


__all__ = [
    "MAX_SPLIT_SIZE_MB",
    "STATUS_BAD",
    "STATUS_OK",
    "STATUS_WARN",
    "TABS",
    "TAB_INDEX",
    "PreferencesDialog",
]
