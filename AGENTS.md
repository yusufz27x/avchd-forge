# AGENTS.md — working notes for AI agents and contributors

This file is the entry point for anyone (human or agent) picking up this repo cold.
Read it, then `docs/IMPLEMENTATION_PLAN.md` (what we are building and in what order)
and `docs/CONTEXT.md` (decisions taken, spec deviations, environment facts).

## What this is

`avchd-forge` — a PySide6 desktop GUI that remuxes BDRemux-style AVC `.mkv`/`.ts`
releases into a **PS3-compatible AVCHD `BDMV/` folder structure** on an external
(usually FAT32) USB drive. Remux only: video is never re-encoded.

## Golden rules

1. **No transcoding, ever.** Video is copied bit-for-bit. Audio is copied, or when
   the container must carry a compatible flavour (DTS-HD MA, TrueHD, E-AC-3/Atmos),
   only lossless *bitstream-filter* core extraction is allowed. Subtitles are copied
   or converted; never burned in.
2. **Never block the UI.** Every external process (`tsMuxeR`, `ffmpeg`, `ffprobe`)
   runs through `QProcess` and reports by signal. Synchronous `subprocess.run` is
   only allowed behind the plain-`Runner` used by the non-GUI core and tests.
3. **No Qt imports in the core logic.** `probe`, `validate`, `meta`, `plan`,
   `prep`, `drives` must stay importable without PySide6 so tests run headless and
   fast. Qt lives in `process.py` (QProcess runner), `jobs.py`, `settings.py`, `ui/`.
4. **Command building is pure.** Functions that assemble argv must be pure
   functions of their arguments and return `list[str]`. That is what the unit tests
   assert against; nothing computes argv inline at the call site.
5. **Fail loudly, keep evidence.** On failure, keep the temp dir and log the exact
   command, cwd, exit code and full output. On success, clean up.

## Layout

```
src/avchdforge/     application package (src layout)
  constants.py      codec/level/resolution tables, thresholds, default sizes
  errors.py         exception hierarchy
  validate_types.py Issue/ValidationReport (no imports beyond constants)
  probe.py          ffprobe JSON -> MediaInfo/VideoTrack/AudioTrack/SubtitleTrack
  audio.py          per-track verdict: copy, extract core, or refuse
  selection.py      which tracks the user picked + smart defaults
  validate.py       compatibility rules -> Issues (error/warning)
  meta.py           tsMuxeR meta-file generation (pure, golden-file tested)
  tracks.py         tsMuxeR detection run + ffprobe <-> track-id mapping
  plan.py           MediaInfo + user selection -> JobPlan (steps + meta text)
  capabilities.py   what the installed ffmpeg can do (encoders, bitstream filters)
  prep.py           ffmpeg argv for audio-core extraction / subtitle conversion
  tooling.py        tool discovery, version + capability probing
  process.py        Runner protocol, SubprocessRunner, QProcessRunner
  progress.py       progress-line parsers (tsMuxeR, ffmpeg -progress)
  backend.py        executes a JobPlan, verifies the AVCHD/BDAV output layout
  jobs.py           Qt orchestration: signals, cancel, temp-file policy
  drives.py         removable-drive + filesystem detection
  settings.py       QSettings wrapper
  ui/               main_window.py, track_view.py, log_pane.py, preferences.py, ...
tests/              pytest; unit tests mock the Runner, never spawn tools
docs/               IMPLEMENTATION_PLAN.md, CONTEXT.md
```

## Dev setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # PySide6 + pytest (+ pytest-qt)
.venv/bin/pytest -q
.venv/bin/avchd-forge                # or: .venv/bin/python -m avchdforge
```

External tools (`tsMuxeR`, `ffmpeg`, `ffprobe`) are **not** installed by pip. The app
checks for them at startup and refuses to start with a clear message if any is missing.
`~/Documents/avchd-forge/samples/` is git-ignored scratch space for generated test media.

## Testing conventions

* `pytest`, plain functions, no network, no real media files, no spawned processes.
  Two exceptions: `tests/test_qt_process.py` — `QProcessRunner` *is* Qt signal
  plumbing, so it drives trivial `sys.executable -c` children — and
  `tests/test_e2e.py`, the real-tool smoke test marked `@pytest.mark.e2e` and
  excluded from the default run (`-m 'not e2e'`). Keep both fast and keep
  everything else on `FakeRunner`.
* A `FakeRunner` (see `tests/conftest.py`) records argv and replays canned stdout —
  that is what "mock subprocess calls" means here.
* `meta.py` output is asserted against golden files in `tests/data/meta/`. When you
  intentionally change the meta format, update the golden file in the same commit.
* GUI tests use `pytest-qt` with `QT_QPA_PLATFORM=offscreen`; skip if PySide6 is absent.
* Mark slower integration checks (real tools, real media) `@pytest.mark.e2e` and keep
  them out of the default run.

## Commits

Conventional Commits, one coherent step each, imperative subject:

```
<type>(<scope>): <subject>

<body: what changed and why, decisions worth remembering>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `build`.
Scopes: `core`, `meta`, `probe`, `validate`, `audio`, `subs`, `process`, `ui`,
`settings`, `drives`, `packaging`, `docs`.
Record verification in the body (e.g. `pytest: 64 passed`, `e2e: PS3-shaped BDMV tree produced`).
