"""Unit tests for :mod:`avchdforge.settings`.

Each test gets its own INI file, so nothing here reads or writes the developer's
real configuration and ``QSettings`` never has to guess a backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="settings.py wraps QSettings")

from PySide6 import QtCore

from avchdforge.constants import (
    DEFAULT_BOTTOM_OFFSET,
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_NAME,
    DEFAULT_FONT_SIZE,
    DEFAULT_SPLIT_SIZE_MB,
)
from avchdforge.prep import SubtitleStyle
from avchdforge.settings import (
    KEEP_ON_SUCCESS,
    LOG_VISIBLE,
    SPLIT_ENABLED,
    SPLIT_SIZE_MB,
    AppSettings,
    settings_for,
)
from avchdforge.tooling import FFMPEG, FFPROBE, TSMUXER

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def prefs(tmp_path: Path) -> AppSettings:
    return AppSettings(settings_for(tmp_path / "prefs.ini"))


# --------------------------------------------------------------------------
# defaults and round trips
# --------------------------------------------------------------------------


def test_defaults(prefs: AppSettings) -> None:
    assert prefs.log_visible
    assert prefs.last_input_dir() is None
    assert prefs.last_output_dir() is None
    assert prefs.temp_dir() is None  # -> the system temp directory
    assert prefs.split_enabled
    assert prefs.split_size_mb() == DEFAULT_SPLIT_SIZE_MB
    assert not prefs.allow_nonstandard_resolution
    assert prefs.audio_priority() == ()
    assert prefs.subtitle_priority() == ()
    assert not prefs.keep_temp_on_success
    assert prefs.keep_temp_on_failure
    assert prefs.geometry() is None
    assert prefs.window_state() is None
    assert prefs.tool_overrides() == {}


def test_paths_round_trip(prefs: AppSettings, tmp_path: Path) -> None:
    prefs.set_last_input_dir(tmp_path / "movies")
    prefs.set_last_output_dir(tmp_path / "usb" / "AVCHD")
    prefs.set_temp_dir(tmp_path / "scratch")

    assert prefs.last_input_dir() == tmp_path / "movies"
    assert prefs.last_output_dir() == tmp_path / "usb" / "AVCHD"
    assert prefs.temp_dir() == tmp_path / "scratch"

    prefs.set_temp_dir(None)
    assert prefs.temp_dir() is None


def test_boolean_and_number_round_trips(prefs: AppSettings) -> None:
    prefs.set_log_visible(False)
    prefs.set_split_enabled(False)
    prefs.set_split_size_mb(1000)
    prefs.set_allow_nonstandard_resolution(True)
    prefs.set_keep_temp_on_success(True)
    prefs.set_keep_temp_on_failure(False)

    assert not prefs.log_visible
    assert not prefs.split_enabled
    assert prefs.allow_nonstandard_resolution
    assert prefs.keep_temp_on_success
    assert not prefs.keep_temp_on_failure

    prefs.set_split_enabled(True)
    assert prefs.split_size_mb() == 1000
    prefs.set_allow_nonstandard_resolution(False)
    assert not prefs.allow_nonstandard_resolution


def test_values_are_coerced_whatever_the_backend_stored(prefs: AppSettings) -> None:
    # QSettings round-trips through strings in the INI backend; be liberal.
    prefs.set_value(LOG_VISIBLE, "true")
    prefs.set_value(SPLIT_ENABLED, "no")
    prefs.set_value(SPLIT_SIZE_MB, "2048")
    prefs.set_value(KEEP_ON_SUCCESS, 1)

    assert prefs.log_visible
    assert not prefs.split_enabled
    assert prefs.split_size_mb() is None  # splitting is off, so the size is moot
    assert prefs.keep_temp_on_success

    prefs.set_value(SPLIT_ENABLED, "yes")
    assert prefs.split_size_mb() == 2048
    prefs.set_value(SPLIT_SIZE_MB, "not a number")
    assert prefs.split_size_mb() == DEFAULT_SPLIT_SIZE_MB


def test_the_stored_split_size_survives_splitting_being_off(prefs: AppSettings) -> None:
    prefs.set_split_size_mb(2048)
    prefs.set_split_enabled(False)

    assert prefs.split_size_mb() is None  # what the mux will do
    assert prefs.stored_split_size_mb() == 2048  # what Preferences has to keep

    prefs.set_split_enabled(True)
    assert prefs.split_size_mb() == 2048


def test_a_broken_stored_split_size_falls_back_to_the_default(prefs: AppSettings) -> None:
    prefs.set_value(SPLIT_SIZE_MB, "0")

    assert prefs.stored_split_size_mb() == DEFAULT_SPLIT_SIZE_MB


def test_window_state_round_trip(prefs: AppSettings) -> None:
    geometry = QtCore.QByteArray(b"geometry-bytes")
    state = QtCore.QByteArray(b"state-bytes")

    prefs.set_geometry(geometry)
    prefs.set_window_state(state)

    assert prefs.geometry() == geometry
    assert prefs.window_state() == state


def test_settings_persist_to_the_ini_file(tmp_path: Path) -> None:
    path = tmp_path / "prefs.ini"
    first = AppSettings(settings_for(path))
    first.set_split_enabled(False)
    first.set_tool_path(TSMUXER, "/opt/tsMuxeR/tsMuxeR")

    second = AppSettings(settings_for(path))

    assert not second.split_enabled
    assert second.tool_path(TSMUXER) == Path("/opt/tsMuxeR/tsMuxeR")


# --------------------------------------------------------------------------
# tool overrides
# --------------------------------------------------------------------------


def test_tool_overrides_only_list_what_was_set(prefs: AppSettings) -> None:
    prefs.set_tool_path(TSMUXER, "/opt/tsMuxeR")
    prefs.set_tool_path(FFPROBE, "")

    assert prefs.tool_path(TSMUXER) == Path("/opt/tsMuxeR")
    assert prefs.tool_path(FFPROBE) is None
    assert prefs.tool_path(FFMPEG) is None
    assert prefs.tool_overrides() == {TSMUXER: Path("/opt/tsMuxeR")}

    prefs.set_tool_path(TSMUXER, None)
    assert prefs.tool_overrides() == {}


def test_unknown_tool_names_are_refused(prefs: AppSettings) -> None:
    with pytest.raises(KeyError):
        prefs.set_tool_path("mediaInfo", "/usr/bin/mediainfo")
    assert prefs.tool_path("mediaInfo") is None


# --------------------------------------------------------------------------
# derived values
# --------------------------------------------------------------------------


def test_plan_options_defaults(prefs: AppSettings) -> None:
    options = prefs.plan_options()

    assert options.tsmuxer == "tsMuxeR"
    assert options.ffmpeg == "ffmpeg"
    assert options.mux.split_size_mb == DEFAULT_SPLIT_SIZE_MB
    assert options.subtitle_style == SubtitleStyle()
    assert not options.capabilities.has_pgs_encoder
    assert options.allow_nonstandard_resolution is False


def test_plan_options_carry_the_non_standard_resolution_opt_in(prefs: AppSettings) -> None:
    prefs.set_allow_nonstandard_resolution(True)

    assert prefs.plan_options().allow_nonstandard_resolution is True


def test_plan_options_use_the_resolved_toolchain(prefs: AppSettings) -> None:
    prefs.set_tool_path(TSMUXER, "/should/be/overridden")

    options = prefs.plan_options(tsmuxer="/usr/local/bin/tsMuxeR", ffmpeg="/usr/bin/ffmpeg")

    assert options.tsmuxer == "/usr/local/bin/tsMuxeR"
    assert options.ffmpeg == "/usr/bin/ffmpeg"


def test_plan_options_fall_back_to_the_stored_override_or_the_tool_name(
    prefs: AppSettings,
) -> None:
    prefs.set_tool_path(FFMPEG, "/opt/ffmpeg/bin/ffmpeg")

    options = prefs.plan_options()

    assert options.tsmuxer == "tsMuxeR"
    assert options.ffmpeg == "/opt/ffmpeg/bin/ffmpeg"


def test_split_can_be_switched_off_entirely(prefs: AppSettings) -> None:
    prefs.set_split_enabled(False)

    assert prefs.split_size_mb() is None
    assert prefs.plan_options().mux.split_size_mb is None


def test_subtitle_style_round_trip(prefs: AppSettings) -> None:
    assert prefs.subtitle_style() == SubtitleStyle()

    prefs.set_subtitle_style(SubtitleStyle(font_name="Liberation Sans", font_size=48))

    style = prefs.subtitle_style()
    assert style.font_name == "Liberation Sans"
    assert style.font_size == 48
    assert style.font_color == DEFAULT_FONT_COLOR
    assert style.bottom_offset == DEFAULT_BOTTOM_OFFSET


def test_subtitle_style_falls_back_on_silly_values(prefs: AppSettings) -> None:
    prefs.set_subtitle_style(SubtitleStyle(font_name="", font_size=4))

    style = prefs.subtitle_style()

    assert style.font_name is None  # empty means "let tsMuxeR use its default"
    assert style.font_size == DEFAULT_FONT_SIZE  # 4 is below the minimum


def test_language_priority_accepts_strings_lists_and_nothing(prefs: AppSettings) -> None:
    prefs.set_audio_priority("eng, jpn")
    assert prefs.audio_priority() == ("eng", "jpn")

    prefs.set_subtitle_priority(["ENG", "ger", " "])
    assert prefs.subtitle_priority() == ("eng", "ger")

    prefs.set_audio_priority("")
    assert prefs.audio_priority() == ()
    # the plumbing accepts a semicolon-separated value, too
    prefs.set_value("langs/audio_priority", "eng;jpn")
    assert prefs.audio_priority() == ("eng", "jpn")


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def test_snapshot_reports_every_known_key(prefs: AppSettings) -> None:
    prefs.set_split_enabled(False)
    prefs.set_temp_dir("/tmp/forge")

    values = prefs.snapshot()

    assert values[SPLIT_ENABLED] is False
    assert values["paths/temp_dir"] == "/tmp/forge"
    assert values["mux/font_name"] == DEFAULT_FONT_NAME
    assert "tools/tsmuxer" in values


def test_clear_removes_every_known_key(prefs: AppSettings) -> None:
    prefs.set_split_enabled(False)
    prefs.set_tool_path(TSMUXER, "/opt/tsMuxeR")
    prefs.set_temp_dir("/tmp/forge")

    prefs.clear()

    assert prefs.split_enabled
    assert prefs.tool_overrides() == {}
    assert prefs.temp_dir() is None
