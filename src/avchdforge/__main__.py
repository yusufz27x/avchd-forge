"""``avchd-forge [file] [--output DIR]`` — the console entry point.

The window is the program; these options only pre-fill it, which keeps the
whole GUI scriptable (``avchd-forge movie.mkv --output /media/usb``) and gives
``--help`` a sensible answer. Startup order matters:

1. ``QApplication`` first — every widget, every ``QSettings`` read and the
   offscreen platform plugin need it;
2. the window, which restores its geometry from the preferences;
3. the tool gate (:meth:`~avchdforge.ui.main_window.MainWindow.ensure_tools`) —
   tsMuxeR/ffmpeg/ffprobe have to resolve before anything can be read or muxed,
   and this is where the user may fix them or choose to quit;
4. the drive list, so the picker knows what is mounted;
5. only then the optional source, because probing is the one slow step.

Quitting at the gate exits ``1``: nothing was done, and scripts that start the
GUI should be able to tell that apart from a normal close.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6 import QtWidgets

from .settings import APPLICATION, ORGANIZATION

#: Exit code when the startup tool gate was quit (the window never opened).
GATE_QUIT_EXIT_CODE = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Pure argument parsing (tested without starting a QApplication)."""
    parser = argparse.ArgumentParser(
        prog="avchd-forge",
        description=(
            "Remux an AVC BDRemux (.mkv/.ts) into a PS3-compatible AVCHD "
            "BDMV/ structure. Video is never re-encoded."
        ),
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="source .mkv/.ts to open (and probe) on startup",
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        help="output folder to preselect (usually the FAT32 USB drive)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_version()}",
    )
    return parser.parse_args(argv)


def _version() -> str:
    from . import __version__

    return __version__


def main(argv: list[str] | None = None) -> int:
    """Build the window and run the event loop; returns the exit code."""
    args = parse_args(sys.argv[1:] if argv is None else argv)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(APPLICATION)
    app.setOrganizationName(ORGANIZATION)

    from .ui.main_window import MainWindow

    window = MainWindow()
    if not window.ensure_tools():
        return GATE_QUIT_EXIT_CODE
    window.refresh_drives()
    if args.output:
        window.set_output_dir(Path(args.output).expanduser())
    window.show()
    if args.file:
        window.open_source(Path(args.file).expanduser())
    return app.exec()


if __name__ == "__main__":  # pragma: no cover - module run as a script
    raise SystemExit(main())
