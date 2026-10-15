"""Preferences: a typed view of ``QSettings``.

Every key string lives here, so the UI never has to remember one, and defaults
live next to the getter that applies them. Values are coerced on the way out
(``QSettings`` hands back whatever the backend stored — ``"true"``, ``"1"`` and
``True`` all happen in practice), which keeps the rest of the code free of
``str(...).lower() == "true"``.

Tool overrides interact with :mod:`avchdforge.tooling` like this: the value here
(Preferences) and ``$AVCHD_FORGE_*`` are *authoritative*. When the user set a
path we pass it to ``check_toolchain(overrides=…)`` and it wins over ``PATH``; an
empty value means "search as usual".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from PySide6 import QtCore

from .capabilities import FfmpegCapabilities
from .constants import (
    DEFAULT_BOTTOM_OFFSET,
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_NAME,
    DEFAULT_FONT_SIZE,
    DEFAULT_SPLIT_SIZE_MB,
)
from .meta import MuxOptions
from .plan import PlanOptions
from .prep import SubtitleStyle
from .tooling import FFMPEG, FFPROBE, TSMUXER

ORGANIZATION = "avchd-forge"
APPLICATION = "avchd-forge"

# -- keys (the only place they appear as strings) --------------------------

GEOMETRY = "ui/geometry"
WINDOW_STATE = "ui/window_state"
LOG_VISIBLE = "ui/log_visible"

LAST_INPUT_DIR = "paths/last_input_dir"
LAST_OUTPUT_DIR = "paths/last_output_dir"
TEMP_DIR = "paths/temp_dir"

TOOL_KEYS = {
    TSMUXER: "tools/tsmuxer",
    FFMPEG: "tools/ffmpeg",
    FFPROBE: "tools/ffprobe",
}

SPLIT_ENABLED = "mux/split_enabled"
SPLIT_SIZE_MB = "mux/split_size_mb"
FONT_NAME = "mux/font_name"
FONT_SIZE = "mux/font_size"
FONT_COLOR = "mux/font_color"
BOTTOM_OFFSET = "mux/bottom_offset"

AUDIO_PRIORITY = "langs/audio_priority"
SUBTITLE_PRIORITY = "langs/subtitle_priority"

KEEP_ON_SUCCESS = "temp/keep_on_success"
KEEP_ON_FAILURE = "temp/keep_on_failure"

#: Keys ``snapshot()`` reports and ``clear()`` removes.
KNOWN_KEYS: tuple[str, ...] = (
    GEOMETRY,
    WINDOW_STATE,
    LOG_VISIBLE,
    LAST_INPUT_DIR,
    LAST_OUTPUT_DIR,
    TEMP_DIR,
    *TOOL_KEYS.values(),
    SPLIT_ENABLED,
    SPLIT_SIZE_MB,
    FONT_NAME,
    FONT_SIZE,
    FONT_COLOR,
    BOTTOM_OFFSET,
    AUDIO_PRIORITY,
    SUBTITLE_PRIORITY,
    KEEP_ON_SUCCESS,
    KEEP_ON_FAILURE,
)

DEFAULTS: dict[str, Any] = {
    LOG_VISIBLE: True,
    SPLIT_ENABLED: True,
    SPLIT_SIZE_MB: DEFAULT_SPLIT_SIZE_MB,
    FONT_NAME: DEFAULT_FONT_NAME,
    FONT_SIZE: DEFAULT_FONT_SIZE,
    FONT_COLOR: DEFAULT_FONT_COLOR,
    BOTTOM_OFFSET: DEFAULT_BOTTOM_OFFSET,
    KEEP_ON_SUCCESS: False,
    KEEP_ON_FAILURE: True,
}


def settings_for(path: Path | str) -> QtCore.QSettings:
    """An INI-backed ``QSettings`` at ``path`` (used by tests and ``--config``)."""
    return QtCore.QSettings(str(path), QtCore.QSettings.Format.IniFormat)


class AppSettings:
    """Typed access to the user's preferences."""

    def __init__(self, settings: QtCore.QSettings | None = None) -> None:
        self._settings = settings or QtCore.QSettings(ORGANIZATION, APPLICATION)

    # -- raw access ------------------------------------------------------

    @property
    def qsettings(self) -> QtCore.QSettings:
        """The underlying object, for the UI's geometry helpers."""
        return self._settings

    def value(self, key: str, default: Any = None) -> Any:
        return self._settings.value(key, default)

    def set_value(self, key: str, value: Any) -> None:
        self._settings.setValue(key, value)
        self._settings.sync()

    def snapshot(self) -> dict[str, Any]:
        """Every known preference as it is stored (for logs and bug reports)."""
        return {key: self._settings.value(key, DEFAULTS.get(key)) for key in KNOWN_KEYS}

    def clear(self) -> None:
        """Remove every known preference, so defaults apply again."""
        for key in KNOWN_KEYS:
            self._settings.remove(key)
        self._settings.sync()

    # -- typed access ----------------------------------------------------

    def _bool(self, key: str, default: bool) -> bool:
        value = self._settings.value(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _int(self, key: str, default: int, *, minimum: int | None = None) -> int:
        try:
            number = int(self._settings.value(key, default))
        except (TypeError, ValueError):
            return default
        if minimum is not None and number < minimum:
            return default
        return number

    def _text(self, key: str, default: str = "") -> str:
        value = self._settings.value(key, default)
        return "" if value is None else str(value).strip()

    def _optional_text(self, key: str, default: str = "") -> str | None:
        """``None`` when the value is empty, which means "let tsMuxeR decide"."""
        return self._text(key, default) or None

    def _path(self, key: str, default: Path | None = None) -> Path | None:
        text = self._text(key)
        return Path(text).expanduser() if text else default

    def _set_path(self, key: str, path: Path | str | None) -> None:
        self.set_value(key, str(path) if path else "")

    # -- window state ----------------------------------------------------

    @property
    def log_visible(self) -> bool:
        return self._bool(LOG_VISIBLE, True)

    def set_log_visible(self, visible: bool) -> None:
        self.set_value(LOG_VISIBLE, visible)

    def geometry(self) -> QtCore.QByteArray | None:
        value = self._settings.value(GEOMETRY)
        return value if isinstance(value, QtCore.QByteArray) else None

    def set_geometry(self, data: QtCore.QByteArray) -> None:
        self.set_value(GEOMETRY, data)

    def window_state(self) -> QtCore.QByteArray | None:
        value = self._settings.value(WINDOW_STATE)
        return value if isinstance(value, QtCore.QByteArray) else None

    def set_window_state(self, data: QtCore.QByteArray) -> None:
        self.set_value(WINDOW_STATE, data)

    # -- paths -----------------------------------------------------------

    def last_input_dir(self) -> Path | None:
        return self._path(LAST_INPUT_DIR)

    def set_last_input_dir(self, path: Path | str | None) -> None:
        self._set_path(LAST_INPUT_DIR, path)

    def last_output_dir(self) -> Path | None:
        return self._path(LAST_OUTPUT_DIR)

    def set_last_output_dir(self, path: Path | str | None) -> None:
        self._set_path(LAST_OUTPUT_DIR, path)

    def temp_dir(self) -> Path | None:
        """Where job temp files go; ``None`` means the system temp directory."""
        return self._path(TEMP_DIR)

    def set_temp_dir(self, path: Path | str | None) -> None:
        self._set_path(TEMP_DIR, path)

    # -- tools -----------------------------------------------------------

    def tool_path(self, name: str) -> Path | None:
        """The user's override for ``name``, or ``None`` when unset."""
        key = TOOL_KEYS.get(name)
        return self._path(key) if key else None

    def set_tool_path(self, name: str, path: Path | str | None) -> None:
        key = TOOL_KEYS.get(name)
        if key is None:
            raise KeyError(f"unknown tool {name!r}")
        self._set_path(key, path)

    def tool_overrides(self) -> dict[str, Path]:
        """Overrides for ``check_toolchain`` — only the tools the user set."""
        return {
            name: path
            for name in TOOL_KEYS
            if (path := self.tool_path(name)) is not None
        }

    # -- mux options -----------------------------------------------------

    @property
    def split_enabled(self) -> bool:
        return self._bool(SPLIT_ENABLED, True)

    def set_split_enabled(self, enabled: bool) -> None:
        self.set_value(SPLIT_ENABLED, enabled)

    def split_size_mb(self) -> int | None:
        """The split size the mux will use, or ``None`` when splitting is off."""
        if not self.split_enabled:
            return None
        return self.stored_split_size_mb()

    def stored_split_size_mb(self) -> int:
        """The split target whether or not splitting is on.

        The Preferences dialog edits this value while the size box is disabled
        (splitting off), so it must read and write the stored number without
        ``split_enabled`` turning it into ``None`` — otherwise merely opening and
        closing the dialog would reset a custom size.
        """
        return self._int(SPLIT_SIZE_MB, DEFAULT_SPLIT_SIZE_MB, minimum=1)

    def set_split_size_mb(self, value: int | None) -> None:
        if value is not None:
            self.set_value(SPLIT_SIZE_MB, int(value))

    def subtitle_style(self) -> SubtitleStyle:
        """Font settings for tsMuxeR's SRT renderer."""
        return SubtitleStyle(
            font_name=self._optional_text(FONT_NAME, DEFAULT_FONT_NAME),
            font_size=self._int(FONT_SIZE, DEFAULT_FONT_SIZE, minimum=8),
            font_color=self._optional_text(FONT_COLOR, DEFAULT_FONT_COLOR),
            bottom_offset=self._int(BOTTOM_OFFSET, DEFAULT_BOTTOM_OFFSET, minimum=0),
        )

    def set_subtitle_style(self, style: SubtitleStyle) -> None:
        self.set_value(FONT_NAME, style.font_name or "")
        self.set_value(FONT_SIZE, style.font_size or DEFAULT_FONT_SIZE)
        self.set_value(FONT_COLOR, style.font_color or "")
        self.set_value(BOTTOM_OFFSET, style.bottom_offset or 0)

    def plan_options(
        self,
        capabilities: FfmpegCapabilities | None = None,
        *,
        tsmuxer: str | Path | None = None,
        ffmpeg: str | Path | None = None,
    ) -> PlanOptions:
        """Everything :func:`build_plan` takes from the preferences.

        ``tsmuxer``/``ffmpeg`` should be the paths the toolchain resolved; when
        they are omitted the stored override (or the plain tool name) is used.
        """
        return PlanOptions(
            tsmuxer=str(tsmuxer or self.tool_path(TSMUXER) or TSMUXER),
            ffmpeg=str(ffmpeg or self.tool_path(FFMPEG) or FFMPEG),
            mux=MuxOptions(split_size_mb=self.split_size_mb()),
            capabilities=capabilities if capabilities is not None else FfmpegCapabilities(),
            subtitle_style=self.subtitle_style(),
        )

    # -- languages -------------------------------------------------------

    def audio_priority(self) -> tuple[str, ...]:
        return self._languages(AUDIO_PRIORITY)

    def subtitle_priority(self) -> tuple[str, ...]:
        return self._languages(SUBTITLE_PRIORITY)

    def set_audio_priority(self, languages: Sequence[str] | str) -> None:
        self.set_value(AUDIO_PRIORITY, _join_languages(languages))

    def set_subtitle_priority(self, languages: Sequence[str] | str) -> None:
        self.set_value(SUBTITLE_PRIORITY, _join_languages(languages))

    def _languages(self, key: str) -> tuple[str, ...]:
        value = self._settings.value(key, "")
        return tuple(_split_languages(value))

    # -- temp-file policy ------------------------------------------------

    @property
    def keep_temp_on_success(self) -> bool:
        return self._bool(KEEP_ON_SUCCESS, False)

    def set_keep_temp_on_success(self, keep: bool) -> None:
        self.set_value(KEEP_ON_SUCCESS, keep)

    @property
    def keep_temp_on_failure(self) -> bool:
        return self._bool(KEEP_ON_FAILURE, True)

    def set_keep_temp_on_failure(self, keep: bool) -> None:
        self.set_value(KEEP_ON_FAILURE, keep)


def _split_languages(value: Any) -> list[str]:
    """Accept a list from QSettings or a ``"eng, jpn"`` string, in either case."""
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[str] = value.replace(";", ",").split(",")
    elif isinstance(value, Iterable):
        items = (str(item) for item in value)
    else:  # pragma: no cover - defensive: QSettings returns str/list/None
        return []
    return [item.strip().lower() for item in items if item and item.strip()]


def _join_languages(languages: Sequence[str] | str) -> str:
    return ", ".join(_split_languages(languages))


__all__ = [
    "APPLICATION",
    "KNOWN_KEYS",
    "ORGANIZATION",
    "AppSettings",
    "settings_for",
]
