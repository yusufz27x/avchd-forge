"""Qt-facing job orchestration: run the pipeline without freezing the window.

There is no worker thread here, and none is needed: every external command runs
through :class:`~avchdforge.process.QProcessRunner`, which spins a nested
:class:`QEventLoop` and keeps delivering paint and input events. That is also why
cancellation works — the Cancel button's click *is* delivered while
:func:`~avchdforge.backend.execute_plan` is still on the stack, ``cancel()`` kills
the running child, and the unwinding surfaces as ``cancelled`` instead of as a
tool error.

The one long stretch that no child process covers is a season's merge: moving
the episodes into the season folder is the application's own file I/O and runs on
this thread. ``start_season`` therefore passes
``QCoreApplication.processEvents`` down as the merge's ``pump``, which the
transfer loop calls between blocks — the window keeps painting there too, and a
Cancel click is picked up between two blocks instead of after the whole season.

Two operations are exposed:

``start_probe``
    ffprobe a file, validate the default selection, emit :class:`ProbeOutcome`
    (the source's :class:`~avchdforge.probe.MediaInfo` plus the issues the track
    view has to show). The full ffprobe JSON stays available on
    ``outcome.media.raw`` for a "Save probe data…" action rather than being
    dumped into the log pane.
``start_job``
    build the plan (mandatory tsMuxeR detection pass, then meta text) and hand it
    to the core :func:`~avchdforge.backend.execute_plan`, inside a temp directory
    this job created with ``mkdtemp`` and whose fate follows the temp policy.

Signals are the only interface: no exception ever escapes ``start_*``. Failures
carry the temp directory when it was kept, so the UI can offer "Open temp dir";
cancellations carry what was left in the output directory.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6 import QtCore

from .backend import (
    JobOutcome,
    JobStage,
    SeasonOutcome,
    execute_plan,
    execute_season,
)
from .drives import existing_tree
from .errors import (
    AvchdForgeError,
    ExistingTreeError,
    JobCancelled,
    MuxError,
    PrepError,
    ProbeError,
    ToolExecutionError,
    ValidationFailed,
    VerificationError,
)
from .plan import JobPlan, PlanOptions, avchd_destination, build_plan, describe_plan
from .probe import MediaInfo, probe
from .process import QProcessRunner, Runner
from .selection import TrackSelection, default_selection
from .tracks import detect_tracks
from .validate import validate_all
from .validate_types import ValidationReport

#: Prefix of the per-job temp directory (``mkdtemp``).
TEMP_PREFIX = "avchd-forge-"

#: Label of the stage between "probe" and "prepare": the mandatory detection run.
PLAN_STAGE_LABEL = "reading tsMuxeR track IDs"

#: Stage a season run was cancelled in, for the rare cancellation the run's own
#: stage checks did not catch (the message then reads "Cancelled the season.").
SEASON_STAGE_LABEL = "the season"

RunnerFactory = Callable[[], Runner]

#: Human title per failure type; anything not matched is an internal error.
_FAILURE_TITLES: tuple[tuple[type[BaseException], str], ...] = (
    (ValidationFailed, "This file cannot be used"),
    (ExistingTreeError, "The output folder cannot be used"),
    (ProbeError, "Could not read the input file"),
    (PrepError, "A preparation step failed"),
    (MuxError, "tsMuxeR failed"),
    (VerificationError, "The output failed verification"),
    (ToolExecutionError, "An external tool failed"),
    (AvchdForgeError, "The job failed"),
)


# --------------------------------------------------------------------------
# values that cross the signal boundary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """What ``start_probe`` found: the file's contents and its usability."""

    source: Path
    media: MediaInfo
    report: ValidationReport
    #: The selection the UI should start with (first compatible audio, PGS on).
    selection: TrackSelection

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.report.errors)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.report.warnings)


@dataclass(frozen=True, slots=True)
class JobFailure:
    """A job stopped because something went wrong.

    ``temp_dir`` is set only when the evidence was kept; ``internal`` marks a bug
    in avchd-forge rather than a problem with the input or the tools.
    """

    error: Exception
    temp_dir: Path | None = None
    output_dir: Path | None = None

    @property
    def message(self) -> str:
        return str(self.error)

    @property
    def internal(self) -> bool:
        return not isinstance(self.error, AvchdForgeError)

    @property
    def title(self) -> str:
        if self.internal:
            return "Internal error"
        for kind, title in _FAILURE_TITLES:
            if isinstance(self.error, kind):
                return title
        return "The job failed"

    @property
    def detail(self) -> str:
        """One diagnostic line for the status bar."""
        first = next(
            (row.strip() for row in self.message.splitlines() if row.strip()),
            self.message,
        )
        return first[:200]


@dataclass(frozen=True, slots=True)
class JobCancellation:
    """The user stopped a job; the temp directory is kept on purpose."""

    stage: str
    source: Path | None = None
    output_dir: Path | None = None
    temp_dir: Path | None = None
    #: Files the aborted run had already written into the output directory.
    leftovers: tuple[Path, ...] = ()

    @property
    def message(self) -> str:
        parts = [f"Cancelled{(' ' + self.stage) if self.stage else ''}."]
        if self.leftovers:
            parts.append("Left behind:")
            parts.extend(f"  {path}" for path in self.leftovers)
        elif self.output_dir is not None:
            parts.append(f"Nothing was written to {self.output_dir}")
        if self.temp_dir is not None:
            parts.append(f"Temporary files kept in {self.temp_dir}")
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class JobRequest:
    """Everything one mux job needs, as a single value the UI can build."""

    source: Path
    output_dir: Path
    #: ``None`` means "the smart defaults" (:func:`default_selection`).
    selection: TrackSelection | None = None
    options: PlanOptions = field(default_factory=PlanOptions)
    #: ``None`` means the system temp directory.
    temp_dir: Path | None = None
    ffprobe: str = "ffprobe"
    keep_temp_on_success: bool = False
    keep_temp_on_failure: bool = True


@dataclass(frozen=True, slots=True)
class SeasonRequest:
    """Everything one season job needs: several episodes, one AVCHD folder.

    ``selections`` runs parallel to ``sources``; a ``None`` entry means "the
    smart defaults" for that episode (:func:`default_selection`). The whole
    tuple may be ``None`` for "defaults everywhere" (a season of unknown
    length probed by another caller).
    """

    sources: tuple[Path, ...]
    output_dir: Path
    selections: tuple[TrackSelection | None, ...] | None = None
    options: PlanOptions = field(default_factory=PlanOptions)
    #: ``None`` means the system temp directory.
    temp_dir: Path | None = None
    ffprobe: str = "ffprobe"
    keep_temp_on_success: bool = False
    keep_temp_on_failure: bool = True


# --------------------------------------------------------------------------
# pure-ish helpers (they only touch ``runner``)
# --------------------------------------------------------------------------


def build_job_plan(request: JobRequest, runner: Runner, *, temp_dir: Path) -> JobPlan:
    """Probe, validate and run tsMuxeR's detection pass, then build the plan.

    The only reason this is not in :mod:`avchdforge.plan` is the validation
    ordering: the GUI shows the issues before a job starts, so validation runs
    first here and its report is reused by ``build_plan`` instead of being
    computed twice. An output folder that already holds an AVCHD tree is
    refused before any tool runs (V20). The plan's ``output_dir`` is the
    ``AVCHD`` folder inside the chosen one
    (:func:`~avchdforge.plan.avchd_destination`).
    """
    output_dir = Path(request.output_dir).expanduser()
    marker = existing_tree(output_dir)
    if marker is not None:
        raise ExistingTreeError(output_dir, marker)
    media = probe(request.source, runner, tool=request.ffprobe)
    selection = (
        request.selection if request.selection is not None else default_selection(media)
    )
    report = validate_all(
        media,
        selection,
    )
    if not report.ok:
        raise ValidationFailed(report)
    detection = detect_tracks(request.options.tsmuxer, request.source, runner)
    return build_plan(
        media,
        selection,
        detection,
        output_dir=request.output_dir,
        temp_dir=temp_dir,
        options=request.options,
        report=report,
    )


def describe_media(media: MediaInfo) -> list[str]:
    """Compact, log-pane friendly summary of a probed source."""
    lines = [
        f"file     {media.path.name}  ({media.format_label or media.format_name or '?'})",
        f"length   {media.duration_label}   size {media.size_label}",
    ]
    lines.extend(f"video    #{track.index} {track.summary()}" for track in media.videos)
    lines.extend(f"audio    #{track.index} {track.summary()}" for track in media.audio)
    lines.extend(f"subs     #{track.index} {track.summary()}" for track in media.subtitles)
    if not media.videos:
        lines.append("video    (none)")
    if not media.audio:
        lines.append("audio    (none)")
    if not media.subtitles:
        lines.append("subs     (none)")
    return lines


def describe_outcome(outcome: JobOutcome) -> list[str]:
    """Summary lines for the log pane after a successful run."""
    count = len(outcome.stream_files)
    lines = [f"done: {count} stream part{'s' if count != 1 else ''} written"]
    for path in outcome.stream_files:
        lines.append(f"  {path.name}  {_size_label(path)}")
    if outcome.plan.splits:
        lines.append(f"  split at {outcome.plan.mux.options.split_size_mb} MB for FAT32")
    lines.extend(f"warning: {warning}" for warning in outcome.warnings)
    if outcome.temp_dir_kept:
        lines.append(f"temp files kept in {outcome.temp_dir}")
    return lines


def describe_season_outcome(outcome: SeasonOutcome) -> list[str]:
    """Summary lines for the log pane after a successful season run."""
    titles = outcome.title_count
    count = len(outcome.stream_files)
    lines = [
        f"done: {titles} title{'s' if titles != 1 else ''} "
        f"({count} stream part{'s' if count != 1 else ''}) written"
    ]
    for index, (plan, playlist) in enumerate(
        zip(outcome.plans, outcome.playlists, strict=True), start=1
    ):
        lines.append(f"  title {index}  playlist {playlist:05d}  {plan.source.name}")
    for path in outcome.stream_files:
        lines.append(f"  {path.name}  {_size_label(path)}")
    if any(plan.splits for plan in outcome.plans):
        split = next(plan for plan in outcome.plans if plan.splits)
        lines.append(f"  split at {split.mux.options.split_size_mb} MB for FAT32")
    lines.extend(f"warning: {warning}" for warning in outcome.warnings)
    if outcome.temp_dir_kept and outcome.temp_dir is not None:
        lines.append(f"temp files kept in {outcome.temp_dir}")
    return lines


def _size_label(path: Path) -> str:
    try:
        size = float(path.stat().st_size)
    except OSError:  # pragma: no cover - the file was just verified
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GiB"  # pragma: no cover - unreachable


def _tree_files(root: Path) -> tuple[Path, ...]:
    """Every file under ``root``, for "what did the aborted run leave behind?"."""
    if not root.is_dir():
        return ()
    return tuple(sorted(path for path in root.rglob("*") if path.is_file()))


# --------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------


class JobRunner(QtCore.QObject):
    """Runs one probe or one mux job and reports through signals.

    The object is single-use at a time: ``start_*`` raises
    :class:`RuntimeError` while another call is still on the stack, which the UI
    cannot trigger because it disables its buttons, but tests and future callers
    should not have to guess. Every terminal signal — ``probed``, ``finished``,
    ``failed``, ``cancelled`` — goes out *after* the runner has been released,
    so a handler may start the next run: the window reads a list of files one
    probe at a time and does exactly that.
    """

    #: The job moved to another stage (``JobStage``).
    stage_changed = QtCore.Signal(object)
    #: Overall progress: a float in ``[0, 1]``, or ``None`` while unmeasurable.
    progress = QtCore.Signal(object)
    #: One line of tool output or an avchd-forge note.
    line = QtCore.Signal(str)
    #: A finished probe (``ProbeOutcome``).
    probed = QtCore.Signal(object)
    #: A finished, verified mux (``JobOutcome``).
    finished = QtCore.Signal(object)
    #: A job that stopped with an error (``JobFailure``).
    failed = QtCore.Signal(object)
    #: A job the user stopped (``JobCancellation``).
    cancelled = QtCore.Signal(object)

    def __init__(
        self,
        parent: QtCore.QObject | None = None,
        *,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner_factory: RunnerFactory = runner_factory or (
            lambda: QProcessRunner(parent=self)
        )
        self._runner: Runner | None = None
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def cancel(self) -> None:
        """Ask the running tool to stop; safe to call while nothing runs.

        Called from the GUI thread this is delivered *during* the nested event
        loop, which is the whole point of the design: the running child is killed
        and the job unwinds as a cancellation.
        """
        runner = self._runner
        if runner is not None:
            runner.cancel()

    # -- phases ----------------------------------------------------------

    def start_probe(self, source: Path | str, *, ffprobe: str = "ffprobe") -> None:
        """Probe ``source`` and emit :class:`ProbeOutcome` (or a failure)."""
        path = Path(source).expanduser()
        runner = self._begin()
        self.stage_changed.emit(JobStage(key="probe", label=f"reading {path.name}"))
        self.line.emit(f"[avchd-forge] probing {path}\n")
        outcome: ProbeOutcome | None = None
        failure: Exception | None = None
        try:
            media = probe(path, runner, tool=ffprobe)
            for row in describe_media(media):
                self.line.emit(f"[avchd-forge] {row}\n")
            selection = default_selection(media)
            outcome = ProbeOutcome(
                source=path,
                media=media,
                report=validate_all(media, selection),
                selection=selection,
            )
        except Exception as exc:
            failure = exc
        finally:
            self._end()
        if failure is not None:
            self._route_failure(
                failure,
                runner=runner,
                stage="probing",
                source=path,
                output_dir=None,
                temp_dir=None,
                keep_temp=False,
            )
            return
        if outcome is not None:
            self.probed.emit(outcome)

    def start_job(self, request: JobRequest) -> None:
        """Run a whole mux job: plan, prepare, mux, verify, cleanup."""
        source = Path(request.source).expanduser()
        output_dir = Path(request.output_dir).expanduser()
        runner = self._begin()
        try:
            temp_dir = self._make_temp(request.temp_dir)
        except OSError as exc:
            self._end()
            self._emit_failure(exc, temp_dir=None, output_dir=output_dir)
            return

        self.line.emit(f"[avchd-forge] source {source}\n")
        self.line.emit(f"[avchd-forge] output {output_dir}\n")
        self.line.emit(f"[avchd-forge] temp   {temp_dir}\n")

        outcome: JobOutcome | None = None
        failure: Exception | None = None
        try:
            outcome = self._mux(request, temp_dir, runner)
        except Exception as exc:
            failure = exc
        finally:
            self._end()
        if failure is not None:
            self._route_failure(
                failure,
                runner=runner,
                stage=PLAN_STAGE_LABEL,
                source=source,
                output_dir=output_dir,
                temp_dir=temp_dir,
                keep_temp=request.keep_temp_on_failure,
            )
            return
        if outcome is not None:
            for row in describe_outcome(outcome):
                self.line.emit(f"[avchd-forge] {row}\n")
            self.finished.emit(outcome)

    # -- internals -------------------------------------------------------

    def _mux(self, request: JobRequest, temp_dir: Path, runner: Runner) -> JobOutcome:
        self.stage_changed.emit(JobStage(key="plan", label=PLAN_STAGE_LABEL))
        plan = build_job_plan(request, runner, temp_dir=temp_dir)
        for row in describe_plan(plan):
            self.line.emit(f"[avchd-forge] {row}\n")
        return execute_plan(
            plan,
            runner,
            ffprobe=request.ffprobe,
            on_line=self.line.emit,
            on_progress=self.progress.emit,
            on_stage=self.stage_changed.emit,
            keep_temp_on_success=request.keep_temp_on_success,
            keep_temp_on_failure=request.keep_temp_on_failure,
            owns_temp=True,  # this job created the directory with mkdtemp
        )

    def start_season(self, request: SeasonRequest) -> None:
        """Run a whole season: plan every episode, mux, merge, verify, clean up."""
        sources = tuple(Path(source).expanduser() for source in request.sources)
        output_dir = Path(request.output_dir).expanduser()
        if not sources:
            raise ValueError("a season needs at least one episode")
        if request.selections is not None and len(request.selections) != len(sources):
            raise ValueError("one selection per episode, or none at all")
        runner = self._begin()
        try:
            temp_dir = self._make_temp(request.temp_dir)
        except OSError as exc:
            self._end()
            self._emit_failure(exc, temp_dir=None, output_dir=output_dir)
            return

        self.line.emit(f"[avchd-forge] season of {len(sources)} episodes\n")
        for source in sources:
            self.line.emit(f"[avchd-forge] source {source}\n")
        self.line.emit(f"[avchd-forge] output {output_dir}\n")
        self.line.emit(f"[avchd-forge] temp   {temp_dir}\n")

        outcome: SeasonOutcome | None = None
        failure: Exception | None = None
        try:
            outcome = self._season(request, sources, temp_dir, runner)
        except Exception as exc:
            failure = exc
        finally:
            self._end()
        if failure is not None:
            self._route_failure(
                failure,
                runner=runner,
                stage=SEASON_STAGE_LABEL,
                source=None,
                output_dir=output_dir,
                temp_dir=temp_dir,
                keep_temp=request.keep_temp_on_failure,
            )
            return
        if outcome is not None:
            for row in describe_season_outcome(outcome):
                self.line.emit(f"[avchd-forge] {row}\n")
            self.finished.emit(outcome)

    def _season(
        self,
        request: SeasonRequest,
        sources: tuple[Path, ...],
        temp_dir: Path,
        runner: Runner,
    ) -> SeasonOutcome:
        """Plan one episode at a time, then hand the batch to the backend."""
        # The whole run writes one tree; the per-episode plans (and their
        # probes) would all check the same empty folder, so check it here once,
        # before any tool runs (V20). The final merge refuses it again.
        output_dir = Path(request.output_dir).expanduser()
        marker = existing_tree(output_dir)
        if marker is not None:
            raise ExistingTreeError(output_dir, marker)

        plans: list[JobPlan] = []
        count = len(sources)
        for index, source in enumerate(sources, start=1):
            self.stage_changed.emit(
                JobStage(key="plan", label=f"episode {index}/{count}: {PLAN_STAGE_LABEL}")
            )
            # One scratch directory per episode: the mux writes <scratch>/AVCHD
            # and nothing else may share those names (the plan's meta file and
            # prep outputs live there too).
            episode_dir = temp_dir / f"episode-{index:03d}"
            selection = (
                request.selections[index - 1] if request.selections is not None else None
            )
            job = JobRequest(
                source=source,
                output_dir=episode_dir,
                selection=selection,
                options=request.options,
                temp_dir=episode_dir,
                ffprobe=request.ffprobe,
            )
            plan = build_job_plan(job, runner, temp_dir=episode_dir)
            for row in describe_plan(plan):
                self.line.emit(f"[avchd-forge] {row}\n")
            plans.append(plan)

        return execute_season(
            plans,
            runner,
            destination=avchd_destination(request.output_dir),
            ffprobe=request.ffprobe,
            on_line=self.line.emit,
            on_progress=self.progress.emit,
            on_stage=self.stage_changed.emit,
            # The season merge copies whole episodes on this thread; pumping the
            # event loop between its blocks is what keeps the window painting
            # and the Cancel button working there (the muxes pump themselves
            # through QProcessRunner's nested event loop).
            pump=QtCore.QCoreApplication.processEvents,
            keep_temp_on_success=request.keep_temp_on_success,
            keep_temp_on_failure=request.keep_temp_on_failure,
            owns_temp=True,  # this job created the directory with mkdtemp
        )

    def _route_failure(
        self,
        exc: Exception,
        *,
        runner: Runner,
        stage: str,
        source: Path | None,
        output_dir: Path | None,
        temp_dir: Path | None,
        keep_temp: bool,
    ) -> None:
        """Send an exception to ``cancelled`` or ``failed`` — never further up."""
        if isinstance(exc, JobCancelled):
            self._emit_cancelled(
                exc, source=source, output_dir=output_dir, temp_dir=temp_dir
            )
            return
        if runner.cancelled and isinstance(exc, ToolExecutionError):
            # The tool "failed" because we killed it: a cancellation, not an
            # error, and the temp dir stays whatever ``keep_temp`` says.
            self._emit_cancelled(
                JobCancelled(stage),
                source=source,
                output_dir=output_dir,
                temp_dir=temp_dir,
            )
            return
        kept = (
            keep_temp
            and temp_dir is not None
            and temp_dir.exists()
            # an empty temp dir holds no evidence, so a failure before the first
            # tool wrote anything does not leave a stray directory behind
            and any(temp_dir.iterdir())
        )
        if temp_dir is not None and not kept:
            shutil.rmtree(temp_dir, ignore_errors=True)
        self._emit_failure(exc, temp_dir=temp_dir if kept else None, output_dir=output_dir)

    def _begin(self) -> Runner:
        if self._busy:
            raise RuntimeError("a job is already running")
        self._busy = True
        runner = self._runner_factory()
        self._runner = runner
        return runner

    def _end(self) -> None:
        self._busy = False
        self._runner = None

    def _emit_failure(
        self, exc: Exception, *, temp_dir: Path | None, output_dir: Path | None
    ) -> None:
        failure = JobFailure(error=exc, temp_dir=temp_dir, output_dir=output_dir)
        self.line.emit(f"[avchd-forge] {failure.title}: {failure.detail}\n")
        if temp_dir is not None:
            self.line.emit(f"[avchd-forge] temp files kept in {temp_dir}\n")
        self.failed.emit(failure)

    def _emit_cancelled(
        self,
        exc: JobCancelled,
        *,
        source: Path | None,
        output_dir: Path | None,
        temp_dir: Path | None,
    ) -> None:
        # A tool that was killed raises a tool error from its own stack; the
        # runner's flag is the reliable way to tell "stopped on purpose" apart
        # from "crashed", so callers route both through here.
        # The run writes under the AVCHD folder, so scan that one: the chosen
        # folder may hold unrelated files, or other titles on the same drive.
        leftovers = (
            _tree_files(avchd_destination(output_dir)) if output_dir is not None else ()
        )
        cancellation = JobCancellation(
            stage=exc.stage,
            source=source,
            output_dir=output_dir,
            temp_dir=temp_dir,
            leftovers=leftovers,
        )
        self.line.emit(f"[avchd-forge] {cancellation.message}\n")
        self.cancelled.emit(cancellation)

    @staticmethod
    def _make_temp(root: Path | None) -> Path:
        base = Path(tempfile.gettempdir()) if root is None else Path(root).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(base)))


__all__ = [
    "PLAN_STAGE_LABEL",
    "SEASON_STAGE_LABEL",
    "TEMP_PREFIX",
    "JobCancellation",
    "JobFailure",
    "JobRequest",
    "JobRunner",
    "ProbeOutcome",
    "RunnerFactory",
    "SeasonRequest",
    "build_job_plan",
    "describe_media",
    "describe_outcome",
    "describe_season_outcome",
]
