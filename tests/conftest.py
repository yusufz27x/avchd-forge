"""Shared pytest fixtures and the fake process runner.

The core promise of the test suite: **no external process is ever spawned**. Every
test that would normally call tsMuxeR/ffmpeg/ffprobe receives a :class:`FakeRunner`
that records the argv it was given and replays scripted output, which is also how
progress-parsing paths get exercised.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
DATA_DIR = Path(__file__).resolve().parent / "data"

# Allow running the suite from a checkout without installing the package.
if str(SRC_DIR) not in sys.path:  # pragma: no cover - depends on invocation
    sys.path.insert(0, str(SRC_DIR))

from avchdforge.process import CompletedRun  # noqa: E402

FFPROBE_DATA = DATA_DIR / "ffprobe"
META_DATA = DATA_DIR / "meta"
TSMUXER_DATA = DATA_DIR / "tsmuxer"


# --------------------------------------------------------------------------
# Fake runner
# --------------------------------------------------------------------------


@dataclass
class FakeResponse:
    """One scripted tool invocation.

    ``match`` is checked against the joined argv; the first response whose match
    succeeds (or that has no match at all) is used and consumed, so a runner can
    script a whole pipeline in order.
    """

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    match: str | None = None
    echo_as_lines: bool = True
    side_effect: Callable[[Sequence[str]], None] | None = None


@dataclass
class FakeRunner:
    """Stand-in for :class:`avchdforge.process.Runner`."""

    responses: list[FakeResponse] = field(default_factory=list)
    default: FakeResponse = field(default_factory=FakeResponse)
    calls: list[list[str]] = field(default_factory=list)
    cwds: list[Path | None] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    progress: list[float] = field(default_factory=list)
    cancelled: bool = False

    # -- queueing helpers ------------------------------------------------
    def add(self, response: FakeResponse) -> FakeRunner:
        self.responses.append(response)
        return self

    def add_stdout(self, stdout: str, **kwargs: Any) -> FakeRunner:
        return self.add(FakeResponse(stdout=stdout, **kwargs))

    def add_error(self, stderr: str, returncode: int = 1, **kwargs: Any) -> FakeRunner:
        return self.add(FakeResponse(stderr=stderr, returncode=returncode, **kwargs))

    def add_json(self, payload: dict[str, Any], **kwargs: Any) -> FakeRunner:
        return self.add_stdout(json.dumps(payload), **kwargs)

    # -- Runner protocol -------------------------------------------------
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        on_line: Callable[[str], None] | None = None,
        on_progress: Callable[[float | None], None] | None = None,
        scanner: Any = None,
        phase: str = "",
        timeout: float | None = None,
    ) -> CompletedRun:
        args = [str(item) for item in argv]
        self.calls.append(args)
        self.cwds.append(cwd)
        self.phases.append(phase)
        response = self._next_response(args)
        if response.side_effect is not None:
            response.side_effect(args)
        for line in _iter_lines(response.stdout) + _iter_lines(response.stderr):
            self.lines.append(line)
            if on_line is not None:
                on_line(line)
            if scanner is not None and on_progress is not None:
                value = scanner.feed(line)
                if value is not None:
                    self.progress.append(value)
        return CompletedRun(
            argv=tuple(args),
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
        )

    def cancel(self) -> None:
        self.cancelled = True

    # -- inspection helpers ---------------------------------------------
    def _next_response(self, argv: list[str]) -> FakeResponse:
        joined = " ".join(argv)
        for index, response in enumerate(self.responses):
            if response.match is not None and response.match in joined:
                return self.responses.pop(index)
        if self.responses:
            return self.responses.pop(0)
        return self.default

    @property
    def last_call(self) -> list[str]:
        assert self.calls, "no command was run"
        return self.calls[-1]

    def joined_calls(self) -> str:
        return "\n".join(" ".join(call) for call in self.calls)

    def calls_to(self, tool: str) -> list[list[str]]:
        return [call for call in self.calls if call and Path(call[0]).name == tool]


def _iter_lines(text: str) -> list[str]:
    if not text:
        return []
    return text.splitlines(keepends=True)


@pytest.fixture()
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture()
def make_runner() -> Callable[..., FakeRunner]:
    def factory(*responses: FakeResponse, **kwargs: Any) -> FakeRunner:
        return FakeRunner(responses=list(responses), **kwargs)

    return factory


# --------------------------------------------------------------------------
# Fixture data helpers
# --------------------------------------------------------------------------


def load_json(name: str) -> dict[str, Any]:
    """Load an ffprobe JSON fixture from ``tests/data``."""
    path = name if Path(name).is_absolute() else FFPROBE_DATA / name
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    path = Path(name)
    if not path.is_absolute():
        for base in (TSMUXER_DATA, META_DATA, DATA_DIR):
            candidate = base / name
            if candidate.exists():
                path = candidate
                break
    return Path(path).read_text(encoding="utf-8")


def build_media(
    streams: list[dict[str, Any]] | None = None,
    path: str = "movie.mkv",
    **format_overrides: Any,
) -> Any:
    """Build a :class:`MediaInfo` from inline ffprobe-shaped stream dicts."""
    from avchdforge.probe import parse_ffprobe_json

    format_info = {
        "filename": path,
        "format_name": "matroska,webm",
        "format_long_name": "Matroska / WebM",
        "duration": "100.000000",
        "size": "1000000",
        "bit_rate": "80000",
    }
    format_info.update(format_overrides)
    return parse_ffprobe_json({"streams": streams or [], "format": format_info}, path)


def vstream(index: int = 0, codec: str = "h264", **overrides: Any) -> dict[str, Any]:
    """Minimal ffprobe-shaped video stream (overridable per test)."""
    stream: dict[str, Any] = {
        "index": index,
        "codec_type": "video",
        "codec_name": codec,
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "level": 41,
        "profile": "High",
        "field_order": "progressive",
        "avg_frame_rate": "24000/1001",
        "r_frame_rate": "24000/1001",
        "bit_rate": "9000000",
        "disposition": {},
        "tags": {"language": "und"},
    }
    stream.update(overrides)
    return stream


def astream(index: int = 1, codec: str = "ac3", **overrides: Any) -> dict[str, Any]:
    """Minimal ffprobe-shaped audio stream (overridable per test)."""
    stream: dict[str, Any] = {
        "index": index,
        "codec_type": "audio",
        "codec_name": codec,
        "channels": 6,
        "channel_layout": "5.1(side)",
        "sample_rate": "48000",
        "bit_rate": "448000",
        "disposition": {},
        "tags": {"language": "eng"},
    }
    stream.update(overrides)
    return stream


def sstream(index: int = 2, codec: str = "hdmv_pgs_subtitle", **overrides: Any) -> dict[str, Any]:
    """Minimal ffprobe-shaped subtitle stream (overridable per test)."""
    stream: dict[str, Any] = {
        "index": index,
        "codec_type": "subtitle",
        "codec_name": codec,
        "disposition": {},
        "tags": {"language": "eng"},
    }
    stream.update(overrides)
    return stream


@pytest.fixture()
def fixture_json() -> Callable[[str], dict[str, Any]]:
    return load_json


@pytest.fixture()
def fixture_text() -> Callable[[str], str]:
    return load_text


@pytest.fixture()
def golden() -> Callable[[str], str]:
    """Expected file contents for golden-file assertions (meta files, logs…)."""

    def read(name: str) -> str:
        path = META_DATA / name
        return path.read_text(encoding="utf-8")

    return read


@pytest.fixture()
def tmp_media(tmp_path: Path) -> Callable[[str, str], Path]:
    """Create a dummy file with the given suffix (contents are irrelevant)."""

    def factory(name: str = "source.mkv", suffix: str | None = None) -> Path:
        target = tmp_path / name
        if suffix and not target.suffix:
            target = target.with_suffix(suffix)
        target.write_bytes(b"\x00")
        return target

    return factory
