"""Unit tests for the console entry point.

Only the argument parsing is tested: ``main`` would start an event loop, and
everything it does after that is covered by the window tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="__main__ builds a QApplication")

from avchdforge.__main__ import parse_args


def test_no_arguments_means_an_empty_window() -> None:
    args = parse_args([])

    assert args.file is None
    assert args.output is None


def test_a_source_and_an_output_can_be_preselected() -> None:
    args = parse_args(["movie.mkv", "--output", "/media/usb"])

    assert args.file == "movie.mkv"
    assert args.output == "/media/usb"


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--help"])

    assert exit_info.value.code == 0
    assert "avchd-forge" in capsys.readouterr().out


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    from avchdforge import __version__

    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out
