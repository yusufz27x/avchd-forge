"""Drives, filesystems and free space.

The output lives on a USB stick more often than not, so the UI has to answer
three questions before a job starts: which drives are mounted, is the target
FAT32, and is there room for the estimated job. ``lsblk -J`` answers the first
two precisely on Linux (mountpoint, fstype, label, removable flag, size, model,
uuid) and ``/proc/mounts`` is the fallback when it is missing or empty; both are
parsed by pure functions so the tests pin the shapes down with canned input.

The rules that end up as warnings are the ones from the brief:

* **V15** — the filesystem and the split setting disagree. FAT32 cannot hold a
  file of 4 GiB or more, so splitting must stay on there; on exFAT/NTFS/ext4 it
  is optional, and turning it off is the only way to get a single ``.m2ts``.
* **V16** — the estimated output does not fit in the free space.

One check blocks instead of warning — **V20**: the output folder must not
already hold an AVCHD tree (``BDMV/`` or ``AVCHD/BDMV/``). tsMuxeR writes fixed
names, so a second run into the same folder would overwrite the first title.

Everything here is Qt-free: ``list_drives`` takes a
:class:`~avchdforge.process.Runner` (the GUI passes its ``QProcessRunner``, the
tests a ``FakeRunner``) and nothing else spawns a process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .constants import FAT32_FILESYSTEMS
from .process import Runner
from .validate_types import Issue, error, warning

LSBLK: Final[str] = "lsblk"

#: Exactly the columns we use; extra ones would only slow lsblk down.
LSBLK_ARGV: Final[tuple[str, ...]] = (
    LSBLK,
    "-J",
    "-o",
    "NAME,MOUNTPOINT,FSTYPE,LABEL,RM,SIZE,MODEL,UUID",
)

#: Linux's mount table — the fallback when lsblk is missing.
PROC_MOUNTS: Final[Path] = Path("/proc/mounts")

#: Device nodes / filesystems that are never targets: snap and ISO loop mounts,
#: and SWAP (which lsblk "mounts" at the pseudo-path ``[SWAP]``).
_SKIP_NAME_PREFIXES: Final[tuple[str, ...]] = ("loop",)
_SKIP_FSTYPES: Final[frozenset[str]] = frozenset({"squashfs", "swap"})

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGTPE]?)(i?B?)\s*$", re.IGNORECASE)

_UNITS: Final[dict[str, int]] = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
    "E": 1024**6,
}

#: V15 and V16 live here; the media rules own the rest of the numbering.
FAT32_SPLIT_WARNING: Final[str] = "V15"
FREE_SPACE_WARNING: Final[str] = "V16"

#: Blocking: the chosen folder already holds an AVCHD tree (V20).
EXISTING_TREE_ERROR: Final[str] = "V20"

#: Names that mean an AVCHD tree already lives in the chosen folder: the tree
#: this app writes (``BDMV/``) and the camcorder/USB layout (``AVCHD/BDMV/``).
TREE_MARKERS: Final[tuple[Path, ...]] = (Path("BDMV"), Path("AVCHD") / "BDMV")


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Drive:
    """One mounted filesystem, as the output picker sees it."""

    device: str
    mountpoint: Path
    fstype: str = ""
    label: str = ""
    removable: bool = False
    size_bytes: int | None = None
    model: str = ""
    uuid: str = ""

    @property
    def is_fat32(self) -> bool:
        return self.fstype.lower() in FAT32_FILESYSTEMS

    @property
    def badge(self) -> str:
        """Short filesystem tag for the combo box."""
        return self.fstype.upper() if self.fstype else "unknown FS"

    @property
    def name(self) -> str:
        return self.label or self.device or self.mountpoint.name

    @property
    def display_name(self) -> str:
        """One line for the drive combo: ``PS3USB (29.7 GiB, VFAT, removable)``."""
        details = []
        if self.size_bytes:
            details.append(format_size(self.size_bytes))
        details.append(self.badge)
        if self.removable:
            details.append("removable")
        return f"{self.name} ({', '.join(details)})"

    def describe(self) -> str:
        """The drive combo line plus its model and mountpoint."""
        model = f"{self.model}, " if self.model else ""
        return f"{self.display_name} — {model}{self.mountpoint}"

    def contains(self, path: Path | str) -> bool:
        """True when ``path`` lives on this filesystem (longest match wins)."""
        try:
            target = Path(path).expanduser().absolute()
        except (OSError, RuntimeError):  # pragma: no cover - pathological paths
            return False
        mount = self.mountpoint
        return target == mount or mount in target.parents


@dataclass(frozen=True, slots=True)
class TargetReport:
    """What the UI needs to know about the chosen output directory."""

    path: Path
    drive: Drive | None = None
    free_bytes: int | None = None
    needed_bytes: int | None = None
    #: ``None`` when the path does not exist yet (it is created at mux time).
    writable: bool | None = None
    warnings: tuple[Issue, ...] = ()
    #: Findings that block a job (an existing AVCHD tree); warnings never do.
    errors: tuple[Issue, ...] = ()

    @property
    def fstype(self) -> str:
        return self.drive.fstype if self.drive else ""

    @property
    def is_fat32(self) -> bool:
        return bool(self.drive and self.drive.is_fat32)

    @property
    def removable(self) -> bool:
        return bool(self.drive and self.drive.removable)

    @property
    def device(self) -> str:
        return self.drive.device if self.drive else ""

    @property
    def badge(self) -> str:
        """``FAT32``, ``not FAT32 (EXFAT)`` or ``unknown filesystem``."""
        if self.drive is None:
            return "unknown filesystem"
        if self.drive.is_fat32:
            return "FAT32"
        return f"not FAT32 ({self.drive.badge})"

    @property
    def ok(self) -> bool:
        """Warnings never block a job; a read-only target or an existing tree does."""
        return self.writable in (True, None) and not self.errors

    @property
    def has_error(self) -> bool:
        return not self.ok

    def messages(self) -> list[str]:
        return [issue.message for issue in (*self.errors, *self.warnings)]

    def free_label(self) -> str:
        return format_size(self.free_bytes) if self.free_bytes is not None else "unknown"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_size(value: Any) -> int | None:
    """Parse an lsblk human size (``"447.1G"``) into bytes.

    lsblk prints binary powers by default, so ``G`` means GiB — the same number
    the drive picker wants to show.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _SIZE_RE.match(str(value))
    if match is None:
        return None
    number, unit, _suffix = match.groups()
    return int(float(number) * _UNITS[unit.upper()])


def parse_lsblk_json(payload: Mapping[str, Any] | str) -> tuple[Drive, ...]:
    """Flatten ``lsblk -J`` output into mounted filesystems.

    Unmounted partitions are dropped (there is no directory to write into), and a
    partition inherits its physical disk's ``model`` and ``removable`` flag, which
    lsblk only reports on the parent — a partition of a USB stick is on a
    removable device by definition.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return ()
    else:
        data = payload
    if not isinstance(data, Mapping):
        return ()

    drives: list[Drive] = []

    def visit(node: Mapping[str, Any], model: str, removable: bool) -> None:
        own_model = _text(node.get("model")) or model
        own_removable = bool(node.get("rm")) or removable
        mountpoints = _mountpoints(node)
        name = _text(node.get("name"))
        fstype = _text(node.get("fstype"))
        skip = name.lower().startswith(_SKIP_NAME_PREFIXES) or fstype.lower() in _SKIP_FSTYPES
        if mountpoints and not skip:
            for mountpoint in mountpoints:
                drives.append(
                    Drive(
                        device=name,
                        mountpoint=Path(mountpoint),
                        fstype=fstype,
                        label=_text(node.get("label")),
                        removable=own_removable,
                        size_bytes=parse_size(node.get("size")),
                        model=own_model,
                        uuid=_text(node.get("uuid")),
                    )
                )
        for child in node.get("children") or ():
            if isinstance(child, Mapping):
                visit(child, own_model, own_removable)

    for device in data.get("blockdevices") or ():
        if isinstance(device, Mapping):
            visit(device, "", False)
    return _dedupe(drives)


def parse_proc_mounts(text: str) -> tuple[Drive, ...]:
    """Parse ``/proc/mounts`` (or ``lsblk``-less systems) into drives.

    Only real device mounts are kept: ``proc``, ``sysfs``, ``tmpfs`` and friends
    have no ``/dev/...`` source and are not targets. Octal escapes
    (``\\040`` for a space) are decoded, exactly like ``lsblk``'s mountpoint.
    """
    drives: list[Drive] = []
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, fstype = fields[0], fields[1], fields[2]
        if not device.startswith("/dev/") or "/dev/loop" in device:
            continue
        if fstype.lower() in _SKIP_FSTYPES:
            continue
        drives.append(
            Drive(
                device=device,
                mountpoint=Path(_unescape_mount(mountpoint)),
                fstype=fstype,
            )
        )
    return _dedupe(drives)


def _mountpoints(node: Mapping[str, Any]) -> list[str]:
    """``mountpoint`` (str) on older lsblk, ``mountpoints`` (list) on newer ones.

    lsblk marks pseudo-mountpoints in brackets (``[SWAP]``); those are not
    directories anybody can write to, so they are not mountpoints here.
    """
    found: list[str] = []
    mountpoint = node.get("mountpoint")
    if isinstance(mountpoint, str) and mountpoint and not mountpoint.startswith("["):
        found.append(mountpoint)
    for extra in node.get("mountpoints") or ():
        if isinstance(extra, str) and extra and not extra.startswith("[") and extra not in found:
            found.append(extra)
    return found


def _unescape_mount(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _dedupe(drives: Iterable[Drive]) -> tuple[Drive, ...]:
    seen: dict[Path, Drive] = {}
    for drive in drives:
        previous = seen.get(drive.mountpoint)
        if previous is None or (drive.fstype and not previous.fstype):
            seen[drive.mountpoint] = drive
    return tuple(seen.values())


def _ordered(drives: Sequence[Drive]) -> tuple[Drive, ...]:
    """Removable first, then FAT32, then shallower mountpoints, then by path.

    The order is what the output picker wants: a plugged-in USB stick is at the
    top, the root filesystem at the bottom.
    """
    return tuple(
        sorted(
            drives,
            key=lambda drive: (
                not drive.removable,
                not drive.is_fat32,
                len(drive.mountpoint.parts),
                str(drive.mountpoint),
            ),
        )
    )


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------


def list_drives(runner: Runner | None = None) -> tuple[Drive, ...]:
    """Mounted filesystems, most interesting first.

    With a ``runner`` this asks ``lsblk``; without one — or when lsblk is absent,
    fails or prints nothing usable — it falls back to ``/proc/mounts`` on Linux
    and ``os.listdrives()`` on Windows. The fallback knows less: no labels, no
    sizes, no removable flag.
    """
    if runner is not None:
        result = runner.run(LSBLK_ARGV, phase="drives")
        if result.ok and result.stdout.strip():
            drives = parse_lsblk_json(result.stdout)
            if drives:
                return _ordered(drives)
    return _ordered(_fallback_drives())


def _fallback_drives() -> tuple[Drive, ...]:
    if sys.platform == "win32":  # pragma: no cover - platform specific
        return _windows_drives()
    try:
        text = PROC_MOUNTS.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - not Linux, or no /proc
        return ()
    return parse_proc_mounts(text)


def _windows_drives() -> tuple[Drive, ...]:  # pragma: no cover - platform specific
    """``os.listdrives()`` (3.12+) is all Python offers without extra tools."""
    lister = getattr(os, "listdrives", None)
    if lister is None:
        return ()
    drives: list[Drive] = []
    for root in lister():
        drive = Path(root)
        drives.append(Drive(device=root.rstrip("\\/"), mountpoint=drive))
    return tuple(drives)


def find_drive(path: Path | str, drives: Sequence[Drive]) -> Drive | None:
    """The filesystem ``path`` lives on: the deepest mountpoint containing it."""
    matches = [drive for drive in drives if drive.contains(path)]
    if not matches:
        return None
    return max(matches, key=lambda drive: len(drive.mountpoint.parts))


def free_bytes(path: Path | str) -> int | None:
    """Free space at ``path``, walking up to the nearest existing directory."""
    target = Path(path).expanduser()
    for candidate in (target, *target.parents):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None  # pragma: no cover - only when nothing on the path exists


def existing_tree(path: Path | str) -> Path | None:
    """The AVCHD marker already present under ``path``, or ``None``.

    tsMuxeR reuses fixed names (``index.bdmv``, ``00000.m2ts``, …), so muxing
    into a folder that already holds a tree would overwrite the first title.
    """
    target = Path(path).expanduser()
    for marker in TREE_MARKERS:
        if (target / marker).exists():
            return marker
    return None


def assess_target(
    path: Path | str,
    *,
    drives: Sequence[Drive] | None = None,
    needed_bytes: int | None = None,
    splitting: bool = True,
) -> TargetReport:
    """Everything the UI needs to warn about the chosen output directory.

    ``needed_bytes`` is :attr:`~avchdforge.plan.JobPlan.estimated_size_bytes`
    (or any other estimate); without it only the filesystem is judged.
    ``splitting`` is whether the job will split at all, which decides the
    direction of the V15 warning.
    """
    target = Path(path).expanduser()
    known = list(drives) if drives is not None else list_drives()
    drive = find_drive(target, known)
    free = free_bytes(target)
    writable = _is_writable(target)

    issues: list[Issue] = []
    if drive is None:
        issues.append(
            warning(
                FAT32_SPLIT_WARNING,
                f"Could not identify the filesystem of {target} (no mounted drive "
                f"contains it). FAT32 splitting stays "
                f"{'on' if splitting else 'off'}.",
            )
        )
    elif drive.is_fat32 and not splitting:
        issues.append(
            warning(
                FAT32_SPLIT_WARNING,
                f"{target} is on FAT32 ({drive.device}), which cannot hold files of "
                f"4 GiB or more. Enable splitting or the mux will fail on a large file.",
            )
        )
    elif not drive.is_fat32 and splitting:
        issues.append(
            warning(
                FAT32_SPLIT_WARNING,
                f"{target} is on {drive.badge}, not FAT32 ({drive.device}). "
                f"Splitting at 4 GiB is optional here; switch it off for a single .m2ts.",
            )
        )

    if needed_bytes is not None and free is not None and free < needed_bytes:
        issues.append(
            warning(
                FREE_SPACE_WARNING,
                f"The estimated output ({format_size(needed_bytes)}) does not fit in "
                f"the {format_size(free)} free on {drive.mountpoint if drive else target}.",
            )
        )

    errors: list[Issue] = []
    marker = existing_tree(target)
    if marker is not None:
        errors.append(
            error(
                EXISTING_TREE_ERROR,
                f"{target} already contains an AVCHD structure ({marker}). "
                f"Muxing here would overwrite it — choose an empty folder for "
                f"each title, or delete the tree to retry.",
            )
        )

    return TargetReport(
        path=target,
        drive=drive,
        free_bytes=free,
        needed_bytes=needed_bytes,
        writable=writable,
        warnings=tuple(issues),
        errors=tuple(errors),
    )


def _is_writable(path: Path) -> bool | None:
    """``None`` when the directory does not exist yet (it will be created)."""
    if not path.exists():
        return None
    if not path.is_dir():
        return False
    return os.access(path, os.W_OK)


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def format_size(size: int | None) -> str:
    """``1234567`` → ``"1.18 MiB"`` (binary units, one decimal from MiB up)."""
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            digits = 0 if unit in {"B", "KiB"} else 1
            return f"{value:.{digits}f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - unreachable


__all__ = [
    "EXISTING_TREE_ERROR",
    "FAT32_SPLIT_WARNING",
    "FREE_SPACE_WARNING",
    "LSBLK",
    "LSBLK_ARGV",
    "PROC_MOUNTS",
    "TREE_MARKERS",
    "Drive",
    "TargetReport",
    "assess_target",
    "existing_tree",
    "find_drive",
    "format_size",
    "free_bytes",
    "list_drives",
    "parse_lsblk_json",
    "parse_proc_mounts",
    "parse_size",
]
