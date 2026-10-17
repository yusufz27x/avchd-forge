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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .errors import (
    JobCancelled,
    MuxError,
    PrepError,
    ProbeError,
    VerificationError,
)
from .plan import JobPlan, PlannedTrack
from .prep import PrepStep, check_prep_output
from .probe import AudioTrack, MediaInfo, SubtitleTrack, VideoTrack, probe
from .process import CompletedRun, LineCallback, ProgressCallback, Runner
from .progress import FfmpegProgressScanner, TsmuxerScanner, is_mux_success
from .tracks import codec_family

#: Stages a job passes through. ``probe`` and ``plan`` are emitted by the Qt job
#: layer (``jobs.py``) before this module starts working; the rest come from here.
StageKey = Literal["probe", "plan", "prepare", "mux", "verify", "cleanup"]
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
    measurable = 1.0 - TAIL_SHARE

    def announce(key: StageKey, label: str, index: int = 0, total: int = 1) -> None:
        if on_stage is not None:
            on_stage(JobStage(key=key, label=label, index=index, total=total))

    def progress(fraction: float | None, base: float, span: float) -> None:
        """Report ``fraction`` of a phase holding ``span`` weight after ``base``."""
        if on_progress is None:
            return
        if fraction is None:
            on_progress(None)
            return
        done = base + span * min(max(fraction, 0.0), 1.0)
        on_progress(min(max(measurable * done / total_weight, 0.0), 1.0))

    def check_cancel(stage: str) -> None:
        if runner.cancelled:
            raise JobCancelled(stage)

    try:
        for index, step in enumerate(plan.prep_steps):
            check_cancel("preparing tracks")
            announce("prepare", step.label, index, len(plan.prep_steps))
            base = index * PREP_STEP_WEIGHT
            progress(0.0, base, PREP_STEP_WEIGHT)
            run_prep_step(
                plan,
                step,
                runner,
                ffprobe=ffprobe,
                on_line=on_line,
                on_progress=lambda value, base=base: progress(
                    value, base, PREP_STEP_WEIGHT
                ),
            )
            progress(1.0, base, PREP_STEP_WEIGHT)

        check_cancel("muxing")
        announce("mux", f"muxing {plan.source.name}")
        progress(0.0, prep_weight, MUX_WEIGHT)
        mux_result = run_mux(
            plan,
            runner,
            on_line=on_line,
            on_progress=lambda value: progress(value, prep_weight, MUX_WEIGHT),
        )
        progress(1.0, prep_weight, MUX_WEIGHT)

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
        if on_progress is not None:
            on_progress(None)  # probe work is not measurable, show a busy bar
        report = _verification_report(plan, runner, mux_result, ffprobe=ffprobe, on_line=on_line)
        if not report.ok:
            raise VerificationError(report.problems, plan.output_dir)

        temp_kept = keep_temp_on_success
        if not temp_kept:
            announce("cleanup", "cleaning up")
            _clean_temp(plan, owned=temp_owned)
        if on_progress is not None:
            on_progress(1.0)

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
    the codec the step promised.
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
    """Write the meta file and run tsMuxeR; raise :class:`MuxError` on failure."""
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
    if source is not None and (stream.width, stream.height) != (source.width, source.height):
        return (
            f"video resolution is {stream.width}x{stream.height}, "
            f"expected {source.width}x{source.height}"
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
    "MUX_WEIGHT",
    "PREP_STEP_WEIGHT",
    "REQUIRED_BDMV_DIRS",
    "REQUIRED_BDMV_FILES",
    "SPLIT_MB_BYTES",
    "SPLIT_OVERSHOOT_ALLOWANCE",
    "STREAM_DIR",
    "TAIL_SHARE",
    "USB_FILE_RENAMES",
    "USB_SUFFIX_RENAMES",
    "JobOutcome",
    "JobStage",
    "StageCallback",
    "StageKey",
    "VerificationReport",
    "check_output_tree",
    "check_stream_composition",
    "execute_plan",
    "find_streams",
    "rename_tree_for_usb",
    "run_mux",
    "run_prep_step",
    "verify_output",
]
