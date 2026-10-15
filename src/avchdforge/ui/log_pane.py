"""The collapsible log pane.

Every line the tools print ends up here: monospace, read-only, no line wrapping
(commands are long), and with a *pause* toggle so a user can read a failure while
a job keeps printing. ``Save log…`` and ``Copy`` are one click away because the
first thing anyone does with a failed run is paste it somewhere.

The widget is deliberately dumb: it never runs a process and never decides what
to keep. ``MainWindow`` owns the file dialogs; :meth:`LogPane.save_to` takes a
path so it can be tested without one.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

#: Lines kept in the pane. A whole 30 GB mux prints far fewer than this, and the
#: cap keeps a runaway process from growing the widget without limit.
MAX_BLOCKS = 20_000

MONOSPACE_FAMILIES = ("Cascadia Mono", "JetBrains Mono", "DejaVu Sans Mono", "Consolas",
                      "Liberation Mono", "monospace")


class LogPane(QtWidgets.QWidget):
    """Output pane with a collapse toggle, a pause switch and export actions."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._paused = False

        self.toggle_button = QtWidgets.QToolButton(self)
        self.toggle_button.setText("Log")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.toggle_button.setArrowType(QtCore.Qt.ArrowType.DownArrow)
        self.toggle_button.setAutoRaise(True)

        self.pause_button = QtWidgets.QToolButton(self)
        self.pause_button.setText("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip("Stop following the output (the job keeps running)")
        self.clear_button = QtWidgets.QToolButton(self)
        self.clear_button.setText("Clear")
        self.save_button = QtWidgets.QToolButton(self)
        self.save_button.setText("Save log…")
        self.copy_button = QtWidgets.QToolButton(self)
        self.copy_button.setText("Copy")

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(self.toggle_button)
        header.addWidget(self._stage_label(""))
        header.addStretch(1)
        for button in (self.pause_button, self.clear_button, self.save_button,
                       self.copy_button):
            header.addWidget(button)

        self.view = QtWidgets.QPlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setUndoRedoEnabled(False)
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setMaximumBlockCount(MAX_BLOCKS)
        self.view.setFont(self._monospace_font())
        self.view.setPlaceholderText("Tool output appears here.")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)

        self.toggle_button.toggled.connect(self._apply_collapsed)
        self.pause_button.toggled.connect(self.set_paused)
        self.clear_button.clicked.connect(self.clear)
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        self._apply_collapsed(True)

    # -- content ---------------------------------------------------------

    def append(self, text: str) -> None:
        """Show ``text`` as it arrives; ignored (not queued) while paused."""
        if self._paused or not text:
            return
        cursor = self.view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if self.pause_button.isChecked():  # pragma: no cover - paused returns above
            return
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def append_lines(self, lines: list[str]) -> None:
        for line in lines:
            self.append(line if line.endswith("\n") else f"{line}\n")

    def text(self) -> str:
        return self.view.toPlainText()

    def clear(self) -> None:
        self.view.clear()

    def copy_to_clipboard(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.text())

    def save_to(self, path: Path | str) -> Path:
        """Write the current contents to ``path`` and return it."""
        target = Path(path)
        target.write_text(self.text(), encoding="utf-8")
        return target

    # -- pause / collapse ------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setChecked(paused)
        if not paused:
            self.view.verticalScrollBar().setValue(
                self.view.verticalScrollBar().maximum()
            )

    @property
    def collapsed(self) -> bool:
        return not self.toggle_button.isChecked()

    def set_collapsed(self, collapsed: bool) -> None:
        self.toggle_button.setChecked(not collapsed)

    def _apply_collapsed(self, expanded: bool) -> None:
        self.view.setVisible(expanded)
        self.toggle_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
        )
        for button in (self.pause_button, self.clear_button, self.save_button,
                       self.copy_button):
            button.setVisible(expanded)

    # -- header ----------------------------------------------------------

    def set_stage(self, label: str) -> None:
        """A one-word reminder in the header of what the job is doing."""
        self._stage_label_widget.setText(label)

    def _stage_label(self, text: str) -> QtWidgets.QLabel:
        self._stage_label_widget = QtWidgets.QLabel(text, self)
        self._stage_label_widget.setStyleSheet("color: palette(mid);")
        return self._stage_label_widget

    @staticmethod
    def _monospace_font() -> QtGui.QFont:
        font = QtGui.QFont()
        for family in MONOSPACE_FAMILIES:
            if family in QtGui.QFontDatabase.families():
                font.setFamily(family)
                break
        font.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        font.setFixedPitch(True)
        font.setPointSize(max(8, QtWidgets.QApplication.font().pointSize() - 1))
        return font


__all__ = ["MAX_BLOCKS", "MONOSPACE_FAMILIES", "LogPane"]
