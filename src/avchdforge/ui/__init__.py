"""The Qt layer: everything that needs a screen.

``main_window`` is the only widget the application instantiates directly;
``track_view``, ``log_pane``, ``preferences`` and ``tool_gate`` are its parts.
Importing this package pulls PySide6 in, which is why nothing outside it does:

    >>> from avchdforge.ui.main_window import MainWindow

The core modules (``probe``, ``validate``, ``plan``, …) stay importable without
Qt so the test suite runs headless and fast (decision D6).
"""

from __future__ import annotations

__all__: list[str] = []
