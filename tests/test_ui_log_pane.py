"""Unit tests for the collapsible log pane.

The pane is deliberately dumb — no process, no file dialogs — so these tests
only exercise widget state: appending, pausing, collapsing and exporting.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="ui/log_pane.py is Qt")
pytest.importorskip("pytestqt", reason="the ui tests use pytest-qt")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from avchdforge.ui.log_pane import MAX_BLOCKS, LogPane

from .conftest import dark_palette, text_contrast

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def pane() -> LogPane:
    return LogPane()


def test_append_shows_text_as_it_arrives(pane: LogPane) -> None:
    pane.append("first\n")
    pane.append("second\n")

    assert pane.text() == "first\nsecond\n"


def test_append_without_newline_keeps_the_stream_continuous(pane: LogPane) -> None:
    pane.append("progress 10%")
    pane.append(" 20%\n")

    assert pane.text() == "progress 10% 20%\n"


def test_empty_text_is_a_no_op(pane: LogPane) -> None:
    pane.append("")

    assert pane.text() == ""


def test_append_lines_adds_missing_newlines(pane: LogPane) -> None:
    pane.append_lines(["one", "two\n"])

    assert pane.text() == "one\ntwo\n"


def test_pausing_drops_lines_instead_of_queueing_them(pane: LogPane) -> None:
    pane.set_paused(True)
    pane.append("while paused\n")

    assert pane.paused
    assert pane.text() == ""

    pane.set_paused(False)
    pane.append("after resume\n")

    assert pane.text() == "after resume\n"


def test_pause_button_drives_the_state(pane: LogPane) -> None:
    pane.pause_button.setChecked(True)

    assert pane.paused


def test_clear_empties_the_pane(pane: LogPane) -> None:
    pane.append("something\n")

    pane.clear()

    assert pane.text() == ""


def test_collapse_hides_the_view_and_the_buttons(pane: LogPane) -> None:
    assert not pane.collapsed

    pane.set_collapsed(True)

    assert pane.collapsed
    assert pane.view.isHidden()
    assert pane.save_button.isHidden()
    assert pane.pause_button.isHidden()

    pane.set_collapsed(False)

    assert not pane.collapsed
    assert not pane.view.isHidden()
    assert not pane.save_button.isHidden()


def test_toggle_button_drives_the_collapse_state(pane: LogPane) -> None:
    pane.toggle_button.setChecked(False)

    assert pane.collapsed


def test_save_to_writes_the_current_contents(pane: LogPane, tmp_path: Path) -> None:
    pane.append("line one\nline two\n")

    written = pane.save_to(tmp_path / "run.log")

    assert written == tmp_path / "run.log"
    assert written.read_text(encoding="utf-8") == "line one\nline two\n"


def test_copy_puts_the_contents_on_the_clipboard(pane: LogPane) -> None:
    pane.append("paste me\n")
    QtWidgets.QApplication.clipboard().clear()

    pane.copy_to_clipboard()

    assert QtWidgets.QApplication.clipboard().text() == "paste me\n"


def test_stage_label_shows_what_the_job_is_doing(pane: LogPane) -> None:
    pane.set_stage("muxing")

    assert pane._stage_label_widget.text() == "muxing"


def test_the_stage_label_stays_readable_on_a_dark_palette(pane: LogPane, qapp) -> None:
    pane.set_stage("muxing")
    original = qapp.palette()
    qapp.setPalette(dark_palette())
    try:
        # ``palette(mid)`` on Breeze-Dark-shaped colours: text == background.
        assert text_contrast(pane._stage_label_widget) > 3
    finally:
        qapp.setPalette(original)


def test_the_pane_is_capped_so_a_runaway_tool_cannot_grow_it_forever(pane: LogPane) -> None:
    assert pane.view.maximumBlockCount() == MAX_BLOCKS
    assert pane.view.isReadOnly()
    assert pane.view.lineWrapMode() == QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
