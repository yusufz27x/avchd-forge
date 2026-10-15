"""The startup tool gate: what is missing, and what to do about it.

avchd-forge cannot probe a source or write an AVCHD tree without tsMuxeR, ffmpeg
and ffprobe, so the application refuses to start until they resolve — or the user
explicitly chooses to continue anyway. *Continue* is only offered when the report
is usable: an old-but-working ffmpeg warns and may be continued past, a missing
tsMuxeR may not, because there is nothing to mux with.

Two deliberate choices:

* :func:`gate_text` and :func:`searched_text` are pure strings, so the wording is
  tested without a widget and the dialog is only a frame around them. The full
  list of places a tool was looked for is long (every ``PATH`` name plus the
  install directories) and goes into the details area, not the headline.
* :meth:`ToolGateDialog.ask` returns a plain string (:data:`RECHECK`,
  :data:`PREFERENCES`, :data:`CONTINUE`, :data:`QUIT`) and nothing else, which is
  what lets ``MainWindow.ensure_tools`` drive the loop without a widget and the
  tests drive the loop without a modal event loop. Closing the dialog any other
  way (Esc, the window manager) counts as *quit*: guessing "continue" on a broken
  toolchain is how a user ends up with a half-written disc and no explanation.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..tooling import ToolInfo, ToolReport

#: The four answers, as strings — the caller only branches on them.
RECHECK = "recheck"
PREFERENCES = "preferences"
CONTINUE = "continue"
QUIT = "quit"

#: Leader of the dialog: what avchd-forge is, and what it cannot do without.
HEADLINE = (
    "avchd-forge remuxes with tsMuxeR, ffmpeg and ffprobe; without all three it "
    "can neither read a source nor write an AVCHD structure."
)


def gate_text(report: ToolReport) -> str:
    """The gate's main text: one line per tool, the warnings, and what to do."""
    lines = [HEADLINE, ""]
    lines.extend(_tool_line(tool) for tool in report.tools)
    if report.warnings:
        lines.append("")
        lines.extend(f"warning: {warning}" for warning in report.warnings)
    lines.append("")
    lines.append(_advice(report))
    return "\n".join(lines)


def searched_text(report: ToolReport) -> str:
    """Where each *missing* tool was looked for (the dialog's details area)."""
    blocks: list[str] = []
    for tool in report.missing:
        blocks.append("\n".join([f"{tool.name}:", *(f"    {item}" for item in tool.searched)]))
    return "\n\n".join(blocks)


def _tool_line(tool: ToolInfo) -> str:
    if tool.found:
        return tool.describe()
    return f"{tool.name}: not found" + (f" — {tool.reason}" if tool.reason else "")


def _advice(report: ToolReport) -> str:
    if report.missing:
        return (
            "Install the missing tool, or set its path in Preferences, then choose "
            "“Re-check”. Start stays disabled until every tool resolves."
        )
    return (
        "Nothing is missing — the warnings above only disable the features they "
        "name, so the rest of the application works as usual."
    )


class ToolGateDialog(QtWidgets.QMessageBox):
    """The startup refusal, with the four ways out.

    ``Preferences…`` is the "set the path" shortcut: it is where the tool paths
    live, with a per-tool browse button and a check that does not save anything.
    """

    def __init__(self, report: ToolReport, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._report = report
        self.setWindowTitle("External tools")
        self.setIcon(
            QtWidgets.QMessageBox.Icon.Critical
            if report.missing
            else QtWidgets.QMessageBox.Icon.Warning
        )
        # Paths and search lists are data, not markup: never render them as HTML.
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.setText(gate_text(report))
        details = searched_text(report)
        if details:
            self.setDetailedText(details)

        self.recheck_button = self.addButton(
            "Re-check", QtWidgets.QMessageBox.ButtonRole.ActionRole
        )
        self.preferences_button = self.addButton(
            "Preferences…", QtWidgets.QMessageBox.ButtonRole.ActionRole
        )
        self.continue_button: QtWidgets.QPushButton | None = None
        if report.ok:
            self.continue_button = self.addButton(
                "Continue", QtWidgets.QMessageBox.ButtonRole.AcceptRole
            )
        self.quit_button = self.addButton("Quit", QtWidgets.QMessageBox.ButtonRole.RejectRole)

        self._choices: dict[QtWidgets.QPushButton, str] = {
            self.recheck_button: RECHECK,
            self.preferences_button: PREFERENCES,
            self.quit_button: QUIT,
        }
        if self.continue_button is not None:
            self._choices[self.continue_button] = CONTINUE
            self.setDefaultButton(self.continue_button)
        else:
            self.setDefaultButton(self.recheck_button)
        self.setEscapeButton(self.quit_button)

    @property
    def report(self) -> ToolReport:
        return self._report

    def choice(self) -> str:
        """What the user picked; anything that is not a button means :data:`QUIT`."""
        return self._choices.get(self.clickedButton(), QUIT)

    @classmethod
    def ask(cls, report: ToolReport, parent: QtWidgets.QWidget | None = None) -> str:
        """Show the gate and return the choice — the window's one modal startup call."""
        dialog = cls(report, parent)
        dialog.exec()
        return dialog.choice()


__all__ = [
    "CONTINUE",
    "HEADLINE",
    "PREFERENCES",
    "QUIT",
    "RECHECK",
    "ToolGateDialog",
    "gate_text",
    "searched_text",
]
