"""The navigation data of an AVCHD tree: ``index.bdmv`` and ``MovieObject.bdmv``.

Together those two files turn a pile of clips and playlists into a disc a player
can address by title:

* ``index.bdmv`` holds the disc's title table. Every entry names the *movie
  object* the player runs for that title, plus the two special entries
  FirstPlay (what starts when the disc is inserted) and TopMenu (what the
  remote control's Top Menu button runs).
* ``MovieObject.bdmv`` holds those movie objects: short straight-line HDMV
  programs built from 12-byte instructions. A title's object is essentially
  "play playlist N" — and tsMuxeR writes exactly one such object per disc.

That single title is the piece tsMuxeR cannot express for a season. The builders
below reproduce the bytes tsMuxeR itself writes — the golden tests in
``tests/test_bdmv.py`` compare against captured tsMuxeR output byte for byte —
and generalise them to N titles, one per playlist.

Field and instruction names follow libbluray's parsers (``index_parse.c``,
``mobj_parse.c``, ``hdmv_insn.h``), so the layout comments describe what a player
reads:

==========  ============================================================
``INDX``    signature, version, body address, extension-data address
app info    flags and provider data; zeros for the AVCHD trees we write
title table FirstPlay, TopMenu, title count, then 12-byte entries
``MOBJ``    signature, version, payload length, object count
object      2-byte flag word, command count, 12-byte commands
==========  ============================================================

Everything here is pure: no process, no Qt, no filesystem.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .errors import NavigationDataError

# --------------------------------------------------------------------------
# playback entries (shared by index and, conceptually, movie objects)
# --------------------------------------------------------------------------

#: Object the entry points at: 1 = HDMV movie object, 2 = BD-J (AVCHD never).
OBJECT_TYPE_HDMV: Final[int] = 1
OBJECT_TYPE_BDJ: Final[int] = 2

#: The two pseudo title numbers players use inside movie objects.
#: ``JumpTitle 0`` runs the TopMenu entry, ``JumpTitle 0xFFFF`` FirstPlay, and
#: a real title is 1-based (libbluray's ``_is_valid_title``).
TITLE_TOP_MENU: Final[int] = 0
TITLE_FIRST_PLAY: Final[int] = 0xFFFF

# --------------------------------------------------------------------------
# index.bdmv
# --------------------------------------------------------------------------

_INDEX_SIGNATURE: Final[bytes] = b"INDX"
_INDEX_VERSION_AVCHD: Final[bytes] = b"0100"
_APPLICATION_INFO_LENGTH: Final[int] = 34

#: Offset of the index body in every AVCHD index tsMuxeR writes:
#: header (16) + 24 reserved + application info (4 + 34) = 0x4E.
INDEX_BODY_OFFSET: Final[int] = 0x4E

#: Offset of the extension-data address field inside the header.
_EXTENSION_ADDRESS_FIELD: Final[int] = 0x0C

_INDEX_HEAD: Final[bytes] = (
    _INDEX_SIGNATURE
    + _INDEX_VERSION_AVCHD
    + struct.pack(">I", INDEX_BODY_OFFSET)
    + b"\x00" * 4  # extension-data address, patched by build_index()
    + b"\x00" * 24  # reserved
    + struct.pack(">I", _APPLICATION_INFO_LENGTH)
    + b"\x00" * 2  # flags; video format/frame-rate code
    + b"\x00" * 32  # provider data
)

#: The AVCHD index extension ("IDEX") tsMuxeR appends after the title table.
#: libbluray logs it as an unknown extension (``4096.256``) but every AVCHD
#: index tsMuxeR produces carries it, so it is preserved verbatim — the season
#: navigation keeps the same bytes and only grows the table in front of them.
_AVCHD_INDEX_EXTENSION: Final[bytes] = bytes.fromhex(
    "000001920000001800000001"
    "10000100000000180000017e"
    "494445580000000000000162"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000013610130001"
    "545220302e302e312e380000"
    "000000000000000000000000"
    "00000000000000000006ffff"
    "422007080800545300900a54"
    "5220302e302e312e38000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000000000"
    "000000000000000000180000"
    "000200000000000200000001"
    "303030303001000000000000"
)


@dataclass(frozen=True)
class IndexEntry:
    """One playback entry: which movie object runs for this title (or menu)."""

    object_type: int
    interactive: bool
    id_ref: int
    access_type: int = 0


@dataclass(frozen=True)
class Index:
    """The parsed title table of an ``index.bdmv``."""

    first_play: IndexEntry
    top_menu: IndexEntry
    titles: tuple[IndexEntry, ...]


def _encode_entry(entry: IndexEntry) -> bytes:
    first = (entry.object_type << 6) | (entry.access_type << 4)
    second = (1 if entry.interactive else 0) << 6
    return bytes((first, 0, 0, 0, second, 0)) + struct.pack(">H", entry.id_ref) + b"\x00" * 4


def build_index(*, first_play: int, top_menu: int, titles: Sequence[int]) -> bytes:
    """Build an ``index.bdmv`` for ``titles`` with the given object references.

    ``first_play`` and ``top_menu`` are movie-object indexes; ``titles[i]`` is
    the movie object that title ``i + 1`` runs. FirstPlay and TopMenu are marked
    interactive (the flag tsMuxeR writes; players hide their length in UIs).
    """
    body = bytearray()
    for id_ref in (first_play, top_menu):
        body += _encode_entry(IndexEntry(OBJECT_TYPE_HDMV, interactive=True, id_ref=id_ref))
    body += struct.pack(">H", len(titles))
    for id_ref in titles:
        body += _encode_entry(IndexEntry(OBJECT_TYPE_HDMV, interactive=False, id_ref=id_ref))

    head = bytearray(_INDEX_HEAD)
    struct.pack_into(">I", head, _EXTENSION_ADDRESS_FIELD, len(head) + 4 + len(body))
    return bytes(head) + struct.pack(">I", len(body)) + bytes(body) + _AVCHD_INDEX_EXTENSION


def _parse_entry(data: bytes, offset: int) -> IndexEntry:
    return IndexEntry(
        object_type=data[offset] >> 6,
        access_type=(data[offset] >> 4) & 0x03,
        interactive=bool(data[offset + 4] >> 6),
        id_ref=struct.unpack_from(">H", data, offset + 6)[0],
    )


def parse_index(data: bytes) -> Index:
    """Read the title table back. Raises :class:`NavigationDataError` loudly."""
    if len(data) < INDEX_BODY_OFFSET or data[:4] != _INDEX_SIGNATURE:
        raise NavigationDataError("index.bdmv: missing INDX signature")
    body = struct.unpack_from(">I", data, 8)[0]
    if body + 4 > len(data):
        raise NavigationDataError(
            f"index.bdmv: table address {body:#x} is outside a {len(data)}-byte file"
        )
    body_length = struct.unpack_from(">I", data, body)[0]
    if body + 4 + body_length > len(data):
        raise NavigationDataError(f"index.bdmv: table length {body_length} overflows the file")
    count = struct.unpack_from(">H", data, body + 4 + 24)[0]
    titles = []
    offset = body + 4 + 26
    for _ in range(count):
        if offset + 12 > len(data):
            raise NavigationDataError("index.bdmv: truncated title table")
        titles.append(_parse_entry(data, offset))
        offset += 12
    return Index(
        first_play=_parse_entry(data, body + 4),
        top_menu=_parse_entry(data, body + 16),
        titles=tuple(titles),
    )


# --------------------------------------------------------------------------
# MovieObject.bdmv
# --------------------------------------------------------------------------

_MOBJ_SIGNATURE: Final[bytes] = b"MOBJ"
_MOBJ_VERSION_AVCHD: Final[bytes] = b"0100"
#: First movie object: header (16) + 24 reserved + payload length + reserved +
#: object count.
_MOBJ_OBJECTS_OFFSET: Final[int] = 0x32

#: Bits of each object's flag word (libbluray's names for the PSR-adjacent
#: behaviour the player applies around a playing object).
RESUME_INTENTION: Final[int] = 0x8000
MENU_CALL_MASK: Final[int] = 0x4000
TITLE_SEARCH_MASK: Final[int] = 0x2000

# Opcode bytes. All commands are 12 bytes: opcode (4) + destination (4) +
# source (4), 32-bit big endian. tsMuxeR's sequences are copied verbatim, so
# only the opcodes its objects use have builders here.
_OP_MOVE_IMMEDIATE = bytes((0x50, 0x40, 0x00, 0x01))  # SET/SET move, immediate source
_OP_MOVE_REGISTER = bytes((0x50, 0x00, 0x00, 0x01))  # SET/SET move, register source
_OP_PLAY_PLAYLIST = bytes((0x22, 0x00, 0x00, 0x00))  # BRANCH/PLAY play playlist
_OP_PLAY_PLAYLIST_FROM_MARK = bytes((0x42, 0x82, 0x00, 0x00))  # BRANCH/PLAY play at mark
_OP_COMPARE_NOT_EQUAL = bytes((0x48, 0x40, 0x03, 0x00))  # CMP/SET not equal
_OP_JUMP_TITLE = bytes((0x21, 0x81, 0x00, 0x00))  # BRANCH/JUMP jump to title
_OP_JUMP_TITLE_FROM_REGISTER = bytes((0x21, 0x01, 0x00, 0x00))  # ... title from register
_OP_BREAK = bytes((0x00, 0x02, 0x00, 0x00))  # BRANCH/GOTO break: ends the object

#: General-purpose registers tsMuxeR's objects use.
_REGISTER_MARK: Final[int] = 10  # play mark; 0 means "from the start"
_REGISTER_ZERO: Final[int] = 0
_REGISTER_RESUME_PLAYLIST: Final[int] = 3  # seeded 0xFFFF = nothing to resume
_REGISTER_RESUME_TITLE: Final[int] = 4  # seeded 0 = no title to resume


def _command(opcode: bytes, dst: int, src: int = 0) -> bytes:
    return opcode + struct.pack(">II", dst, src)


def move_immediate(register: int, value: int) -> bytes:
    """``move register, value``."""
    return _command(_OP_MOVE_IMMEDIATE, register, value)


def move_register(destination: int, source: int) -> bytes:
    """``move destination, source`` (register to register)."""
    return _command(_OP_MOVE_REGISTER, destination, source)


def compare_not_equal(register: int, value: int) -> bytes:
    """``ne register, value`` — *skips the next command when they are equal*.

    HDMV comparisons modify the program counter: a true comparison falls
    through, a false one jumps over one instruction. The patterns below read as
    "condition; action when true; fall-through".
    """
    return _command(_OP_COMPARE_NOT_EQUAL, register, value)


def play_playlist(playlist: int) -> bytes:
    """Play ``playlist`` from the start, via the mark register tsMuxeR keeps in 0."""
    return _command(_OP_PLAY_PLAYLIST_FROM_MARK, playlist, _REGISTER_MARK)


def play_playlist_from_register(register: int) -> bytes:
    """Play the playlist number held in ``register``."""
    return _command(_OP_PLAY_PLAYLIST, register)


def jump_title(title: int) -> bytes:
    """Jump to ``title``; ``TITLE_TOP_MENU``/``TITLE_FIRST_PLAY`` are legal here."""
    return _command(_OP_JUMP_TITLE, title)


def jump_title_from_register(register: int) -> bytes:
    """Jump to the title number held in ``register``."""
    return _command(_OP_JUMP_TITLE_FROM_REGISTER, register)


def end_object() -> bytes:
    """HDMV ``break``: ends the movie object."""
    return _command(_OP_BREAK, 0)


@dataclass(frozen=True)
class MovieObject:
    """One movie object: a flag word plus a straight-line command list."""

    commands: tuple[bytes, ...] = ()
    resume_intention: bool = True
    menu_call_mask: bool = False
    title_search_mask: bool = False

    def _flags(self) -> int:
        flags = 0
        if self.resume_intention:
            flags |= RESUME_INTENTION
        if self.menu_call_mask:
            flags |= MENU_CALL_MASK
        if self.title_search_mask:
            flags |= TITLE_SEARCH_MASK
        return flags

    def encode(self) -> bytes:
        flags = struct.pack(">H", self._flags())
        return flags + struct.pack(">H", len(self.commands)) + b"".join(self.commands)


def build_movie_objects(objects: Sequence[MovieObject]) -> bytes:
    """Build a ``MovieObject.bdmv`` holding ``objects`` in order."""
    payload = b"".join(obj.encode() for obj in objects)
    return (
        _MOBJ_SIGNATURE
        + _MOBJ_VERSION_AVCHD
        + b"\x00" * 4  # extension-data address (unused by AVCHD)
        + b"\x00" * 28  # reserved
        + struct.pack(">I", len(payload) + 6)  # payload length + this/next fields
        + b"\x00" * 4
        + struct.pack(">H", len(objects))
        + payload
    )


def parse_movie_objects(data: bytes) -> tuple[MovieObject, ...]:
    """Read movie objects back. Raises :class:`NavigationDataError` loudly."""
    if len(data) < _MOBJ_OBJECTS_OFFSET or data[:4] != _MOBJ_SIGNATURE:
        raise NavigationDataError("MovieObject.bdmv: missing MOBJ signature")
    count = struct.unpack_from(">H", data, _MOBJ_OBJECTS_OFFSET - 2)[0]
    objects = []
    offset = _MOBJ_OBJECTS_OFFSET
    for _ in range(count):
        if offset + 4 > len(data):
            raise NavigationDataError("MovieObject.bdmv: truncated object header")
        flags, command_count = struct.unpack_from(">HH", data, offset)
        offset += 4
        commands = []
        for _ in range(command_count):
            if offset + 12 > len(data):
                raise NavigationDataError("MovieObject.bdmv: truncated command list")
            commands.append(bytes(data[offset : offset + 12]))
            offset += 12
        objects.append(
            MovieObject(
                commands=tuple(commands),
                resume_intention=bool(flags & RESUME_INTENTION),
                menu_call_mask=bool(flags & MENU_CALL_MASK),
                title_search_mask=bool(flags & TITLE_SEARCH_MASK),
            )
        )
    return tuple(objects)


# --------------------------------------------------------------------------
# season navigation: N titles, one per episode playlist
# --------------------------------------------------------------------------


def episode_object(playlist: int) -> MovieObject:
    """The object a title runs: clear the mark register, play ``playlist``.

    Byte-for-byte the object tsMuxeR writes for its single title, with the
    playlist number as the only variable. The two ``move r0, 0`` commands are
    tsMuxeR's register clearing plus the slot where an authored blank playlist
    would go; both are kept so the shape stays identical to its output.
    """
    return MovieObject(
        commands=(
            move_register(_REGISTER_MARK, _REGISTER_ZERO),
            move_immediate(_REGISTER_ZERO, 0),
            move_immediate(_REGISTER_ZERO, 0),
            play_playlist(playlist),
            end_object(),
        )
    )


def top_menu_object() -> MovieObject:
    """tsMuxeR's TopMenu object: replay an interrupted playlist/title, else go to title 1.

    The player seeds no registers, so both "resume" conditions are false on a
    freshly inserted disc and the object falls through to ``jump title 1``.
    """
    return MovieObject(
        commands=(
            move_register(_REGISTER_MARK, _REGISTER_RESUME_PLAYLIST),
            move_immediate(_REGISTER_RESUME_PLAYLIST, 0xFFFF),
            compare_not_equal(_REGISTER_MARK, 0xFFFF),
            play_playlist_from_register(_REGISTER_MARK),
            move_register(_REGISTER_MARK, _REGISTER_RESUME_TITLE),
            move_immediate(_REGISTER_RESUME_TITLE, 0),
            compare_not_equal(_REGISTER_MARK, 0),
            jump_title_from_register(_REGISTER_MARK),
            jump_title(1),
        )
    )


def first_play_object() -> MovieObject:
    """tsMuxeR's FirstPlay object: clear the resume registers, enter the TopMenu.

    ``jump title 0`` is the TopMenu entry, so inserting the disc plays whatever
    the TopMenu object decides — for this tree, title 1.
    """
    return MovieObject(
        commands=(
            move_immediate(_REGISTER_ZERO, 0),
            move_immediate(2, 1),
            move_immediate(_REGISTER_RESUME_PLAYLIST, 0xFFFF),
            move_immediate(_REGISTER_RESUME_TITLE, 0),
            jump_title(TITLE_TOP_MENU),
        )
    )


@dataclass(frozen=True)
class SeasonNavigation:
    """The two navigation files for a season, plus the title table they define."""

    index: bytes
    movie_objects: bytes
    playlists: tuple[int, ...]

    @property
    def title_count(self) -> int:
        return len(self.playlists)


def season_navigation(playlists: Sequence[int]) -> SeasonNavigation:
    """Navigation for one title per playlist, in the given order.

    Title ``i + 1`` plays ``playlists[i]``; the two extra movie objects are
    tsMuxeR's TopMenu and FirstPlay, so pressing Top Menu (or inserting the
    disc) starts at title 1. Chapter navigation stays inside each title's
    playlist, which is what makes episodes independent.
    """
    if not playlists:
        raise NavigationDataError("a season needs at least one episode playlist")
    objects = [episode_object(playlist) for playlist in playlists]
    objects.append(top_menu_object())
    objects.append(first_play_object())
    index = build_index(
        first_play=len(playlists) + 1,
        top_menu=len(playlists),
        titles=tuple(range(len(playlists))),
    )
    return SeasonNavigation(
        index=index,
        movie_objects=build_movie_objects(objects),
        playlists=tuple(playlists),
    )
