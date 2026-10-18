"""Executing a :class:`~avchdforge.plan.JobPlan`: prepare, mux, verify, clean up.

tsMuxeR 2.7.0 exits 0 while quietly producing wrong output — a phantom 0-channel
AC-3 track, zero video frames, or a track that never made it into the file
(``docs/CONTEXT.md`` §2.2) — so this module never trusts an exit code. After the
mux the tree is renamed to the AVCHD-on-FAT32 (8.3) names a PS3 needs to mount
AVCHD from a USB drive (§2.11), the renamed tree is checked, the first stream
part is probed, and the streams found there are compared one by one with
``plan.tracks`` (decisions D4/D10/D22).

Nothing here imports Qt and nothing spawns a process by itself: everything goes
through the :class:`~avchdforge.process.Runner` and plain callbacks, which is what
keeps the whole flow unit-testable with a ``FakeRunner``.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .drives import filesystem_type, free_bytes
from .errors import (
    JobCancelled,
    MuxError,
    OutOfSpaceError,
    PrepError,
    ProbeError,
    SeasonMergeError,
    VerificationError,
    looks_like_no_space,
)
from .plan import AVCHD_DIR_NAME, JobPlan, PlannedTrack
from .prep import PrepStep, check_prep_output
from .probe import AudioTrack, MediaInfo, SubtitleTrack, VideoTrack, probe
from .process import CompletedRun, LineCallback, ProgressCallback, Runner
from .progress import FfmpegProgressScanner, TsmuxerScanner, is_mux_success
from .season import SeasonBuilder
from .tracks import codec_family

#: Stages a job passes through. ``probe`` and ``plan`` are emitted by the Qt job
#: layer (``jobs.py``) before this module starts working; the rest come from here.
StageKey = Literal["probe", "plan", "prepare", "mux", "verify", "merge", "cleanup"]
StageCallback = Callable[["JobStage"], None]

#: The AVCHD tree tsMuxeR's ``--avchd`` mode writes (``docs/CONTEXT.md`` §2.1).
BDMV_DIR: Final[str] = "BDMV"
STREAM_DIR: Final[str] = "STREAM"
BACKUP_DIR: Final[str] = "BACKUP"
#: Every required ``BDMV`` file, with the names that satisfy it: tsMuxeR's
#: optical-media name first, the AVCHD-on-FAT32 (PS3 USB) name second.
REQUIRED_BDMV_FILES: Final[tuple[tuple[str, ...], ...]] = (
    ("index.bdmv", "INDEX.BDM"),
    ("MovieObject.bdmv", "MOVIEOBJ.BDM"),
)
#: Sub-directories that must contain at least one matching file (same rule).
REQUIRED_BDMV_DIRS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("PLAYLIST", ("*.mpls", "*.MPL")),
    ("CLIPINF", ("*.clpi", "*.CPI")),
    (STREAM_DIR, ("*.m2ts", "*.MTS")),
)
#: The renames that turn tsMuxeR's tree into the FAT32 flavour a PS3 mounts from
#: USB (``docs/CONTEXT.md`` §2.11). Applied in ``BDMV`` and ``BDMV/BACKUP``.
USB_FILE_RENAMES: Final[tuple[tuple[str, str], ...]] = (
    ("index.bdmv", "INDEX.BDM"),
    ("MovieObject.bdmv", "MOVIEOBJ.BDM"),
)
USB_SUFFIX_RENAMES: Final[tuple[tuple[str, str], ...]] = (
    ("PLAYLIST/*.mpls", ".MPL"),
    ("CLIPINF/*.clpi", ".CPI"),
    ("STREAM/*.m2ts", ".MTS"),
)
#: Entries tsMuxeR may legitimately create next to each other.
EXPECTED_TOP_LEVEL: Final[frozenset[str]] = frozenset({"BDMV", "CERTIFICATE"})

#: tsMuxeR's ``MB`` is decimal — ``tsMuxer.cpp::parseMuxOpt`` maps "MB" to
#: 1,000,000 and "MiB" to 1,048,576 — so ``--split-size=4096MB`` is
#: 4,096,000,000 bytes and leaves ~199 MB under the FAT32 file limit.
SPLIT_MB_BYTES: Final[int] = 1_000_000
#: tsMuxeR splits only at a keyframe *after* the size was exceeded
#: (``isSplitPoint``), so a chunk overshoots its target by up to one GOP. 32 MiB
#: covers a long GOP at AVCHD bitrates while staying far below the FAT32 ceiling.
SPLIT_OVERSHOOT_ALLOWANCE: Final[int] = 32 * 1024 * 1024
#: Largest file FAT32 can store (4 GiB - 1).
FAT32_MAX_BYTES: Final[int] = 4_294_967_295


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobStage:
    """What the job is doing right now (for the status label and progress)."""

    key: StageKey
    label: str
    index: int = 0
    total: int = 1


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """What the post-mux checks found.

    ``problems`` make the job fail; ``warnings`` are shown but do not. ``details``
    is what was actually seen (stream layout, files) for the log pane.
    """

    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    verified_file: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def merged(self, other: VerificationReport) -> VerificationReport:
        return VerificationReport(
            problems=self.problems + other.problems,
            warnings=self.warnings + other.warnings,
            details=self.details + other.details,
            verified_file=other.verified_file or self.verified_file,
        )


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """A finished, verified job."""

    plan: JobPlan
    stream_files: tuple[Path, ...]
    mux_output: str
    verified_file: Path | None = None
    warnings: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    #: True when the temp directory was deliberately left in place.
    temp_dir_kept: bool = False

    @property
    def output_dir(self) -> Path:
        """The ``AVCHD`` folder the tree was written into."""
        return self.plan.output_dir

    @property
    def temp_dir(self) -> Path:
        return self.plan.temp_dir


@dataclass(frozen=True, slots=True)
class SeasonOutcome:
    """A finished, verified season: one title per episode in one tree.

    ``plans[i]``'s tree became title ``i + 1`` and runs ``playlists[i]``
    (:class:`avchdforge.season.MergedSeason`). ``verified_file`` has no season
    counterpart: every episode's own tree was probed before the merge and the
    merged tree was checked file by file after it.
    """

    plans: tuple[JobPlan, ...]
    #: The merged ``AVCHD`` folder, ready for the drive.
    root: Path
    playlists: tuple[int, ...]
    stream_files: tuple[Path, ...]
    mux_outputs: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    #: The directory the episode trees were staged in (one subdirectory each).
    temp_dir: Path | None = None
    temp_dir_kept: bool = False

    @property
    def title_count(self) -> int:
        return len(self.plans)

    @property
    def output_dir(self) -> Path:
        return self.root


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


def find_streams(output_dir: Path | str) -> tuple[Path, ...]:
    """Every produced stream part, whichever AVCHD flavour it is named in."""
    stream_dir = Path(output_dir) / BDMV_DIR / STREAM_DIR
    if not stream_dir.is_dir():
        return ()
    return tuple(sorted({*stream_dir.glob("*.m2ts"), *stream_dir.glob("*.MTS")}))


def check_output_tree(
    output_dir: Path | str, *, split_size_mb: int | None = None
) -> VerificationReport:
    """Check the AVCHD file layout tsMuxeR is supposed to have written.

    Pure filesystem inspection: no tool runs here. Every required file is
    accepted under either AVCHD name — tsMuxeR's optical flavour (``index.bdmv``)
    and the FAT32 flavour a PS3 mounts from USB (``INDEX.BDM``, §2.11). A missing
    ``BDMV`` short-circuits the rest — there is nothing to look at, and the probe
    phase would only add noise. ``split_size_mb`` compares every stream part
    against the size tsMuxeR was asked to split at, plus
    :data:`SPLIT_OVERSHOOT_ALLOWANCE` for the keyframe boundary; chunks over the
    bare target but within the allowance are reported in ``details`` rather than
    as problems.
    """
    root = Path(output_dir)
    bdmv = root / BDMV_DIR
    if not bdmv.is_dir():
        return VerificationReport(
            problems=(f"{bdmv} is missing: tsMuxeR did not write an AVCHD structure",)
        )

    problems: list[str] = []
    warnings: list[str] = []
    details: list[str] = []

    for names in REQUIRED_BDMV_FILES:
        found = next((bdmv / name for name in names if (bdmv / name).is_file()), None)
        if found is None:
            problems.append(f"{bdmv}: no {' or '.join(names)} file")
        elif found.stat().st_size == 0:
            problems.append(f"{found} is empty")

    for name, patterns in REQUIRED_BDMV_DIRS:
        directory = bdmv / name
        entries = (
            sorted({entry for pattern in patterns for entry in directory.glob(pattern)})
            if directory.is_dir()
            else []
        )
        if not entries:
            problems.append(f"{directory} contains no {' or '.join(patterns)} file")
        else:
            details.append(f"{name}: {', '.join(entry.name for entry in entries)}")

    target = split_size_mb * SPLIT_MB_BYTES if split_size_mb else None
    for stream in find_streams(root):
        size = stream.stat().st_size
        if size == 0:
            problems.append(f"{stream.name} is empty")
        elif target is not None and size > target + SPLIT_OVERSHOOT_ALLOWANCE:
            problems.append(
                f"{stream.name} is {size} bytes, far over the requested "
                f"{split_size_mb} MB split: tsMuxeR did not split this output"
            )
        elif target is not None and size > target:
            details.append(
                f"{stream.name} is {size} bytes, {size - target} over the "
                f"{split_size_mb} MB split target (tsMuxeR splits at keyframes)"
            )

    strays = sorted(
        entry.name for entry in root.iterdir() if entry.name not in EXPECTED_TOP_LEVEL
    )
    if strays:
        warnings.append("unexpected entries in the output folder: " + ", ".join(strays))

    return VerificationReport(
        problems=tuple(problems), warnings=tuple(warnings), details=tuple(details)
    )


def check_stream_composition(plan: JobPlan, output: MediaInfo) -> VerificationReport:
    """Compare the probed streams of the produced file with ``plan.tracks``.

    tsMuxeR reports success even when ``track=`` pointed at the wrong stream, so
    every planned stream must be found in the output, in the same order and with
    the expected codec family. Extracted cores are the exception to the channel
    rule: stripping a DD+/Atmos or TrueHD extension can legitimately reduce the
    channel count (``eac3_core`` "drops extra channels"), so those tracks only have
    to be non-zero channel audio of the right family.
    """
    problems: list[str] = []
    details: list[str] = []

    wanted_video = [track for track in plan.tracks if track.kind == "video"]
    wanted_audio = [track for track in plan.tracks if track.kind == "audio"]
    wanted_subs = [track for track in plan.tracks if track.kind == "subtitle"]

    problems.extend(_count_problems("video", wanted_video, output.videos))
    problems.extend(_count_problems("audio", wanted_audio, output.audio))
    problems.extend(_count_problems("subtitle", wanted_subs, output.subtitles))

    if len(output.videos) == len(wanted_video):
        for track, stream in zip(wanted_video, output.videos, strict=True):
            if (problem := _video_problem(plan, track, stream)) is not None:
                problems.append(problem)
    if len(output.audio) == len(wanted_audio):
        for track, stream in zip(wanted_audio, output.audio, strict=True):
            if (problem := _audio_problem(track, stream)) is not None:
                problems.append(problem)
    if len(output.subtitles) == len(wanted_subs):
        for track, stream in zip(wanted_subs, output.subtitles, strict=True):
            if (problem := _subtitle_problem(track, stream)) is not None:
                problems.append(problem)

    if not problems:
        details.append(f"stream layout confirmed: {_describe_actual(output.streams)}")

    return VerificationReport(problems=tuple(problems), details=tuple(details))


def _count_problems(kind: str, wanted: list[PlannedTrack], actual: tuple) -> list[str]:
    if len(actual) == len(wanted):
        return []
    return [
        f"{kind} streams: expected {len(wanted)} ({_describe_planned(wanted)}), "
        f"found {len(actual)} ({_describe_actual(actual)})"
    ]


def verify_output(
    plan: JobPlan, runner: Runner, *, ffprobe: str = "ffprobe"
) -> VerificationReport:
    """Check the tree, then probe the first stream part and compare the streams.

    Only the first part is probed: with ``--split-size`` tsMuxeR repeats every
    stream in every part (verified), so one part is representative and a multi-GB
    disc does not need one ffprobe run per chunk.
    """
    report = check_output_tree(plan.output_dir, split_size_mb=plan.mux.options.split_size_mb)
    streams = find_streams(plan.output_dir)
    if not streams:
        return report

    target = streams[0]
    try:
        media = probe(target, runner, tool=ffprobe)
    except ProbeError as exc:
        if runner.cancelled:
            raise JobCancelled("verifying") from None
        return report.merged(
            VerificationReport(
                problems=(f"could not inspect {target.name} with ffprobe: {exc}",)
            )
        )

    composition = check_stream_composition(plan, media)
    return report.merged(composition).merged(VerificationReport(verified_file=target))


# --------------------------------------------------------------------------
# PS3 USB naming
# --------------------------------------------------------------------------


def rename_tree_for_usb(output_dir: Path | str) -> tuple[str, ...]:
    """Rename tsMuxeR's tree to the AVCHD-on-FAT32 names a PS3 mounts from USB.

    tsMuxeR always writes the optical-media flavour — ``index.bdmv``,
    ``MovieObject.bdmv``, ``00000.mpls``, ``00000.clpi``, ``00000.m2ts`` — but a
    PS3 only mounts AVCHD from a USB drive when the legacy 8.3 names are present
    (``docs/CONTEXT.md`` §2.11). With the optical names the drive's files are
    still browsable and the raw stream parts play as ordinary video, but the
    AVCHD disc never appears in the XMB.

    Nothing inside any file is touched: playlists and clip info reference clips
    as ``00000M2TS`` and players accept either extension. Returns one
    ``"old -> new"`` string per rename; a second call is a no-op.
    """
    root = Path(output_dir)
    renamed: list[str] = []
    for base in (root / BDMV_DIR, root / BDMV_DIR / BACKUP_DIR):
        if not base.is_dir():
            continue
        for source_name, target_name in USB_FILE_RENAMES:
            source = base / source_name
            if source.is_file():
                _rename_path(source, base / target_name)
                renamed.append(f"{source.name} -> {target_name}")
        for pattern, suffix in USB_SUFFIX_RENAMES:
            for source in sorted(base.glob(pattern)):
                target = source.with_suffix(suffix)
                _rename_path(source, target)
                renamed.append(f"{source.name} -> {target.name}")
    return tuple(renamed)


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

#: Run-bar weight of one prep step: it reads the whole source once.
PREP_STEP_WEIGHT: Final[float] = 1.0
#: Run-bar weight of the mux: one full read of the source plus one full write
#: of the output, where a prep step only reads.
MUX_WEIGHT: Final[float] = 2.0
#: Share of the bar reserved for the work after the mux — the write-buffer
#: flush, the stream index and playlist, the AVCHD-on-FAT32 rename,
#: verification and cleanup. That tail is seconds against the mux's minutes,
#: so it must not hold a whole unit of the bar the way a prep step does: with
#: a unit of its own the mux ended at ``(steps + 1) / (steps + 2)`` of the bar
#: — 80% with three prep steps — and everything after it jumped to 100%.
TAIL_SHARE: Final[float] = 0.02
#: Run-bar weight of a season's merge: it hands the whole season to the
#: destination once — a copy, or a rename per file when both sides share a
#: filesystem — so one unit of the bar is the right price for it. The transfer
#: happens an episode at a time, each episode allotted its share of that unit
#: (``execute_season``), which keeps the bar's total the same.
MERGE_WEIGHT: Final[float] = MUX_WEIGHT

#: Room the space check wants in the temp directory on top of the plan's own
#: estimates. An extracted DTS-HD/TrueHD core or a converted subtitle is a
#: fraction of this, so it covers them without guessing their sizes; the mux's
#: own working files (meta text, playlist, clip info) are smaller still.
TEMP_HEADROOM: Final[int] = 1 << 30

#: Advice for a temp directory that is too small. The system default is the
#: trap: ``tempfile.gettempdir()`` is ``/tmp``, which many distributions mount
#: as a memory-backed tmpfs of a few GB — a season that stages whole episodes
#: there runs out of RAM, not out of disk (D29).
TEMP_SPACE_HINT: Final[str] = (
    "Free space there, or point the Temporary files folder (Preferences → "
    "Paths) at a drive with room. A temp folder on the output's drive also "
    "lets a season move each episode instead of copying it."
)

#: Advice for an output drive that is full (a single job muxes straight into it).
OUTPUT_SPACE_HINT: Final[str] = (
    "Free space on that drive, or choose another output folder."
)


class _RunBar:
    """One run's progress arithmetic: phase fractions onto a single 0..1 bar.

    Every phase declares the weight of the data it moves (the constants above);
    ``total_weight`` is their sum. All but :data:`TAIL_SHARE` of the bar is
    measurable, so the post-mux tail — the write-buffer flush, verification,
    the AVCHD-on-FAT32 rename and cleanup — takes the last few percent whatever
    the run did before. A ``None`` fraction means "working, not measurable" and
    shows as a busy bar.
    """

    def __init__(
        self,
        total_weight: float,
        *,
        on_progress: ProgressCallback | None,
        on_stage: StageCallback | None,
    ) -> None:
        self._total = total_weight
        self._measurable = 1.0 - TAIL_SHARE
        self._on_progress = on_progress
        self._on_stage = on_stage

    def announce(self, key: StageKey, label: str, index: int = 0, total: int = 1) -> None:
        if self._on_stage is not None:
            self._on_stage(JobStage(key=key, label=label, index=index, total=total))

    def phase(self, fraction: float | None, base: float, span: float) -> None:
        """Report ``fraction`` of a phase holding ``span`` weight after ``base``."""
        if self._on_progress is None:
            return
        if fraction is None:
            self._on_progress(None)
            return
        done = base + span * min(max(fraction, 0.0), 1.0)
        self._on_progress(min(max(self._measurable * done / self._total, 0.0), 1.0))

    def busy(self) -> None:
        """Unmeasurable work is coming (a probe, a copy): show a busy bar."""
        if self._on_progress is not None:
            self._on_progress(None)

    def done(self) -> None:
        if self._on_progress is not None:
            self._on_progress(1.0)


def temp_space_needed(plan: JobPlan) -> int | None:
    """Rough bytes ``plan`` needs in its temp directory at once, or ``None``.

    What the plan writes there is what matters: a repaired video stream (a
    window fix) is source-sized, while audio cores and converted subtitles are
    small enough for :data:`TEMP_HEADROOM`. When the mux itself writes into temp
    — a season stages each episode at ``plan.temp_dir/AVCHD`` — its output tree
    is source-sized too and is counted. The estimates are the plan's own
    (:attr:`~avchdforge.plan.JobPlan.estimated_size_bytes`), which makes the
    check as good as the mux's own write gauge.
    """
    source = plan.estimated_size_bytes or plan.media.size_bytes
    if not source:
        return None
    needed = TEMP_HEADROOM
    if any(step.kind == "video-window" for step in plan.prep_steps):
        needed += source
    if plan.output_dir.is_relative_to(plan.temp_dir):
        needed += source
    return needed


def check_temp_space(plan: JobPlan) -> None:
    """Fail before a step writes when the temp directory cannot hold the plan.

    Naming the folder, the estimate and the free space is the difference
    between a diagnosis and a puzzle: a tool that runs out of room reports a
    bare exit code (ffmpeg's ENOSPC surfaces as ``228``), which reads as a bug
    in the tool rather than as a full filesystem — often the RAM-backed
    ``/tmp`` a season's staged episodes do not fit in (D29).
    """
    needed = temp_space_needed(plan)
    if needed is None:
        return
    free = free_bytes(plan.temp_dir)
    if free is None or free >= needed:
        return
    raise OutOfSpaceError(
        plan.temp_dir,
        needed_bytes=needed,
        free_bytes=free,
        hint=_temp_space_hint(plan.temp_dir),
    )


def _temp_space_hint(path: Path) -> str:
    """The space advice for ``path``, calling out a memory-backed filesystem."""
    if filesystem_type(path) == "tmpfs":
        return (
            f"{path} is a memory-backed (tmpfs) filesystem, so its free space "
            f"is RAM, not disk. {TEMP_SPACE_HINT}"
        )
    return TEMP_SPACE_HINT


def _mux_one(
    plan: JobPlan,
    runner: Runner,
    *,
    bar: _RunBar,
    base: float,
    weight: float,
    ffprobe: str,
    on_line: LineCallback | None,
    prefix: str = "",
) -> CompletedRun:
    """Run one plan's preparation and mux inside the bar slice ``[base, base+weight]``.

    The preparation steps and the mux read the same source once each, but the
    mux also writes the output, hence the weights (:data:`PREP_STEP_WEIGHT`
    against :data:`MUX_WEIGHT`). ``prefix`` labels the stage names for a season
    — ``"episode 2/3: "`` — and stays empty for a single job.

    The temp directory is checked for room first: this is the last point before
    data is written, and the first point where an out-of-space run can be
    reported as one instead of as a tool failure (D29).
    """

    def stage(text: str) -> str:
        return f"{prefix}{text}"

    check_temp_space(plan)
    prep_weight = len(plan.prep_steps) * PREP_STEP_WEIGHT
    for index, step in enumerate(plan.prep_steps):
        if runner.cancelled:
            raise JobCancelled(stage("preparing tracks"))
        bar.announce("prepare", stage(step.label), index, len(plan.prep_steps))
        step_base = base + index * PREP_STEP_WEIGHT
        bar.phase(0.0, step_base, PREP_STEP_WEIGHT)
        run_prep_step(
            plan,
            step,
            runner,
            ffprobe=ffprobe,
            on_line=on_line,
            on_progress=lambda value, step_base=step_base: bar.phase(
                value, step_base, PREP_STEP_WEIGHT
            ),
        )
        bar.phase(1.0, step_base, PREP_STEP_WEIGHT)

    if runner.cancelled:
        raise JobCancelled(stage("muxing"))
    bar.announce("mux", stage(f"muxing {plan.source.name}"))
    mux_base = base + prep_weight
    mux_weight = weight - prep_weight
    bar.phase(0.0, mux_base, mux_weight)
    mux_result = run_mux(
        plan,
        runner,
        on_line=on_line,
        on_progress=lambda value: bar.phase(value, mux_base, mux_weight),
    )
    bar.phase(1.0, mux_base, mux_weight)
    return mux_result


def execute_plan(
    plan: JobPlan,
    runner: Runner,
    *,
    ffprobe: str = "ffprobe",
    on_line: LineCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_stage: StageCallback | None = None,
    keep_temp_on_success: bool = False,
    keep_temp_on_failure: bool = True,
    owns_temp: bool | None = None,
) -> JobOutcome:
    """Run the whole plan and return the verified result, or raise.

    ``owns_temp`` says whether ``plan.temp_dir`` may be deleted outright. By
    default it is inferred from whether the directory already existed, which is
    right for callers that let this function create it; a caller that pre-created a
    per-run directory (``jobs.py`` uses ``mkdtemp``) passes ``owns_temp=True``. A
    shared, pre-existing directory is only emptied of the files this job wrote,
    never deleted.

    On failure the temp directory is kept by default (event evidence), on success
    it is removed unless ``keep_temp_on_success`` says otherwise. Cancellation
    always keeps it, whatever the failure policy says: a cancelled job is the one
    case where the user still wants to see what happened. Either way the caller
    decides; nothing is written outside ``temp_dir`` and ``output_dir``.
    """
    temp_owned = (not plan.temp_dir.exists()) if owns_temp is None else owns_temp
    plan.temp_dir.mkdir(parents=True, exist_ok=True)
    # The bar weighs the phases by the data they move (see the constants above):
    # every prep step and the mux share everything but TAIL_SHARE.
    prep_weight = len(plan.prep_steps) * PREP_STEP_WEIGHT
    total_weight = prep_weight + MUX_WEIGHT
    bar = _RunBar(total_weight, on_progress=on_progress, on_stage=on_stage)
    announce = bar.announce

    def check_cancel(stage: str) -> None:
        if runner.cancelled:
            raise JobCancelled(stage)

    try:
        mux_result = _mux_one(
            plan,
            runner,
            bar=bar,
            base=0.0,
            weight=total_weight,
            ffprobe=ffprobe,
            on_line=on_line,
        )

        # Convert before verifying, so the checks inspect exactly what ships: a
        # PS3 only mounts the FAT32 flavour from USB (docs/CONTEXT.md §2.11).
        renamed = rename_tree_for_usb(plan.output_dir)
        if renamed and on_line is not None:
            on_line(
                f"[avchd-forge] renamed {len(renamed)} files to the "
                f"AVCHD-on-FAT32 (PS3 USB) names\n"
            )

        check_cancel("verifying")
        announce("verify", "verifying the AVCHD structure")
        bar.busy()  # probe work is not measurable, show a busy bar
        report = _verification_report(plan, runner, mux_result, ffprobe=ffprobe, on_line=on_line)
        if not report.ok:
            raise VerificationError(report.problems, plan.output_dir)

        temp_kept = keep_temp_on_success
        if not temp_kept:
            announce("cleanup", "cleaning up")
            _clean_temp(plan, owned=temp_owned)
        bar.done()

        return JobOutcome(
            plan=plan,
            stream_files=find_streams(plan.output_dir),
            mux_output=mux_result.output,
            verified_file=report.verified_file,
            warnings=report.warnings,
            details=report.details,
            temp_dir_kept=temp_kept,
        )
    except JobCancelled:
        # Not an error and never cleaned up: a cancelled job is the one case where
        # the user still wants the evidence, whatever keep_temp_on_failure says.
        raise
    except BaseException:
        if not keep_temp_on_failure:
            _clean_temp(plan, owned=temp_owned)
        raise


def execute_season(
    plans: Sequence[JobPlan],
    runner: Runner,
    *,
    destination: Path | str,
    ffprobe: str = "ffprobe",
    on_line: LineCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_stage: StageCallback | None = None,
    pump: Callable[[], None] | None = None,
    keep_temp_on_success: bool = False,
    keep_temp_on_failure: bool = True,
    owns_temp: bool | None = None,
) -> SeasonOutcome:
    """Mux every episode and merge the trees into one AVCHD folder.

    Each plan must be staged for a season run: its mux destination is a scratch
    folder of its own, ``plan.temp_dir/AVCHD`` (what a plan built with
    ``output_dir=plan.temp_dir`` produces), and every episode's scratch folder
    must share one parent — the staging root, which is what ``owns_temp`` and
    the temp policy apply to. The merged tree lands in ``destination``, which
    must be empty or missing.

    Every episode is muxed and verified on its own tree first (tsMuxeR's output
    is checked before it is merged, so a bad episode cannot poison a season),
    and then handed to the season folder **right away**: the season is built one
    episode at a time by :class:`~avchdforge.season.SeasonBuilder`, and the
    episode's scratch files are dropped as soon as it is in. The temp directory
    holds about one episode instead of the whole season (D29), which is what
    makes a season run fit on a normal temp drive at all. ``move`` is implied
    unless the caller asked for the temp directory to survive — keeping the temp
    means keeping the staged trees, so the merge copies in that case, and then
    the temp directory still grows with the whole season.

    That incremental merge changes the failure semantics: a season that fails or
    is cancelled part-way leaves the episodes already merged in ``destination``
    (a partial season, which must be deleted before a retry) and keeps the
    scratch of the episodes that were not merged. Failures inside a transfer
    never leave a half-written stream file behind.

    ``pump`` is called between the transfer's blocks; cancellation is asked from
    ``runner.cancelled`` the same way, so a GUI caller can pass
    ``QCoreApplication.processEvents`` and keep its window painting (and its
    Cancel button clickable) through a multi-GB copy.
    """
    plans = tuple(plans)
    if not plans:
        raise SeasonMergeError("a season needs at least one episode plan")
    root = Path(destination)
    staging_root = Path(plans[0].temp_dir).parent
    for number, plan in enumerate(plans, start=1):
        if plan.output_dir != plan.temp_dir / AVCHD_DIR_NAME:
            raise SeasonMergeError(
                f"episode {number} ({plan.source.name}): a season muxes into "
                f"<scratch>/AVCHD (got {plan.output_dir} for scratch {plan.temp_dir})"
            )
        if Path(plan.temp_dir).parent != staging_root:
            raise SeasonMergeError(
                f"episode {number} ({plan.source.name}): every episode's scratch "
                f"folder must share one parent (got {plan.temp_dir})"
            )
    temp_owned = (not staging_root.exists()) if owns_temp is None else owns_temp
    # Prep steps write into the scratch dir before the mux creates its output
    # tree, exactly as in execute_plan; the caller may already have made it.
    for plan in plans:
        plan.temp_dir.mkdir(parents=True, exist_ok=True)

    count = len(plans)
    # One merge unit for the season, sliced per episode: each episode carries its
    # own share of the transfer, which is when that share is spent.
    transfer_share = MERGE_WEIGHT / count
    weights = [
        len(plan.prep_steps) * PREP_STEP_WEIGHT + MUX_WEIGHT + transfer_share
        for plan in plans
    ]
    bar = _RunBar(sum(weights), on_progress=on_progress, on_stage=on_stage)
    warnings: list[str] = []
    details: list[str] = []
    mux_outputs: list[str] = []
    builder = SeasonBuilder(
        root,
        # A kept temp directory means the caller wants the staged trees to
        # survive, so the merge has to copy instead of consuming them.
        move=not keep_temp_on_success,
        should_cancel=lambda: runner.cancelled,
        pump=pump,
    )

    try:
        base = 0.0
        for index, (plan, weight) in enumerate(zip(plans, weights, strict=True), start=1):
            prefix = f"episode {index}/{count}: "
            transfer_base = base + weight - transfer_share
            mux_result = _mux_one(
                plan,
                runner,
                bar=bar,
                base=base,
                weight=weight - transfer_share,
                ffprobe=ffprobe,
                on_line=on_line,
                prefix=prefix,
            )
            mux_outputs.append(mux_result.output)
            base = transfer_base

            # Verify the episode on its own staging tree, before the merge: the
            # merge only renumbers names and playlist references (season.py), so
            # a composition that held here holds in the merged tree.
            if runner.cancelled:
                raise JobCancelled(f"{prefix}verifying")
            bar.announce("verify", f"{prefix}verifying the AVCHD structure")
            bar.busy()
            report = _verification_report(
                plan, runner, mux_result, ffprobe=ffprobe, on_line=on_line
            )
            if not report.ok:
                raise VerificationError(report.problems, plan.output_dir)
            warnings.extend(report.warnings)
            details.extend(report.details)

            # Hand the verified episode to the season folder right away, then
            # drop its scratch files — that is what keeps the staging area at
            # about one episode while the rest of the season is still unwritten
            # (D29). A kept temp directory keeps them all instead.
            if runner.cancelled:
                raise JobCancelled(f"{prefix}merging into the season folder")
            bar.announce(
                "merge", f"{prefix}merging into the season folder", index, count
            )
            bar.phase(0.0, transfer_base, transfer_share)
            builder.add(
                plan.output_dir,
                on_progress=lambda value, start=transfer_base: bar.phase(
                    value, start, transfer_share
                ),
            )
            bar.phase(1.0, transfer_base, transfer_share)
            base += transfer_share
            if not keep_temp_on_success:
                _clean_episode_temp(plan)

        merged = builder.finish()

        renamed = rename_tree_for_usb(root)
        if renamed and on_line is not None:
            on_line(
                f"[avchd-forge] renamed {len(renamed)} files to the "
                f"AVCHD-on-FAT32 (PS3 USB) names\n"
            )

        bar.announce("verify", "verifying the merged AVCHD structure")
        bar.busy()
        tree = check_output_tree(root, split_size_mb=_shared_split_size(plans))
        if not tree.ok:
            raise VerificationError(tree.problems, root)
        warnings.extend(tree.warnings)
        details.extend(tree.details)
        if on_line is not None:
            for detail in tree.details:
                on_line(f"[avchd-forge] {detail}\n")
            for warning in tree.warnings:
                on_line(f"[avchd-forge] warning: {warning}\n")

        temp_kept = keep_temp_on_success
        if not temp_kept:
            bar.announce("cleanup", "cleaning up")
            _clean_season_temp(plans, staging_root, owned=temp_owned)
        bar.done()

        return SeasonOutcome(
            plans=plans,
            root=root,
            playlists=merged.playlists,
            stream_files=find_streams(root),
            mux_outputs=tuple(mux_outputs),
            warnings=tuple(warnings),
            details=tuple(details),
            temp_dir=staging_root,
            temp_dir_kept=temp_kept,
        )
    except JobCancelled:
        raise
    except BaseException:
        if not keep_temp_on_failure:
            _clean_season_temp(plans, staging_root, owned=temp_owned)
        raise


def _shared_split_size(plans: Sequence[JobPlan]) -> int | None:
    """The one split size every episode muxed with, or ``None`` when they differ."""
    targets = {plan.mux.options.split_size_mb for plan in plans}
    return targets.pop() if len(targets) == 1 else None


def _clean_season_temp(
    plans: Sequence[JobPlan], staging_root: Path, *, owned: bool
) -> None:
    """Drop a season's staged episode trees — the whole root, or only this run's files."""
    if owned:
        shutil.rmtree(staging_root, ignore_errors=True)
        return
    for plan in plans:
        _clean_temp(plan, owned=False)
        shutil.rmtree(plan.output_dir, ignore_errors=True)


def _clean_episode_temp(plan: JobPlan) -> None:
    """Drop one episode's scratch files once its tree is in the season folder.

    By then the tree itself is empty (the merge moved its files), and the files
    the plan wrote next to it — meta file, prepared streams — are not needed any
    more. Cleaning per episode instead of all at the end is what keeps a
    season's temp footprint at about one episode (D29).
    """
    _clean_temp(plan, owned=False)
    shutil.rmtree(plan.output_dir, ignore_errors=True)


def run_prep_step(
    plan: JobPlan,
    step: PrepStep,
    runner: Runner,
    *,
    ffprobe: str = "ffprobe",
    on_line: LineCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> MediaInfo:
    """Run one ffmpeg step and verify what it produced.

    Raises :class:`~avchdforge.errors.PrepError` when ffmpeg fails and
    :class:`~avchdforge.errors.VerificationError` when the produced file is not
    the codec the step promised. A failure that is really a full temp
    filesystem — ffmpeg reports ENOSPC as exit code 228 — raises
    :class:`~avchdforge.errors.OutOfSpaceError` instead, naming the folder.
    """
    if on_line is not None:
        on_line(f"$ {step.command_line}\n")
    scanner = FfmpegProgressScanner(duration=plan.media.duration)
    result = runner.run(
        step.argv,
        on_line=on_line,
        scanner=scanner,
        on_progress=on_progress,
        phase=f"prepare:{step.kind}",
    )
    if not result.ok:
        if runner.cancelled:
            raise JobCancelled(step.label)
        output = result.output
        if looks_like_no_space(result.returncode, output):
            # The step writes into the plan's temp directory by construction
            # (prep.py), so that is the folder to name.
            raise OutOfSpaceError(
                plan.temp_dir,
                tool=_tool_name(step.argv[0]),
                returncode=result.returncode,
                output=output,
                hint=_temp_space_hint(plan.temp_dir),
            )
        raise PrepError(
            _tool_name(step.argv[0]),
            step.argv,
            result.returncode,
            result.stderr,
            result.stdout,
            step=step.label,
        )

    media = _probe_or_cancel(step.output, runner, ffprobe, stage=step.label)
    problem = check_prep_output(step, media)
    if problem:
        raise VerificationError([problem], step.output)
    return media


def run_mux(
    plan: JobPlan,
    runner: Runner,
    *,
    on_line: LineCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> CompletedRun:
    """Write the meta file and run tsMuxeR; raise :class:`MuxError` on failure.

    A failure that is really a full filesystem — tsMuxeR writing the tree or
    ffmpeg writing a repaired stream — raises
    :class:`~avchdforge.errors.OutOfSpaceError` instead of :class:`MuxError`,
    naming the folder that ran out of room (D29).
    """
    # plan.output_dir is the AVCHD folder (:func:`avchdforge.plan.avchd_destination`);
    # it may not exist yet when the user pointed at a drive root, and tsMuxeR
    # expects the folder it writes into to be there.
    plan.output_dir.mkdir(parents=True, exist_ok=True)
    plan.meta_path.write_text(plan.meta_text, encoding="utf-8")
    if on_line is not None:
        on_line(f"$ {plan.command_line}\n")
    scanner = TsmuxerScanner()
    result = runner.run(
        plan.mux_argv,
        on_line=on_line,
        scanner=scanner,
        on_progress=on_progress,
        phase="mux",
    )
    if not result.ok:
        if runner.cancelled:
            raise JobCancelled("muxing")
        output = result.output
        if looks_like_no_space(result.returncode, output):
            hint = (
                _temp_space_hint(plan.output_dir)
                if plan.output_dir.is_relative_to(plan.temp_dir)
                else OUTPUT_SPACE_HINT
            )
            raise OutOfSpaceError(
                plan.output_dir,
                tool="tsMuxeR",
                returncode=result.returncode,
                output=output,
                hint=hint,
            )
        raise MuxError(
            "tsMuxeR", plan.mux_argv, result.returncode, result.stderr, result.stdout
        )
    return result


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _verification_report(
    plan: JobPlan,
    runner: Runner,
    mux_result: CompletedRun,
    *,
    ffprobe: str,
    on_line: LineCallback | None,
) -> VerificationReport:
    report = VerificationReport()
    if not is_mux_success(mux_result.output):
        report = report.merged(
            VerificationReport(
                problems=(
                    "tsMuxeR exited 0 but never printed 'Mux successful complete'; "
                    "the mux did not finish normally and its output cannot be trusted",
                )
            )
        )
    report = report.merged(verify_output(plan, runner, ffprobe=ffprobe))
    if on_line is not None:
        for detail in report.details:
            on_line(f"[avchd-forge] {detail}\n")
        for warning in report.warnings:
            on_line(f"[avchd-forge] warning: {warning}\n")
    return report


def _clean_temp(plan: JobPlan, *, owned: bool) -> None:
    if owned:
        shutil.rmtree(plan.temp_dir, ignore_errors=True)
        return
    for path in plan.temp_files:
        path.unlink(missing_ok=True)


def _probe_or_cancel(path: Path, runner: Runner, ffprobe: str, *, stage: str) -> MediaInfo:
    try:
        return probe(path, runner, tool=ffprobe)
    except ProbeError:
        if runner.cancelled:
            raise JobCancelled(stage) from None
        raise


def _tool_name(argv0: str) -> str:
    return Path(argv0).name or argv0


def _rename_path(source: Path, target: Path) -> None:
    """Rename one file, handling the case-only rename a FAT mount needs.

    On a case-insensitive filesystem the target ``exists`` as the same file;
    ``os.replace`` is the portable way to change its case (Windows ``os.rename``
    refuses). A genuinely different file at the target means the tree is not what
    tsMuxeR just wrote, so fail loudly instead of clobbering it.
    """
    if target.exists() and not _same_file(source, target):
        raise OSError(f"refusing to overwrite {target} while converting the tree")
    os.replace(source, target)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:  # pragma: no cover - one side vanished mid-check
        return False


def _video_problem(plan: JobPlan, track: PlannedTrack, stream: VideoTrack) -> str | None:
    family = codec_family(stream.codec)
    if family != track.family:
        return f"video stream is {stream.codec.upper()} ({family}), expected {track.family}"
    source = plan.media.video
    if source is not None:
        expected = source.output_resolution
        if (stream.width, stream.height) != expected:
            return (
                f"video resolution is {stream.width}x{stream.height}, "
                f"expected {expected[0]}x{expected[1]}"
            )
    return None


def _audio_problem(track: PlannedTrack, stream: AudioTrack) -> str | None:
    family = codec_family(stream.codec)
    if family != track.family:
        return (
            f"audio track {track.index} is {stream.codec.upper()} ({family}) in the output, "
            f"expected {track.family} ({track.note or 'copy'})"
        )
    if not stream.channels:
        return (
            f"audio track {track.index} came out with 0 channels: tsMuxeR accepted "
            f"track={track.track_id} but decoded nothing"
        )
    if not track.prepared and track.channels and stream.channels != track.channels:
        return (
            f"audio track {track.index} has {stream.channels} channels, "
            f"expected {track.channels}"
        )
    return None


def _subtitle_problem(track: PlannedTrack, stream: SubtitleTrack) -> str | None:
    family = codec_family(stream.codec)
    if family != track.family:
        return (
            f"subtitle track {track.index} is {stream.codec.upper()} ({family}) in the output, "
            f"expected {track.family} ({track.note or 'copy'})"
        )
    return None


def _describe_planned(tracks: list[PlannedTrack]) -> str:
    parts = [
        track.family + (f" {track.channels}ch" if track.channels else "") for track in tracks
    ]
    return ", ".join(parts) or "none"


def _describe_actual(streams: tuple) -> str:
    parts = []
    for stream in streams:
        label = str(getattr(stream, "codec", "?")).upper()
        if channels := getattr(stream, "channels", None):
            label += f" {channels}ch"
        parts.append(label)
    return ", ".join(parts) or "none"


__all__ = [
    "BACKUP_DIR",
    "BDMV_DIR",
    "EXPECTED_TOP_LEVEL",
    "FAT32_MAX_BYTES",
    "MERGE_WEIGHT",
    "MUX_WEIGHT",
    "OUTPUT_SPACE_HINT",
    "PREP_STEP_WEIGHT",
    "REQUIRED_BDMV_DIRS",
    "REQUIRED_BDMV_FILES",
    "SPLIT_MB_BYTES",
    "SPLIT_OVERSHOOT_ALLOWANCE",
    "STREAM_DIR",
    "TAIL_SHARE",
    "TEMP_HEADROOM",
    "TEMP_SPACE_HINT",
    "USB_FILE_RENAMES",
    "USB_SUFFIX_RENAMES",
    "JobOutcome",
    "JobStage",
    "SeasonOutcome",
    "StageCallback",
    "StageKey",
    "VerificationReport",
    "check_output_tree",
    "check_stream_composition",
    "check_temp_space",
    "execute_plan",
    "execute_season",
    "find_streams",
    "rename_tree_for_usb",
    "run_mux",
    "run_prep_step",
    "temp_space_needed",
    "verify_output",
]
