"""Unit tests for :mod:`avchdforge.drives`.

Canned ``lsblk -J`` JSON and ``/proc/mounts`` text live in ``tests/data/drives``,
so no real drive, no ``lsblk`` and no removable media are needed. ``free_bytes``
is monkeypatched where a specific amount of space matters; the real function is
only exercised against ``tmp_path``, where some free space is a given.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avchdforge.drives import (
    EXISTING_TREE_ERROR,
    FAT32_SPLIT_WARNING,
    FREE_SPACE_WARNING,
    LSBLK_ARGV,
    Drive,
    TargetReport,
    assess_target,
    existing_tree,
    filesystem_type,
    find_drive,
    format_size,
    free_bytes,
    list_drives,
    parse_lsblk_json,
    parse_proc_mounts,
    parse_size,
)

from .conftest import FakeRunner

DATA_DIR = Path(__file__).resolve().parent / "data" / "drives"


def fixture_text(name: str) -> str:
    return (DATA_DIR / name).read_text(encoding="utf-8")


@pytest.fixture()
def lsblk_payload() -> dict:
    return json.loads(fixture_text("lsblk.json"))


def drives_from_fixture() -> list[Drive]:
    return list(parse_lsblk_json(json.loads(fixture_text("lsblk.json"))))


# --------------------------------------------------------------------------
# lsblk parsing
# --------------------------------------------------------------------------


def test_lsblk_parsing_flattens_children(lsblk_payload: dict) -> None:
    drives = parse_lsblk_json(lsblk_payload)
    mounts = {str(drive.mountpoint): drive for drive in drives}

    assert set(mounts) == {"/", "/media/user/PS3USB", "/media/user/Intenso"}


def test_lsblk_parsing_reads_every_column(lsblk_payload: dict) -> None:
    usb = parse_lsblk_json(lsblk_payload)[1]

    assert usb == Drive(
        device="sdb1",
        mountpoint=Path("/media/user/PS3USB"),
        fstype="vfat",
        label="PS3USB",
        removable=True,  # inherited from the parent disk, which lsblk marks rm
        size_bytes=int(29.7 * 1024**3),
        model="SanDisk Ultra USB 3.0",
        uuid="1A2B-3C4D",
    )
    assert usb.is_fat32
    assert usb.badge == "VFAT"
    assert "removable" in usb.display_name
    assert "SanDisk" in usb.describe()  # the model, inherited from the disk


def test_lsblk_parsing_skips_unmounted_and_loop_devices(lsblk_payload: dict) -> None:
    drives = parse_lsblk_json(lsblk_payload)

    assert not any(drive.label == "Scratch" for drive in drives)  # mounted nowhere
    assert not any(drive.device.startswith("loop") for drive in drives)  # snap mount
    assert not any(drive.fstype == "squashfs" for drive in drives)


def test_lsblk_parsing_tolerates_rubbish() -> None:
    assert parse_lsblk_json("not json at all") == ()
    assert parse_lsblk_json({}) == ()
    assert parse_lsblk_json({"blockdevices": None}) == ()
    assert parse_lsblk_json({"blockdevices": [{"name": "sda"}]}) == ()  # not mounted


def test_lsblk_parsing_accepts_the_mountpoints_list() -> None:
    payload = {
        "blockdevices": [
            {
                "name": "sdb1",
                "mountpoint": None,
                "mountpoints": ["/media/user/A", "/media/user/B"],
                "fstype": "vfat",
                "rm": True,
            }
        ]
    }

    drives = parse_lsblk_json(payload)

    assert [str(drive.mountpoint) for drive in drives] == ["/media/user/A", "/media/user/B"]


def test_lsblk_parsing_skips_swap_and_pseudo_mountpoints() -> None:
    payload = {
        "blockdevices": [
            {
                "name": "sda2",
                "mountpoint": "[SWAP]",
                "fstype": "swap",
                "rm": False,
            },
            {
                "name": "sda3",
                "mountpoint": "/media/user/USB",
                "fstype": "vfat",
                "rm": True,
            },
        ]
    }

    drives = parse_lsblk_json(payload)

    assert [str(drive.mountpoint) for drive in drives] == ["/media/user/USB"]


# --------------------------------------------------------------------------
# size parsing and formatting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0B", 0),
        ("512M", 512 * 1024**2),
        ("1.5G", 1610612736),
        ("4.5T", int(4.5 * 1024**4)),
        ("73.6M", int(73.6 * 1024**2)),
        (1024, 1024),
        (None, None),
        ("", None),
        ("lots", None),
    ],
)
def test_parse_size(value: object, expected: int | None) -> None:
    assert parse_size(value) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, "unknown"),
        (999, "999 B"),
        (2048, "2 KiB"),
        (1536 * 1024, "1.5 MiB"),
        (int(29.7 * 1024**3), "29.7 GiB"),
    ],
)
def test_format_size(size: int | None, expected: str) -> None:
    assert format_size(size) == expected


# --------------------------------------------------------------------------
# /proc/mounts parsing
# --------------------------------------------------------------------------


def test_proc_mounts_keeps_real_devices_only() -> None:
    drives = parse_proc_mounts(fixture_text("proc_mounts.txt"))
    mounts = [str(drive.mountpoint) for drive in drives]

    assert mounts == [
        "/",
        "/boot",
        "/media/user/My Movies",
        "/media/user/Intenso",
        "/media/user/PS3USB",
    ]
    assert "sysfs" not in {drive.fstype for drive in drives}
    assert drives[2].fstype == "vfat"  # the escaped-space mount
    assert not drives[2].removable  # /proc/mounts simply does not say


def test_proc_mounts_ignores_broken_lines() -> None:
    assert parse_proc_mounts("") == ()
    assert parse_proc_mounts("only-one-field\n# comment\n") == ()


# --------------------------------------------------------------------------
# list_drives
# --------------------------------------------------------------------------


def test_list_drives_uses_lsblk_when_it_works() -> None:
    runner = FakeRunner()
    runner.add_stdout(fixture_text("lsblk.json"))

    drives = list_drives(runner)

    assert runner.last_call == list(LSBLK_ARGV)
    assert runner.phases == ["drives"]
    # removable and FAT32 first, longest-first is *not* the rule here
    assert str(drives[0].mountpoint) == "/media/user/PS3USB"
    assert drives[0].removable and drives[0].is_fat32


def test_list_drives_falls_back_to_proc_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from avchdforge import drives as module

    monkeypatch.setattr(module, "PROC_MOUNTS", DATA_DIR / "proc_mounts.txt")
    runner = FakeRunner()
    runner.add_error("lsblk: not found", returncode=127)

    drives = list_drives(runner)

    # FAT32 first (the picker's order), the root filesystem last
    assert [str(drive.mountpoint) for drive in drives] == [
        "/media/user/My Movies",
        "/media/user/PS3USB",
        "/",
        "/boot",
        "/media/user/Intenso",
    ]


def test_list_drives_falls_back_when_lsblk_prints_nothing_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from avchdforge import drives as module

    monkeypatch.setattr(module, "PROC_MOUNTS", DATA_DIR / "proc_mounts.txt")
    runner = FakeRunner()
    runner.add_stdout("lsblk: no block devices found")

    drives = list_drives(runner)

    assert len(drives) == 5


def test_list_drives_without_a_runner_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from avchdforge import drives as module

    monkeypatch.setattr(module, "PROC_MOUNTS", DATA_DIR / "proc_mounts.txt")

    drives = list_drives()

    assert [drive.fstype for drive in drives[:2]] == ["vfat", "vfat"]


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------


def test_find_drive_picks_the_deepest_mountpoint() -> None:
    drives = [
        Drive("sda1", Path("/"), "ext4"),
        Drive("sdb1", Path("/media/user"), "vfat"),
        Drive("sdc1", Path("/media/user/USB"), "vfat"),
    ]

    found = find_drive("/media/user/USB/AVCHD", drives)

    assert found is not None and found.device == "sdc1"
    assert find_drive("/media/user", drives).device == "sdb1"
    assert find_drive("/etc", drives).device == "sda1"


def test_find_drive_returns_none_for_an_unknown_path() -> None:
    assert find_drive("/somewhere/else", [Drive("sda1", Path("/media/user"), "vfat")]) is None


def test_drive_contains_handles_the_root_mount() -> None:
    root = Drive("sda1", Path("/"), "ext4")

    assert root.contains("/anything/at/all")
    assert root.contains("/")
    assert not Drive("sdb1", Path("/media/user/USB"), "vfat").contains("/media/user")


def test_free_bytes_reports_the_real_usage(tmp_path: Path) -> None:
    free = free_bytes(tmp_path)

    assert free is not None and free > 0
    # a path that does not exist yet walks up to its nearest existing parent
    assert free_bytes(tmp_path / "not" / "created" / "yet") == pytest.approx(free, rel=0.1)


# --------------------------------------------------------------------------
# filesystem_type
# --------------------------------------------------------------------------


def test_filesystem_type_picks_the_deepest_mount(monkeypatch, tmp_path: Path) -> None:
    from avchdforge import drives as module

    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0\n"
        "/dev/sdb1 /media/user/PS3\\040USB vfat rw 0 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "PROC_MOUNTS", mounts)

    assert filesystem_type("/tmp/pytest-of-user") == "tmpfs"
    assert filesystem_type("/tmp") == "tmpfs"
    assert filesystem_type("/home/user/movie.mkv") == "ext4"
    assert filesystem_type("/media/user/PS3 USB/AVCHD") == "vfat"


def test_filesystem_type_returns_none_without_proc_mounts(monkeypatch, tmp_path: Path) -> None:
    from avchdforge import drives as module

    monkeypatch.setattr(module, "PROC_MOUNTS", tmp_path / "not-there")

    assert filesystem_type(tmp_path) is None


# --------------------------------------------------------------------------
# assess_target
# --------------------------------------------------------------------------


def fat32_drives() -> list[Drive]:
    return [
        Drive("sda1", Path("/"), "ext4", size_bytes=447 * 1024**3, removable=False),
        Drive(
            "sdb1",
            Path("/media/user/PS3USB"),
            "vfat",
            label="PS3USB",
            size_bytes=int(29.7 * 1024**3),
            removable=True,
        ),
    ]


def test_assess_target_fat32_with_splitting_is_quiet(tmp_path: Path, monkeypatch) -> None:
    from avchdforge import drives as module

    monkeypatch.setattr(module, "free_bytes", lambda path: 20 * 1024**3)
    target = Path("/media/user/PS3USB/AVCHD")

    report = assess_target(target, drives=fat32_drives(), needed_bytes=8 * 1024**3)

    assert report.warnings == ()
    assert report.ok
    assert report.is_fat32 and report.removable
    assert report.badge == "FAT32"
    assert report.drive is not None and report.drive.label == "PS3USB"
    assert report.free_label() == "20.0 GiB"


def test_assess_target_warns_when_a_fat32_target_is_not_split(tmp_path: Path) -> None:
    report = assess_target(
        "/media/user/PS3USB/AVCHD", drives=fat32_drives(), splitting=False
    )

    assert [issue.code for issue in report.warnings] == [FAT32_SPLIT_WARNING]
    assert "cannot hold files of 4 GiB" in report.warnings[0].message
    assert report.ok  # a warning, not an error


def test_assess_target_warns_about_splitting_on_a_non_fat32_target() -> None:
    drives = [
        Drive("sdc1", Path("/media/user/Intenso"), "exfat", label="Intenso"),
    ]

    report = assess_target("/media/user/Intenso/AVCHD", drives=drives)

    assert report.badge == "not FAT32 (EXFAT)"
    assert report.warnings[0].code == FAT32_SPLIT_WARNING
    assert "optional" in report.warnings[0].message


def test_assess_target_warns_when_the_filesystem_is_unknown() -> None:
    usb_only = [Drive("sdb1", Path("/media/user/PS3USB"), "vfat", removable=True)]

    report = assess_target("/mnt/mystery/out", drives=usb_only)

    assert report.drive is None
    assert report.badge == "unknown filesystem"
    assert report.warnings[0].code == FAT32_SPLIT_WARNING
    assert "Could not identify" in report.warnings[0].message


def test_assess_target_warns_when_the_output_does_not_fit(monkeypatch) -> None:
    from avchdforge import drives as module

    monkeypatch.setattr(module, "free_bytes", lambda path: 3 * 1024**3)

    report = assess_target(
        "/media/user/PS3USB/AVCHD",
        drives=fat32_drives(),
        needed_bytes=8 * 1024**3,
    )

    codes = [issue.code for issue in report.warnings]
    assert codes == [FREE_SPACE_WARNING]
    assert "8.0 GiB" in report.warnings[0].message
    assert "3.0 GiB" in report.warnings[0].message
    assert report.messages() == [report.warnings[0].message]


def test_assess_target_without_an_estimate_skips_the_space_check(monkeypatch) -> None:
    from avchdforge import drives as module

    monkeypatch.setattr(module, "free_bytes", lambda path: 1)

    report = assess_target("/media/user/PS3USB/AVCHD", drives=fat32_drives())

    assert report.warnings == ()
    assert report.free_bytes == 1
    assert report.needed_bytes is None


def test_assess_target_reports_a_read_only_directory(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()

    report = assess_target(target, drives=[])

    assert report.writable is True
    assert report.ok


def test_assess_target_reports_a_file_as_not_writable(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.write_bytes(b"not a directory")

    report = assess_target(target, drives=[])

    assert report.writable is False
    assert report.has_error and not report.ok


def test_assess_target_accepts_a_directory_that_does_not_exist_yet(tmp_path: Path) -> None:
    report = assess_target(tmp_path / "not" / "created", drives=[])

    assert report.writable is None
    assert report.ok


def test_assess_target_blocks_an_existing_bdmv_tree(tmp_path: Path) -> None:
    target = tmp_path / "out"
    (target / "BDMV").mkdir(parents=True)

    report = assess_target(target, drives=[])

    assert not report.ok and report.has_error
    assert [issue.code for issue in report.errors] == [EXISTING_TREE_ERROR]
    assert report.messages()[0] == report.errors[0].message
    assert "already contains an AVCHD structure" in report.errors[0].message
    assert "overwrite" in report.errors[0].message


def test_assess_target_blocks_the_camcorder_layout_too(tmp_path: Path) -> None:
    target = tmp_path / "out"
    (target / "AVCHD" / "BDMV").mkdir(parents=True)

    report = assess_target(target, drives=[])

    assert not report.ok
    assert report.errors[0].code == EXISTING_TREE_ERROR
    assert str(Path("AVCHD") / "BDMV") in report.errors[0].message


def test_existing_tree_only_reports_a_real_tree(tmp_path: Path) -> None:
    assert existing_tree(tmp_path) is None

    (tmp_path / "AVCHD").mkdir()
    assert existing_tree(tmp_path) is None  # an empty AVCHD folder clobbers nothing

    (tmp_path / "AVCHD" / "BDMV").mkdir()
    assert existing_tree(tmp_path) == Path("AVCHD") / "BDMV"


def test_target_report_is_a_plain_value() -> None:
    report = TargetReport(path=Path("/tmp/out"))

    assert report.badge == "unknown filesystem"
    assert not report.is_fat32 and not report.removable
    assert report.device == ""
    assert report.fstype == ""
    assert report.free_label() == "unknown"
