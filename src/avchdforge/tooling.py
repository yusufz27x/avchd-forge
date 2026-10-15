"""Finding the external tools, and finding out what they can do.

The application refuses to start when tsMuxeR/ffmpeg/ffprobe are missing, so this
module answers three questions for the UI, the CLI and the backend:

* where is the tool? (:func:`locate_tool` — user override, environment variable,
  ``PATH``, then the usual install locations, Windows/macOS/Linux)
* which version is it? (parsed from ``--version``/``-version``; old versions are a
  warning, not a refusal, but they are shown to the user)
* what can ffmpeg actually do? (:func:`detect_capabilities` — the PGS-encoder
  question from ``docs/CONTEXT.md`` §2.3 is answered here and nowhere else)

Every process call goes through a :class:`~avchdforge.process.Runner`, so the whole
module is unit tested with canned output and never spawns anything.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .capabilities import FfmpegCapabilities, Toolchain
from .errors import ToolExecutionError, ToolNotFoundError
from .process import Runner

TSMUXER: Final[str] = "tsMuxeR"
FFMPEG: Final[str] = "ffmpeg"
FFPROBE: Final[str] = "ffprobe"

#: Executable names to look for, per tool (case matters on POSIX).
EXECUTABLE_NAMES: Final[dict[str, tuple[str, ...]]] = {
    TSMUXER: ("tsMuxeR", "tsmuxer", "tsMuxeR.exe", "tsmuxer.exe"),
    FFMPEG: ("ffmpeg", "ffmpeg.exe"),
    FFPROBE: ("ffprobe", "ffprobe.exe"),
}

#: Environment variables that override the search (handy for CI and odd installs).
ENV_OVERRIDES: Final[dict[str, str]] = {
    TSMUXER: "AVCHD_FORGE_TSMUXER",
    FFMPEG: "AVCHD_FORGE_FFMPEG",
    FFPROBE: "AVCHD_FORGE_FFPROBE",
}

#: argv tail that prints the version for each tool.
VERSION_FLAGS: Final[dict[str, tuple[str, ...]]] = {
    TSMUXER: ("--version",),
    FFMPEG: ("-version",),
    FFPROBE: ("-version",),
}

#: Versions below these are flagged (still usable, but the user should know).
MINIMUM_VERSIONS: Final[dict[str, tuple[int, ...]]] = {
    TSMUXER: (2, 6, 0),
    FFMPEG: (4, 0, 0),
    FFPROBE: (4, 0, 0),
}

_VERSION = re.compile(r"version\s+v?(\d\S*)")
_ENCODER = re.compile(r"^\s*([VAS])[A-Z.]{5,6}\s+([A-Za-z0-9_]+)(?:\s|$)")
_MUXER = re.compile(r"^\s*[.ED]{1,3}\s+([A-Za-z0-9_]+)(?:\s|$)")
_BARE_NAME = re.compile(r"^\s*([A-Za-z0-9_]+)\s*$")


# --------------------------------------------------------------------------
# searching
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolLookup:
    """Result of a search, including where we looked (for the error message)."""

    path: Path | None = None
    searched: tuple[str, ...] = ()
    reason: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None


def extra_search_dirs() -> tuple[Path, ...]:
    """Install locations to try after ``PATH``, in platform order."""
    dirs: list[Path] = []
    if os.name == "nt":  # pragma: no cover - platform specific
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                dirs.extend([Path(base) / TSMUXER, Path(base) / "tsMuxeR" / "bin"])
    elif sys.platform == "darwin":  # pragma: no cover - platform specific
        dirs.extend(
            [
                Path("/Applications/tsMuxeR.app/Contents/MacOS"),
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
            ]
        )
    else:
        dirs.extend([Path("/usr/local/bin"), Path("/opt/tsMuxeR"), Path.home() / ".local/bin"])
    # A tools/ directory next to the checkout or the installed package.
    dirs.append(Path(__file__).resolve().parents[2] / "tools")
    return tuple(dirs)


def locate_tool(name: str, *, override: Path | str | None = None) -> ToolLookup:
    """Find ``name``, preferring an explicit override, then ``$…``, then ``PATH``.

    An explicit override (Preferences, ``$AVCHD_FORGE_…``) is authoritative: when
    it is broken we do **not** fall back to ``PATH``, because a configured path
    that silently resolves to another binary is how the wrong tsMuxeR build gets
    used. The caller gets the reason instead.
    """
    names = EXECUTABLE_NAMES.get(name, (name,))
    searched: list[str] = []
    broken = ""

    explicit: list[tuple[str, Path]] = []
    if override is not None:
        explicit.append(("configured path", Path(override).expanduser()))
    env_var = ENV_OVERRIDES.get(name)
    if env_var and (value := os.environ.get(env_var)):
        explicit.append((f"${env_var}", Path(value).expanduser()))
    for label, candidate in explicit:
        searched.append(f"{candidate} ({label})")
        if _is_executable(candidate):
            return ToolLookup(candidate, tuple(searched))
        broken = broken or _broken_reason(candidate)
    if explicit:
        return ToolLookup(None, tuple(searched), broken)

    for candidate_name in names:
        searched.append(f"{candidate_name} (PATH)")
        found = shutil.which(candidate_name)
        if found:
            return ToolLookup(Path(found), tuple(searched))

    for base in extra_search_dirs():
        for candidate_name in names:
            candidate = base / candidate_name
            searched.append(str(candidate))
            if _is_executable(candidate):
                return ToolLookup(candidate, tuple(searched))
            broken = broken or _broken_reason(candidate)

    return ToolLookup(None, tuple(searched), broken)


def find_tool(name: str, *, override: Path | str | None = None) -> Path | None:
    """Just the path (or ``None``); use :func:`locate_tool` for the details."""
    return locate_tool(name, override=override).path


def require_tool(name: str, *, override: Path | str | None = None) -> Path:
    """The path of ``name``, or :class:`ToolNotFoundError` with where we looked."""
    lookup = locate_tool(name, override=override)
    if lookup.path is None:
        raise ToolNotFoundError(name, lookup.searched, lookup.reason)
    return lookup.path


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _broken_reason(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_dir():
        return f"{path} is a directory"
    if not os.access(path, os.X_OK):
        return f"{path} exists but is not executable (chmod +x it)"
    return ""


# --------------------------------------------------------------------------
# version parsing
# --------------------------------------------------------------------------


def parse_version(text: str) -> str | None:
    """First ``version X`` token in a tool's banner (``7.1.5-0+deb13u1``)."""
    match = _VERSION.search(text or "")
    return match.group(1).rstrip(".,;:") if match else None


def version_tuple(version: str | None) -> tuple[int, ...]:
    """Numeric part of a version, for comparisons (``7.1.5-0+deb13u1`` → 7.1.5)."""
    if not version:
        return ()
    match = re.match(r"^(\d+(?:\.\d+)*)", version.strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_outdated(name: str, version: str | None) -> bool:
    """True when ``version`` is below :data:`MINIMUM_VERSIONS` for ``name``."""
    minimum = MINIMUM_VERSIONS.get(name)
    parsed = version_tuple(version)
    if not minimum or not parsed:
        return False
    return parsed < minimum[: len(parsed)]


# --------------------------------------------------------------------------
# capability parsing
# --------------------------------------------------------------------------


def parse_ffmpeg_encoders(text: str) -> frozenset[str]:
    """Subtitle encoder names from ``ffmpeg -encoders`` (the ``S`` rows).

    Only subtitle encoders matter to us (``srt``, ``ass``, ``pgssub``…); the
    legend line (``S..... = Subtitle``) is skipped because ``=`` is not a name.
    """
    names = {
        match.group(2)
        for line in text.splitlines()
        if (match := _ENCODER.match(line)) and match.group(1) == "S"
    }
    return frozenset(names)


def parse_ffmpeg_bitstream_filters(text: str) -> frozenset[str]:
    """Bitstream filter names from ``ffmpeg -bsfs`` (bare, one per line)."""
    names = {match.group(1) for line in text.splitlines() if (match := _BARE_NAME.match(line))}
    return frozenset(names)


def parse_ffmpeg_muxers(text: str) -> frozenset[str]:
    """Muxer names from ``ffmpeg -muxers`` (skips the legend rows)."""
    names = {match.group(1) for line in text.splitlines() if (match := _MUXER.match(line))}
    return frozenset(names)


def parse_ffmpeg_subtitle_encoders(text: str) -> frozenset[str]:
    """Alias for :func:`parse_ffmpeg_encoders`, which parses subtitle rows."""
    return parse_ffmpeg_encoders(text)


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """One tool's resolution: path, version, and what went wrong if anything."""

    name: str
    path: Path | None = None
    version: str | None = None
    searched: tuple[str, ...] = ()
    reason: str = ""
    #: Non-fatal problem (unknown or outdated version).
    warning: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None

    def describe(self) -> str:
        if self.path is None:
            return f"{self.name}: not found"
        version = f" {self.version}" if self.version else " (version unknown)"
        return f"{self.name}{version} — {self.path}"


@dataclass(frozen=True, slots=True)
class ToolReport:
    """The state of the toolchain, as shown by the startup gate and Tools menu."""

    tools: tuple[ToolInfo, ...] = ()
    capabilities: FfmpegCapabilities = field(default_factory=FfmpegCapabilities)

    @property
    def ok(self) -> bool:
        return all(tool.found for tool in self.tools)

    @property
    def missing(self) -> tuple[ToolInfo, ...]:
        return tuple(tool for tool in self.tools if not tool.found)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(tool.warning for tool in self.tools if tool.warning)

    def tool(self, name: str) -> ToolInfo | None:
        return next((tool for tool in self.tools if tool.name == name), None)

    def messages(self) -> list[str]:
        """Lines for the log pane / About dialog."""
        lines = [tool.describe() for tool in self.tools]
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        return lines

    def require_toolchain(self) -> Toolchain:
        """The :class:`Toolchain` these tools form, or a clear refusal."""
        missing = self.missing
        if missing:
            first = missing[0]
            raise ToolNotFoundError(first.name, first.searched, first.reason)
        return Toolchain(
            tsmuxer=_required_path(self, TSMUXER),
            ffmpeg=_required_path(self, FFMPEG),
            ffprobe=_required_path(self, FFPROBE),
            capabilities=self.capabilities,
        )


def _required_path(report: ToolReport, name: str) -> Path:
    tool = report.tool(name)
    if tool is None or tool.path is None:  # pragma: no cover - guarded by require_toolchain
        raise ToolNotFoundError(name, tool.searched if tool else (), "not resolved")
    return tool.path


def tool_version(tool: str | Path, runner: Runner, *, name: str = "") -> str | None:
    """Run a tool's version flag and parse the banner (``None`` when it fails)."""
    path = Path(tool)
    label = name or path.name
    argv = [str(path), *VERSION_FLAGS.get(label, ("--version",))]
    result = runner.run(argv, phase="tools")
    if not result.ok:
        return None
    return parse_version(result.output)


def detect_capabilities(ffmpeg: str | Path, runner: Runner) -> FfmpegCapabilities:
    """Ask ffmpeg what it can encode, filter and mux.

    Raises :class:`ToolExecutionError` when a query fails: a half-known toolchain
    would silently pick the wrong subtitle route.
    """
    tool = str(ffmpeg)
    encoders = _run_capability(tool, runner, "-encoders")
    bitstream_filters = _run_capability(tool, runner, "-bsfs")
    muxers = _run_capability(tool, runner, "-muxers")
    subtitle_encoders = parse_ffmpeg_subtitle_encoders(encoders)
    return FfmpegCapabilities(
        has_pgs_encoder="pgssub" in subtitle_encoders,
        subtitle_encoders=subtitle_encoders,
        bitstream_filters=parse_ffmpeg_bitstream_filters(bitstream_filters),
        muxers=parse_ffmpeg_muxers(muxers),
    )


def _run_capability(tool: str, runner: Runner, flag: str) -> str:
    argv = [tool, "-hide_banner", flag]
    result = runner.run(argv, phase="tools")
    if not result.ok:
        raise ToolExecutionError("ffmpeg", argv, result.returncode, result.stderr, result.stdout)
    return result.output


def check_toolchain(
    runner: Runner,
    *,
    overrides: Mapping[str, Path | str] | None = None,
    probe_capabilities: bool = True,
    names: Sequence[str] = (TSMUXER, FFMPEG, FFPROBE),
) -> ToolReport:
    """Locate every tool, read its version and (optionally) probe ffmpeg.

    Never raises for a missing tool: the report is what the startup gate and the
    Tools menu render, including the exact list of places that were searched.
    """
    overrides = overrides or {}
    tools: list[ToolInfo] = []
    for name in names:
        lookup = locate_tool(name, override=overrides.get(name))
        if not lookup.found:
            tools.append(ToolInfo(name=name, searched=lookup.searched, reason=lookup.reason))
            continue
        assert lookup.path is not None  # narrowed by found
        version = tool_version(lookup.path, runner, name=name)
        warning = ""
        if version is None:
            warning = f"{name} did not report a version"
        elif is_outdated(name, version):
            minimum = ".".join(str(part) for part in MINIMUM_VERSIONS[name])
            warning = f"{name} {version} is older than the supported {minimum}"
        tools.append(
            ToolInfo(
                name=name,
                path=lookup.path,
                version=version,
                searched=lookup.searched,
                warning=warning,
            )
        )

    capabilities = FfmpegCapabilities()
    ffmpeg = next((tool for tool in tools if tool.name == FFMPEG), None)
    if probe_capabilities and ffmpeg is not None and ffmpeg.path is not None:
        capabilities = detect_capabilities(ffmpeg.path, runner)
    return ToolReport(tools=tuple(tools), capabilities=capabilities)


def resolve_toolchain(
    runner: Runner,
    *,
    overrides: Mapping[str, Path | str] | None = None,
    probe_capabilities: bool = True,
) -> Toolchain:
    """Check everything and raise :class:`ToolNotFoundError` if anything is missing."""
    report = check_toolchain(
        runner, overrides=overrides, probe_capabilities=probe_capabilities
    )
    return report.require_toolchain()


__all__ = [
    "ENV_OVERRIDES",
    "EXECUTABLE_NAMES",
    "FFMPEG",
    "FFPROBE",
    "MINIMUM_VERSIONS",
    "TSMUXER",
    "VERSION_FLAGS",
    "ToolInfo",
    "ToolLookup",
    "ToolReport",
    "check_toolchain",
    "detect_capabilities",
    "extra_search_dirs",
    "find_tool",
    "is_outdated",
    "locate_tool",
    "parse_ffmpeg_bitstream_filters",
    "parse_ffmpeg_encoders",
    "parse_ffmpeg_muxers",
    "parse_ffmpeg_subtitle_encoders",
    "parse_version",
    "require_tool",
    "resolve_toolchain",
    "tool_version",
    "version_tuple",
]
