"""Exception hierarchy for avchd-forge.

Every failure that a user could plausibly act on gets its own type carrying the
context needed to render a useful message (which tool, which command, what the
tool said). The GUI catches :class:`AvchdForgeError` and shows ``str(exc)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .validate_types import Issue, ValidationReport


class AvchdForgeError(Exception):
    """Base class for all errors raised by avchd-forge."""


class ToolNotFoundError(AvchdForgeError):
    """An external tool is missing or not executable."""

    def __init__(self, name: str, searched: Sequence[str], reason: str = "") -> None:
        self.name = name
        self.searched = list(searched)
        self.reason = reason
        detail = reason or "not found on PATH"
        where = ", ".join(self.searched) if self.searched else "(no candidates)"
        super().__init__(f"{name} is unavailable ({detail}). Looked at: {where}")


class ToolExecutionError(AvchdForgeError):
    """An external tool failed (non-zero exit) or could not be started."""

    def __init__(
        self,
        tool: str,
        argv: Sequence[str],
        returncode: int | None,
        stderr: str = "",
        stdout: str = "",
    ) -> None:
        self.tool = tool
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        tail = _last_lines(stderr or stdout, 5)
        super().__init__(
            f"{tool} failed with exit code {returncode}.\n"
            f"Command: {format_argv(self.argv)}\n"
            f"Output (tail):\n{tail}"
        )


class ProbeError(ToolExecutionError):
    """ffprobe could not read the input file."""


class PlanError(AvchdForgeError):
    """A job plan cannot be built from the media, selection and detection run.

    Reserved for broken invariants (validation runs first and turns user-facing
    problems into a :class:`ValidationFailed`).
    """


class TrackMappingError(AvchdForgeError):
    """ffprobe streams could not be matched to tsMuxeR Track IDs.

    tsMuxeR exits 0 with wrong output (or segfaults) when a ``track=`` index is
    wrong, so an unmappable stream is a hard failure rather than a guess.
    """


class ValidationFailed(AvchdForgeError):
    """Media or selection validation produced at least one error."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        joined = "\n".join(f"  - {issue.message}" for issue in report.errors)
        super().__init__(f"Input is not usable:\n{joined}")


class ExistingTreeError(AvchdForgeError):
    """The output folder already contains an AVCHD structure.

    tsMuxeR writes fixed names (``index.bdmv``, ``00000.m2ts``, …), so muxing
    into a folder that already holds a tree would overwrite the first title.
    """

    def __init__(self, output_dir: Path, marker: Path) -> None:
        self.output_dir = output_dir
        self.marker = marker
        super().__init__(
            f"{output_dir} already contains an AVCHD structure ({marker}). "
            f"Muxing here would overwrite it — choose an empty folder for each "
            f"title, or delete the tree to retry."
        )


class PrepError(ToolExecutionError):
    """An ffmpeg preparation step (core extraction, subtitle conversion) failed."""

    def __init__(
        self,
        tool: str,
        argv: Sequence[str],
        returncode: int | None,
        stderr: str = "",
        stdout: str = "",
        step: str = "",
    ) -> None:
        self.step = step
        super().__init__(tool, argv, returncode, stderr, stdout)


class MuxError(ToolExecutionError):
    """tsMuxeR failed to produce output."""


class VerificationError(AvchdForgeError):
    """tsMuxeR exited 0 but the output does not match the plan.

    tsMuxeR 2.7.0 reports success while silently dropping tracks whose
    ``track=`` index is wrong, so muxing is only considered successful after the
    produced streams have been inspected.
    """

    def __init__(self, problems: Sequence[str], output_dir: Path | None = None) -> None:
        self.problems = list(problems)
        self.output_dir = output_dir
        where = f" in {output_dir}" if output_dir else ""
        joined = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(f"Output verification failed{where}:\n{joined}")


class JobCancelled(AvchdForgeError):
    """Raised internally when the user aborts a job; not an error condition."""

    def __init__(self, stage: str = "") -> None:
        self.stage = stage
        super().__init__(f"Job cancelled{f' during {stage}' if stage else ''}")


def format_argv(argv: Sequence[str]) -> str:
    """Render a command line the way it should be logged (shell-quoted)."""
    out: list[str] = []
    for item in argv:
        if item == "" or any(ch in item for ch in ' \t"\'\n$&|;()<>*?![]{}'):
            escaped = item.replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'"{escaped}"')
        else:
            out.append(item)
    return " ".join(out)


def _last_lines(text: str, count: int) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-count:]) if lines else "(no output)"


__all__ = [
    "AvchdForgeError",
    "ExistingTreeError",
    "Issue",
    "JobCancelled",
    "MuxError",
    "PlanError",
    "PrepError",
    "ProbeError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "TrackMappingError",
    "ValidationFailed",
    "VerificationError",
    "format_argv",
]
