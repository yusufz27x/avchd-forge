"""Process execution abstractions.

The whole application talks to external tools through the :class:`Runner`
protocol. Two implementations exist:

* :class:`SubprocessRunner` (here) — plain ``subprocess``, used by the CLI and by
  tests; it never imports Qt.
* :class:`QProcessRunner` — Qt-based, streams output line by line and keeps the UI
  responsive. PySide6 is imported lazily, so this module still imports cleanly in a
  Qt-less environment.

Keeping the protocol tiny is what allows the core pipeline to be unit tested
without spawning anything.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

LineCallback = Callable[[str], None]
ProgressCallback = Callable[[float | None], None]

#: ANSI/terminal control sequences a tool may print around in-place status
#: output. tsMuxeR on Linux prefixes every ``NN.N% complete`` report after the
#: first one that follows a log message with ``ESC M`` — *reverse index*, the
#: ``METADemuxer::lineBack`` trick that makes a terminal overwrite the previous
#: report; CSI colour sequences would appear if a tool believed it was on a
#: terminal. A log pane has no cursor to move and the progress parsers match
#: from the start of the line, so the sequences are stripped at the boundary
#: where bytes become lines.
_ANSI_SEQUENCE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|[@-_])")


def strip_ansi_sequences(text: str) -> str:
    """Remove terminal control sequences; every printable byte is kept."""
    return _ANSI_SEQUENCE.sub("", text or "")


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

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` was called for the run in progress."""
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
            line = strip_ansi_sequences(line)
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


class _LineBuffer:
    """Accumulates one text stream and produces complete lines.

    Lines keep their trailing newline, exactly like iterating a text-mode stream in
    :class:`SubprocessRunner`, so callbacks see identical text from both runners.
    CRLF and lone CR are folded to ``\\n`` first (universal-newline behaviour); the
    only mismatch left is a CR at the very end of one read and a LF at the start of
    the next, which no tool in this pipeline produces.

    Terminal control sequences are stripped as the lines are formed: tsMuxeR
    prefixes its in-place progress reports with ``ESC M``, and neither the log
    pane nor the progress parsers want the cursor furniture (the collected
    ``CompletedRun`` text is assembled from these same lines).
    """

    __slots__ = ("_pending", "lines")

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._pending = ""

    def feed(self, text: str) -> list[str]:
        """Consume a chunk; return the complete lines it released."""
        self._pending += text.replace("\r\n", "\n").replace("\r", "\n")
        complete = self._pending.split("\n")
        *rows, self._pending = complete
        lines = [strip_ansi_sequences(f"{row}\n") for row in rows]
        self.lines.extend(lines)
        return lines

    def flush(self) -> list[str]:
        """Release a trailing line that never got its newline."""
        if not self._pending:
            return []
        tail, self._pending = strip_ansi_sequences(self._pending), ""
        self.lines.append(tail)
        return [tail]


def _import_qt() -> Any:
    try:
        from PySide6 import QtCore
    except ImportError as exc:  # pragma: no cover - only hit without PySide6
        raise RuntimeError(
            "QProcessRunner needs PySide6; use SubprocessRunner when Qt is unavailable"
        ) from exc
    return QtCore


class QProcessRunner:
    """Runs tools through ``QProcess``, streaming output while the UI paints.

    ``run()`` spins a nested :class:`QEventLoop`: the caller's stack stays intact,
    but paint, timer and keyboard events keep being delivered to the rest of the
    application, so the window never freezes. Signals are connected *before*
    ``start()`` because Qt delivers a failure-to-start synchronously or not long
    after, and a missed signal would mean a loop that never exits.

    Cancellation kills the child only — never the process group — so a cancelled
    mux cannot take the GUI down with it. Timeouts behave the same way and leave a
    bracketed note in ``stderr``. Any of crash, kill or failed start surfaces as a
    returncode of ``-1`` (raw exit codes for those are platform-dependent, so they
    are not a contract).
    """

    def __init__(
        self,
        *,
        echo: Callable[[str], None] | None = None,
        parent: Any | None = None,
    ) -> None:
        self.echo = echo
        self._parent = parent
        self._qt = _import_qt()
        self._process: Any | None = None
        self._active = False
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Kill the running child; safe to call when idle and from any thread."""
        self._cancelled = True
        process = self._process
        if process is not None and process.state() != self._qt.QProcess.NotRunning:
            process.kill()

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
        if self._active:
            raise RuntimeError("QProcessRunner is already running a command")

        qt = self._qt
        args = [str(item) for item in argv]
        stdout_buffer = _LineBuffer()
        stderr_buffer = _LineBuffer()
        state: dict[str, Any] = {"done": False, "start_error": None}

        def handle(line: str) -> None:
            # ``_LineBuffer`` already stripped terminal control sequences.
            if on_line is not None:
                on_line(line)
            elif self.echo is not None:
                self.echo(line)
            if scanner is not None and on_progress is not None:
                value = scanner.feed(line)
                if value is not None:
                    on_progress(value)

        process = qt.QProcess(self._parent)
        process.setProgram(args[0])
        process.setArguments(args[1:])
        if cwd is not None:
            process.setWorkingDirectory(str(cwd))

        loop = qt.QEventLoop()
        timer = qt.QTimer()
        timer.setSingleShot(True)

        def read_stdout() -> None:
            for line in stdout_buffer.feed(_decode(process.readAllStandardOutput())):
                handle(line)

        def read_stderr() -> None:
            for line in stderr_buffer.feed(_decode(process.readAllStandardError())):
                handle(line)

        def stalled() -> None:
            stderr_buffer.lines.append(f"[avchd-forge] command timed out after {timeout}s")
            if process.state() != qt.QProcess.NotRunning:
                process.kill()

        def mark_done(*_args: object) -> None:
            state["done"] = True
            loop.quit()

        def on_error(error: Any) -> None:
            if error == qt.QProcess.FailedToStart:
                state["start_error"] = f"[avchd-forge] failed to start: {process.errorString()}"
                mark_done()

        # Connections must exist before start(): a failure to start can arrive
        # synchronously, and a lost signal would leave the nested loop running.
        process.readyReadStandardOutput.connect(read_stdout)
        process.readyReadStandardError.connect(read_stderr)
        process.finished.connect(mark_done)
        process.errorOccurred.connect(on_error)
        timer.timeout.connect(stalled)

        self._active = True
        self._cancelled = False
        self._process = process
        try:
            process.start()
            if timeout is not None and timeout > 0:
                timer.start(int(timeout * 1000))
            if not state["done"]:
                loop.exec()
            timer.stop()
            # Whatever came in with the child's last breath.
            read_stdout()
            read_stderr()
            for line in stdout_buffer.flush() + stderr_buffer.flush():
                handle(line)
        finally:
            self._active = False
            self._process = None

        if state["start_error"] is not None:
            stderr_buffer.lines.append(str(state["start_error"]))
            returncode = -1
        elif process.exitStatus() == qt.QProcess.CrashExit:
            returncode = -1
        else:
            returncode = process.exitCode()

        return CompletedRun(
            argv=tuple(args),
            returncode=returncode,
            stdout="".join(stdout_buffer.lines),
            stderr="".join(stderr_buffer.lines),
        )


def _decode(data: Any) -> str:
    return bytes(data).decode("utf-8", "replace")


__all__ = [
    "CompletedRun",
    "LineCallback",
    "ProgressCallback",
    "ProgressScanner",
    "QProcessRunner",
    "Runner",
    "SubprocessRunner",
    "strip_ansi_sequences",
]
