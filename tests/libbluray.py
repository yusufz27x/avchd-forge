"""Minimal ctypes binding for libbluray, the independent player used by e2e tests.

The season tests need a second implementation to confirm what the written
``index.bdmv``/``MovieObject.bdmv`` *mean* to a player — our own parsers
agreeing with our own writers proves nothing. libbluray runs the real HDMV VM,
so "inserting the disc plays episode 1" and "Go To -> Title 2 plays episode 2"
become observable locally, before the PS3 ever sees the tree. It is used only
under ``@pytest.mark.e2e`` and the tests skip when the shared library is
missing.

One limit is worth remembering: libbluray opens only the optical names
(``BDMV/index.bdmv``). The AVCHD-on-FAT32 flavour a PS3 mounts from USB
(``BDMV/INDEX.BDM``) is invisible to it — that half is what the console test in
``docs/CONTEXT.md`` is for.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path
from typing import Final

#: libbluray hands out at most one 6144-byte block per read call.
READ_CHUNK: Final[int] = 6144


class _Event(ctypes.Structure):
    """``BD_EVENT``: the event id plus its single parameter."""

    _fields_ = [("event", ctypes.c_int), ("param", ctypes.c_uint32)]


class BlurayDisc:
    """An open disc; use as a context manager.

    ``play()`` is disc insertion (FirstPlay -> TopMenu -> whatever the objects
    decide); ``play_title(n)`` is "Go To -> Title n" with titles 1-based, exactly
    as in the index. Both start the HDMV VM; ``read()`` drives it forward and
    returns the raw m2ts packets (192-byte units, 4-byte arrival timestamps
    included), which is what lets a test tell the episodes' clips apart.
    """

    def __init__(self, library: ctypes.CDLL, tree: Path) -> None:
        self._lib = library
        self._tree = tree
        library.bd_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        library.bd_open.restype = ctypes.c_void_p
        library.bd_close.argtypes = [ctypes.c_void_p]
        library.bd_play.argtypes = [ctypes.c_void_p]
        library.bd_play.restype = ctypes.c_int
        library.bd_play_title.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        library.bd_play_title.restype = ctypes.c_int
        library.bd_read_ext.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        library.bd_read_ext.restype = ctypes.c_int

        self._bd = library.bd_open(str(tree).encode(), None)
        if not self._bd:
            raise OSError(f"libbluray could not open {tree}")
        self._event = _Event()
        self._buffer = ctypes.create_string_buffer(READ_CHUNK)

    def play(self) -> bool:
        """Insert the disc: start at FirstPlay, as a player does on startup."""
        return bool(self._lib.bd_play(self._bd))

    def play_title(self, title: int) -> bool:
        """What the remote's "Go To -> Title N" does."""
        return bool(self._lib.bd_play_title(self._bd, title))

    def read(self, chunks: int = 64) -> bytes:
        """Drive the HDMV VM and collect the TS data it plays."""
        data = bytearray()
        for _ in range(chunks):
            size = self._lib.bd_read_ext(
                self._bd, self._buffer, READ_CHUNK, ctypes.byref(self._event)
            )
            if size > 0:
                data += self._buffer.raw[:size]
        return bytes(data)

    def close(self) -> None:
        if self._bd:
            self._lib.bd_close(self._bd)
            self._bd = None

    def __enter__(self) -> BlurayDisc:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_disc(tree: Path | str) -> BlurayDisc:
    """Open the disc rooted at ``tree`` (the folder holding ``BDMV``).

    Raises :class:`OSError` when libbluray is not installed or cannot load it,
    which callers turn into a skip.
    """
    name = ctypes.util.find_library("bluray")
    if name is None:
        raise OSError("libbluray is not installed (no 'bluray' shared library)")
    try:
        library = ctypes.CDLL(name)
    except OSError as exc:
        raise OSError(f"libbluray could not be loaded: {exc}") from exc
    return BlurayDisc(library, Path(tree))


def starts_with_clip(data: bytes, clip: Path) -> bool:
    """True when ``data`` (raw m2ts packets) begins with the start of ``clip``."""
    if not data:
        return False
    raw = clip.read_bytes()
    return data[: len(raw)] == raw[: len(data)]
