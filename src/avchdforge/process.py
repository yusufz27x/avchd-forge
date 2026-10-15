"""Process execution abstractions.

The whole application talks to external tools through the :class:`Runner`
protocol. Two implementations exist:

* :class:`SubprocessRunner` (here) — plain ``subprocess``, used by the CLI and by
  tests; it never imports Qt.
* :class:`~avchdforge.qprocess.QProcessRunner` — Qt-based, streams output line by
  line and keeps the UI responsive.

Keeping the protocol tiny is what allows the core pipeline to be unit tested
without spawning anything.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

LineCallback = Callable[[str], None]
ProgressCallback = Callable[[float | None], None]


@dataclass(frozen=True, slots=True)
class CompletedRun:
    """Outcome of one external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """stdout and stderr concatenated (tsMuxeR and ffmpeg both use stderr)."""
        return self.stdout + self.stderr

    def tail(self, lines: int = 5) -> str:
        rows = [row for row in self.output.splitlines() if row.strip()]
        return "\n".join(rows[-lines:])


@runtime_checkable
class ProgressScanner(Protocol):
    """Turns one line of tool output into a fraction, or ``None``."""

    def feed(self, line: str) -> float | None:  # pragma: no cover - protocol
        ...


@runtime_checkable
class Runner(Protocol):
    """Executes an external command, reporting output as it arrives."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        on_line: LineCallback | None = None,
        on_progress: ProgressCallback | None = None,
        scanner: ProgressScanner | None = None,
        phase: str = "",
        timeout: float | None = None,
    ) -> CompletedRun:  # pragma: no cover - protocol
        ...

    def cancel(self) -> None:
        """Ask the running command to stop; safe to call when nothing runs."""
        ...


@dataclass
class SubprocessRunner:
    """Straight ``subprocess`` runner with streaming callbacks.

    Used for headless operation and in tests. Cancellation kills only the child
    process, never the group, so a cancelled mux cannot take the UI down with it.
    """

    echo: Callable[[str], None] | None = None
    _process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        on_line: LineCallback | None = None,
        on_progress: ProgressCallback | None = None,
        scanner: ProgressScanner | None = None,
        phase: str = "",
        timeout: float | None = None,
    ) -> CompletedRun:
        argv = [str(item) for item in argv]
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def handle(stream_name: str, line: str) -> None:
            target = stdout_lines if stream_name == "stdout" else stderr_lines
            target.append(line)
            if on_line is not None:
                on_line(line)
            elif self.echo is not None:
                self.echo(line)
            if scanner is not None and on_progress is not None:
                value = scanner.feed(line)
                if value is not None:
                    on_progress(value)

        process = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._lock:
            self._process = process
        try:
            readers = [
                threading.Thread(
                    target=self._pump, args=(process.stdout, "stdout", handle), daemon=True
                ),
                threading.Thread(
                    target=self._pump, args=(process.stderr, "stderr", handle), daemon=True
                ),
            ]
            for reader in readers:
                reader.start()
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
                stderr_lines.append(f"[avchd-forge] command timed out after {timeout}s")
            for reader in readers:
                reader.join(timeout=5)
        finally:
            with self._lock:
                self._process = None

        return CompletedRun(
            argv=tuple(argv),
            returncode=returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )

    @staticmethod
    def _pump(stream, name: str, handle: Callable[[str, str], None]) -> None:
        if stream is None:  # pragma: no cover - defensive
            return
        try:
            for line in stream:
                handle(name, line)
        finally:
            stream.close()


__all__ = [
    "CompletedRun",
    "LineCallback",
    "ProgressCallback",
    "ProgressScanner",
    "Runner",
    "SubprocessRunner",
]
