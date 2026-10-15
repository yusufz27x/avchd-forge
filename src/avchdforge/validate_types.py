"""Validation result types.

Kept in their own module so that :mod:`avchdforge.errors` can reference the
report type without importing the validation rules (which import the media
model, which imports nothing but constants).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Issue:
    """A single validation finding.

    ``code`` is a stable machine-readable identifier (``V3`` …), ``message`` is
    what the user sees, and ``track_index`` links the finding to an ffprobe
    stream when applicable so the UI can flag the offending row.
    """

    severity: Severity
    code: str
    message: str
    track_index: int | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        prefix = "ERROR" if self.is_error else "WARN"
        track = f" [stream {self.track_index}]" if self.track_index is not None else ""
        return f"{prefix} {self.code}{track}: {self.message}"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Aggregated findings for a media file and/or a track selection."""

    issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:  # pragma: no cover - defensive
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.is_error)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if not issue.is_error)

    @property
    def ok(self) -> bool:
        """True when nothing blocks a run (warnings are allowed)."""
        return not self.errors

    def __bool__(self) -> bool:  # truthiness == "usable"
        return self.ok

    def merged(self, other: ValidationReport) -> ValidationReport:
        return ValidationReport(self.issues + other.issues)

    def messages(self, severity: Severity | None = None) -> list[str]:
        return [
            issue.message
            for issue in self.issues
            if severity is None or issue.severity == severity
        ]


def error(code: str, message: str, track_index: int | None = None) -> Issue:
    return Issue("error", code, message, track_index)


def warning(code: str, message: str, track_index: int | None = None) -> Issue:
    return Issue("warning", code, message, track_index)


@dataclass(slots=True)
class IssueCollector:
    """Small helper so rule functions stay readable."""

    issues: list[Issue] = field(default_factory=list)

    def error(self, code: str, message: str, track_index: int | None = None) -> None:
        self.issues.append(error(code, message, track_index))

    def warning(self, code: str, message: str, track_index: int | None = None) -> None:
        self.issues.append(warning(code, message, track_index))

    def report(self) -> ValidationReport:
        return ValidationReport(tuple(self.issues))


__all__ = [
    "Issue",
    "IssueCollector",
    "Severity",
    "ValidationReport",
    "error",
    "warning",
]
