"""Unit tests for :mod:`avchdforge.tooling` (discovery, versions, capabilities).

The parsers are asserted against real captures in ``tests/data/tools`` (ffmpeg
7.1.5, tsMuxeR 2.7.0), so a format change in either tool shows up as a test
failure instead of a silently empty capability set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from avchdforge.capabilities import FfmpegCapabilities, Toolchain
from avchdforge.errors import ToolExecutionError, ToolNotFoundError
from avchdforge.tooling import (
    ENV_OVERRIDES,
    FFMPEG,
    FFPROBE,
    MINIMUM_VERSIONS,
    TSMUXER,
    ToolInfo,
    ToolReport,
    check_toolchain,
    detect_capabilities,
    extra_search_dirs,
    find_tool,
    is_outdated,
    locate_tool,
    parse_ffmpeg_bitstream_filters,
    parse_ffmpeg_encoders,
    parse_ffmpeg_muxers,
    parse_version,
    require_tool,
    resolve_toolchain,
    tool_version,
    version_tuple,
)

from .conftest import load_text

posix_only = pytest.mark.skipif(os.name == "nt", reason="the executable bit is POSIX")


def stub_tool(directory: Path, name: str) -> Path:
    """A file that satisfies the executable check on this platform."""
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def encoders_output() -> str:
    return load_text("ffmpeg_encoders.txt")


@pytest.fixture()
def bsfs_output() -> str:
    return load_text("ffmpeg_bsfs.txt")


@pytest.fixture()
def muxers_output() -> str:
    return load_text("ffmpeg_muxers.txt")


# --------------------------------------------------------------------------
# version parsing
# --------------------------------------------------------------------------


def test_tsmuxer_version_banner_parses():
    assert parse_version(load_text("tsmuxer_version.txt")) == "2.7.0"


def test_ffmpeg_version_banner_parses():
    assert parse_version(load_text("ffmpeg_version.txt")) == "7.1.5-0+deb13u1"


def test_ffprobe_version_banner_parses():
    assert parse_version(load_text("ffprobe_version.txt")) == "7.1.5-0+deb13u1"


def test_parse_version_tolerates_junk():
    assert parse_version("") is None
    assert parse_version("no version here") is None
    assert parse_version("tool version 1.2.3-rc4 (x86_64)") == "1.2.3-rc4"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7.1.5-0+deb13u1", (7, 1, 5)),
        ("2.7.0", (2, 7, 0)),
        ("4", (4,)),
        ("", ()),
        ("nonsense", ()),
    ],
)
def test_version_tuple_keeps_the_numeric_head(text, expected):
    assert version_tuple(text) == expected


@pytest.mark.parametrize(
    ("version", "outdated"),
    [("2.5.0", True), ("2.6.0", False), ("2.7.0", False), (None, False)],
)
def test_is_outdated_compares_against_the_minimum(version, outdated):
    assert is_outdated(TSMUXER, version) is outdated


def test_ffmpeg_minimum_is_checked_too():
    assert is_outdated(FFMPEG, "3.4.2") is True
    assert is_outdated(FFMPEG, "4.0") is False
    assert MINIMUM_VERSIONS[FFPROBE] == (4, 0, 0)


# --------------------------------------------------------------------------
# capability parsing
# --------------------------------------------------------------------------


def test_subtitle_encoders_are_parsed_without_the_legend_row(encoders_output):
    encoders = parse_ffmpeg_encoders(encoders_output)

    assert {"srt", "ass", "ssa", "mov_text", "dvdsub", "dvbsub"} <= encoders
    assert "=" not in encoders, "the 'S..... = Subtitle' legend row is not a codec"
    assert "libx264" not in encoders


def test_stock_ffmpeg_really_has_no_pgs_encoder(encoders_output):
    assert "pgssub" not in parse_ffmpeg_encoders(encoders_output)


def test_bitstream_filters_are_read_from_bare_names(bsfs_output):
    filters = parse_ffmpeg_bitstream_filters(bsfs_output)

    assert {"dca_core", "eac3_core", "truehd_core"} <= filters
    assert "Bitstream" not in filters and "filters:" not in filters


def test_muxers_skip_the_legend_rows(muxers_output):
    muxers = parse_ffmpeg_muxers(muxers_output)

    assert {"matroska", "mpegts"} <= muxers
    assert "=" not in muxers
    assert "Formats:" not in muxers


# --------------------------------------------------------------------------
# capability probing through the runner
# --------------------------------------------------------------------------


def test_detect_capabilities_queries_all_three_tables(
    runner, encoders_output, bsfs_output, muxers_output
):
    runner.add_stdout(encoders_output, match="-encoders")
    runner.add_stdout(bsfs_output, match="-bsfs")
    runner.add_stdout(muxers_output, match="-muxers")

    capabilities = detect_capabilities("/usr/bin/ffmpeg", runner)

    assert capabilities.has_pgs_encoder is False
    assert capabilities.dts_core_supported and capabilities.eac3_core_supported
    assert capabilities.has_muxer("mpegts")
    assert not capabilities.has_bsf("no_such_filter")
    assert [call[0] for call in runner.calls] == ["/usr/bin/ffmpeg"] * 3
    assert [call[2] for call in runner.calls] == ["-encoders", "-bsfs", "-muxers"]
    assert set(runner.phases) == {"tools"}


def test_detect_capabilities_reports_a_pgs_encoder_when_the_build_has_one(runner):
    runner.add_stdout(" S..... pgssub   PGS subtitle (codec hdmv_pgs_subtitle)\n")
    runner.add_stdout("dca_core\n")
    runner.add_stdout(" E  sup   raw HDMV Presentation Graphic Stream subtitles\n")

    capabilities = detect_capabilities("ffmpeg", runner)

    assert capabilities.has_pgs_encoder is True
    assert capabilities.has_subtitle_encoder("pgssub")


def test_a_failing_capability_query_fails_loudly(runner):
    runner.add_stdout("Encoders:\n S..... srt  SubRip subtitle\n", match="-encoders")
    runner.add_error("Unknown option '-bsfs'", returncode=1, match="-bsfs")

    with pytest.raises(ToolExecutionError) as excinfo:
        detect_capabilities("ffmpeg", runner)

    assert excinfo.value.tool == "ffmpeg"
    assert excinfo.value.returncode == 1
    assert "-bsfs" in str(excinfo.value)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

#: A directory that exists on every platform, used to fake an unrunnable tool.
def test_locate_tool_prefers_the_configured_path(tmp_path):
    tool = stub_tool(tmp_path, TSMUXER)

    lookup = locate_tool(TSMUXER, override=tool)

    assert lookup.path == tool
    assert lookup.found
    assert str(tool) in lookup.searched[0]


def test_locate_tool_reports_a_non_executable_override(tmp_path):
    tool = tmp_path / "tsMuxeR"
    tool.write_text("not executable", encoding="utf-8")
    tool.chmod(0o644)

    lookup = locate_tool(TSMUXER, override=tool)

    assert lookup.path is None
    assert "not executable" in lookup.reason


def test_locate_tool_reports_a_missing_override(tmp_path):
    lookup = locate_tool(TSMUXER, override=tmp_path / "nowhere" / "tsMuxeR")

    assert lookup.path is None
    assert lookup.reason == ""
    assert lookup.searched, "the error message needs somewhere to point at"


def test_a_broken_override_is_not_replaced_by_a_path_lookup(tmp_path):
    directory = tmp_path / "not-a-binary"
    directory.mkdir()

    lookup = locate_tool(FFMPEG, override=directory)

    assert lookup.path is None, "the configured path is authoritative, even when broken"
    assert "directory" in lookup.reason
    assert not any("(PATH)" in entry for entry in lookup.searched)


@posix_only
def test_locate_tool_uses_the_environment_override(tmp_path, monkeypatch):
    tool = stub_tool(tmp_path, "mytsmuxer")
    monkeypatch.setenv(ENV_OVERRIDES[TSMUXER], str(tool))

    assert locate_tool(TSMUXER).path == tool


@posix_only
def test_locate_tool_finds_the_lowercase_tsmuxer_name(tmp_path, monkeypatch):
    tool = stub_tool(tmp_path, "tsmuxer")
    monkeypatch.setenv("PATH", str(tmp_path))

    lookup = locate_tool(TSMUXER)

    assert lookup.path == tool
    assert any("PATH" in entry for entry in lookup.searched)


@posix_only
def test_locate_tool_falls_back_to_install_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    tool = stub_tool(tmp_path, FFPROBE)
    monkeypatch.setattr(
        "avchdforge.tooling.extra_search_dirs", lambda: (tmp_path,)
    )

    assert locate_tool(FFPROBE).path == tool


@posix_only
def test_locate_tool_lists_everywhere_it_looked(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    lookup = locate_tool(FFPROBE)

    assert lookup.path is None, "nothing named ffprobe lives in an empty PATH"
    assert any(entry.endswith("(PATH)") for entry in lookup.searched)
    assert all(
        Path(entry).is_absolute() or "(PATH)" in entry or "$" in entry
        for entry in lookup.searched
    )


def test_extra_search_dirs_offer_a_tools_directory():
    dirs = extra_search_dirs()

    assert dirs, "at least one fallback location"
    assert any(path.name == "tools" for path in dirs)


def test_find_tool_returns_none_instead_of_raising(tmp_path):
    assert find_tool(TSMUXER, override=tmp_path / "nothing") is None


def test_require_tool_raises_with_the_searched_locations(tmp_path):
    with pytest.raises(ToolNotFoundError) as excinfo:
        require_tool(TSMUXER, override=tmp_path / "nothing")

    assert "tsMuxeR is unavailable" in str(excinfo.value)
    assert excinfo.value.name == "tsMuxeR"
    assert excinfo.value.searched


# --------------------------------------------------------------------------
# version + capability through the runner
# --------------------------------------------------------------------------


def test_tool_version_uses_the_right_flag_per_tool(runner):
    runner.add_stdout(load_text("tsmuxer_version.txt"))

    version = tool_version("/opt/tsMuxeR", runner, name=TSMUXER)

    assert version == "2.7.0"
    assert runner.last_call == ["/opt/tsMuxeR", "--version"]


def test_tool_version_returns_none_when_the_tool_fails(runner):
    runner.add_error("boom", returncode=127)

    assert tool_version("/opt/ffmpeg", runner, name=FFMPEG) is None


# --------------------------------------------------------------------------
# the whole report
# --------------------------------------------------------------------------


@pytest.fixture()
def installed_tools(tmp_path):
    return {
        TSMUXER: stub_tool(tmp_path, "tsMuxeR"),
        FFMPEG: stub_tool(tmp_path, "ffmpeg"),
        FFPROBE: stub_tool(tmp_path, "ffprobe"),
    }


def test_check_toolchain_collects_versions_and_capabilities(
    runner, installed_tools, encoders_output, bsfs_output, muxers_output
):
    runner.add_stdout(load_text("tsmuxer_version.txt"), match="--version")
    runner.add_stdout(load_text("ffmpeg_version.txt"), match="ffmpeg")
    runner.add_stdout(load_text("ffprobe_version.txt"), match="ffprobe")
    runner.add_stdout(encoders_output, match="-encoders")
    runner.add_stdout(bsfs_output, match="-bsfs")
    runner.add_stdout(muxers_output, match="-muxers")

    report = check_toolchain(runner, overrides=installed_tools)

    assert report.ok
    assert report.missing == ()
    assert report.tool(TSMUXER).version == "2.7.0"
    assert report.tool(FFMPEG).version == "7.1.5-0+deb13u1"
    assert report.capabilities.has_bsf("dca_core")
    assert report.warnings == ()

    lines = report.messages()
    assert any(line.startswith("tsMuxeR 2.7.0") for line in lines)


def test_check_toolchain_reports_every_missing_tool_without_raising(runner, tmp_path):
    report = check_toolchain(
        runner,
        overrides={
            TSMUXER: tmp_path / "absent-tsmuxer",
            FFMPEG: tmp_path / "absent-ffmpeg",
            FFPROBE: tmp_path / "absent-ffprobe",
        },
        probe_capabilities=False,
    )

    assert not report.ok
    assert {tool.name for tool in report.missing} == {TSMUXER, FFMPEG, FFPROBE}
    assert all(tool.searched for tool in report.missing)
    assert "not found" in "\n".join(report.messages())
    with pytest.raises(ToolNotFoundError):
        report.require_toolchain()


def test_check_toolchain_warns_about_an_old_version(runner, installed_tools):
    runner.add_stdout("tsMuxeR version 2.5.0. github.com/justdan96/tsMuxer\n", match="--version")
    runner.add_stdout("ffmpeg version 7.1.5 Copyright\n", match="ffmpeg")
    runner.add_stdout("ffprobe version 7.1.5 Copyright\n", match="ffprobe")

    report = check_toolchain(runner, overrides=installed_tools, probe_capabilities=False)

    assert report.ok, "an old version is a warning, not a refusal"
    assert report.warnings == ("tsMuxeR 2.5.0 is older than the supported 2.6.0",)


def test_check_toolchain_can_skip_capability_probing(runner, installed_tools):
    runner.add_stdout(load_text("tsmuxer_version.txt"), match="--version")
    runner.add_stdout(load_text("ffmpeg_version.txt"), match="ffmpeg")
    runner.add_stdout(load_text("ffprobe_version.txt"), match="ffprobe")

    report = check_toolchain(runner, overrides=installed_tools, probe_capabilities=False)

    assert report.capabilities == FfmpegCapabilities()
    assert "-encoders" not in runner.joined_calls()


def test_report_without_a_version_still_counts_as_found(runner, installed_tools):
    runner.add_error("no version flag", returncode=1)
    runner.add_stdout(load_text("ffprobe_version.txt"), match="ffprobe")

    report = check_toolchain(runner, overrides=installed_tools, probe_capabilities=False)

    assert report.ok
    assert report.tool(TSMUXER).version is None
    assert "did not report a version" in report.tool(TSMUXER).warning


def test_require_toolchain_builds_the_toolchain(runner, installed_tools):
    runner.add_stdout(load_text("tsmuxer_version.txt"), match="--version")
    runner.add_stdout(load_text("ffmpeg_version.txt"), match="ffmpeg")
    runner.add_stdout(load_text("ffprobe_version.txt"), match="ffprobe")
    report = check_toolchain(runner, overrides=installed_tools, probe_capabilities=False)

    toolchain = report.require_toolchain()

    assert isinstance(toolchain, Toolchain)
    assert toolchain.tsmuxer == installed_tools[TSMUXER]
    assert toolchain.capabilities == FfmpegCapabilities()


def test_resolve_toolchain_wires_everything_together(
    runner, installed_tools, encoders_output, bsfs_output, muxers_output
):
    runner.add_stdout(load_text("tsmuxer_version.txt"), match="--version")
    runner.add_stdout(load_text("ffmpeg_version.txt"), match="ffmpeg")
    runner.add_stdout(load_text("ffprobe_version.txt"), match="ffprobe")
    runner.add_stdout(encoders_output, match="-encoders")
    runner.add_stdout(bsfs_output, match="-bsfs")
    runner.add_stdout(muxers_output, match="-muxers")

    toolchain = resolve_toolchain(runner, overrides=installed_tools)

    assert toolchain.ffprobe == installed_tools[FFPROBE]
    assert toolchain.capabilities.dts_core_supported


def test_tool_info_describes_itself():
    found = ToolInfo(name=TSMUXER, path=Path("/opt/tsMuxeR"), version="2.7.0")
    missing = ToolInfo(name=FFMPEG)

    assert found.describe() == "tsMuxeR 2.7.0 — /opt/tsMuxeR"
    assert missing.describe() == "ffmpeg: not found"
    assert (
        ToolInfo(name=FFMPEG, path=Path("/usr/bin/ffmpeg")).describe()
        == "ffmpeg (version unknown) — /usr/bin/ffmpeg"
    )


def test_empty_report_is_ok_and_warns_about_nothing():
    report = ToolReport()

    assert report.ok and report.missing == () and report.warnings == ()
    assert report.tool(FFMPEG) is None
