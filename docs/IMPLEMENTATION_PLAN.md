# Implementation Plan — avchd-forge

Goal: a runnable, testable PySide6 GUI that turns a single AVC BDRemux (`.mkv`/`.ts`)
into a PS3-playable AVCHD `BDMV/` tree on an external drive, remuxing only.

Fixed decisions (from the brief) are treated as contract. Two of them collide with
reality and were re-scoped with the user's explicit approval — see
[Deviations](#deviations-approved-by-user).

## 1. Architecture

```
        ┌──────────────────────────── ui/ (Qt) ─────────────────────────────┐
        │ main_window · track_view · log_pane · preferences · drive_combo   │
        └───────┬───────────────────────────────────────────────┬───────────┘
                │ TrackSelection / Options                      │ log lines
        ┌───────▼──────────── core (no Qt) ────────────┐ ┌───────▼───────────┐
        │ probe → validate → plan → prep → backend     │ │ jobs.JobRunner    │
        │ (pure argv builders + parsers)               │ │ (Qt state machine)│
        └───────┬──────────────────────────────────────┘ └───────┬───────────┘
                │ Runner protocol (run(argv) -> CompletedRun)   │ QProcess signals
        ┌───────▼───────────────────────────────────────────────────────▼─────┐
        │ SubprocessRunner (tests, CLI)   |   QProcessRunner (GUI, streaming) │
        └───────────────────────────────────┬─────────────────────────────────┘
                                            │ tsMuxeR | ffmpeg | ffprobe
```

Everything above `Runner` is pure and unit-testable; `jobs.JobRunner` is the only
place that knows both Qt and the core pipeline.

### Data model (`probe.py`)

| Type | Fields (essential) |
| --- | --- |
| `VideoTrack` | `index`, `codec`, `profile`, `level: float \| None`, `width`, `height`, `fps`, `pix_fmt`, `bitrate`, `field_order`, `duration` |
| `AudioTrack` | `index`, `codec` (`ac3`/`eac3`/`dts`/`truehd`/`aac`/`pcm_*`), `codec_long` (e.g. *DTS-HD MA*), `channels`, `channel_layout`, `sample_rate`, `bitrate`, `language`, `title`, `is_default`, `is_forced`, `has_embedded_core: bool \| None` |
| `SubtitleTrack` | `index`, `codec` (`hdmv_pgs_subtitle`/`subrip`/`ass`/…), `language`, `title`, `is_default`, `is_forced` |
| `MediaInfo` | `path`, `format_name`, `duration`, `size_bytes`, `video`, `audio[]`, `subtitles[]`, `raw` (kept for the log pane) |

Parsing is split in two: `build_ffprobe_argv(tool, path)` and
`parse_ffprobe_json(dict) -> MediaInfo`. `probe(path, runner)` just glues them.
Golden fixtures live in `tests/data/ffprobe/`.

### Validation (`validate.py`)

`validate_container(media)` and `validate_selection(media, selection)` return a
`ValidationReport(issues: tuple[Issue, ...])` with `severity in {error, warning}`.
`Issue` carries `code`, `message`, `track_index`.

| # | Rule | Severity |
| --- | --- | --- |
| V1 | input extension ∈ {`.mkv`, `.ts`, `.m2ts`, `.mpg`} | error |
| V2 | exactly one video stream | error |
| V3 | video codec is `h264`/AVC (HEVC, MPEG-2, VC-1 … rejected) | error |
| V4 | resolution is exactly 1920×1080 or 1280×720 | error (warning with the Preferences opt-in) |
| V5 | H.264 level ≤ 4.1 (level 4.2/5.x allowed but flagged) | warning |
| V6 | 1080 line frame rate ≤ 30 (29.97) and 720 line frame rate ≤ 60 (59.94) | warning |
| V7 | `field_order` interleaved/`tt`/`bb` (interlaced input) | warning |
| V8 | video bitrate > 24 Mbps | warning |
| V9 | at least one audio track selected | error |
| V10 | selected audio codec is AVCHD-legal (`ac3`, `dts`, `lpcm`/`pcm_*`) | warning (AAC) |
| V11 | LPCM sample rate ≠ 48 kHz, or > 6 channels | warning |
| V12 | TrueHD selected but no AC-3 core available | error |
| V13 | PGS subtitle dimensions > 1920×1080 | warning |
| V14 | no subtitle selected at all | warning |
| V15 | target filesystem is not FAT32 while splitting is on | warning |
| V16 | estimated output size > free space on target | warning |
| V17 | duration missing / zero | warning |
| V18 | subtitle streams that cannot be converted to PGS are not offered | warning |
| V19 | a selected subtitle stream is missing or cannot be converted to PGS | error |
| V20 | output folder already contains an AVCHD tree (`BDMV/` or `AVCHD/BDMV/`) | error |

Errors block the Start button; warnings are listed and shown inline on the track row.

### Meta-file generation (`meta.py`)

Pure function `render_meta(job: MuxJob) -> str`, golden-file tested. Shape:

```
MUXOPT --avchd --no-pcr-on-video-pid --new-audio-pes --vbr --vbv-len=500 --split-size=4096M
V_MPEG4/ISO/AVC, "/in/source.mkv", insertSEI, contSPS, track=0, lang=eng
A_AC3, "/in/source.mkv", track=2, lang=eng, timeshift=0ms
S_HDMV/PGS, "/tmp/avchd-forge-xyz/subs/sub-0.sup", track=0, lang=eng
```

* Paths are emitted with native separators and wrapped in double quotes only when
  they contain spaces, tabs or quotes; backslashes are never doubled so that
  Windows paths survive untouched. Line breaks in a path are refused.
* `--split-size=<N>M` present only when FAT32 splitting is enabled (default 4096).
* **Track ids are resolved before the meta is written** (D7/D8): `meta.py` takes a
  finished `track_id` per track and never computes one. Container tracks get the
  `Track ID:` reported by a mandatory `tsMuxeR <source>` detection run
  (`tracks.py`), matched to the ffprobe streams by media type + ordinal, with codec
  classes verified; extracted cores, `.srt` and `.sup` files are raw elementary
  streams referenced as `track=0` (tsMuxeR ignores the parameter there).
* Text subtitles selected for conversion are emitted as `S_TEXT/UTF8` with the
  font options tsMuxeR needs to render PGS itself (`font-name`, `font-size`,
  `bottom-offset`, `font-color`), or as an ffmpeg-produced `.sup` when the local
  ffmpeg advertises a PGS encoder (see Deviations).
* Track order follows the user's selection order; languages come from the track
  metadata, with the language-priority preference used only for *defaults*.
* Determinism is a test invariant: same input → byte-identical meta.

`JobPlan` bundles: `source`, `output_dir`, `temp_dir`, `media`, `selection`,
`steps` (ordered `PrepStep`s + `MuxStep`), `meta` text, `warnings`.

### Audio matrix (`prep.py`)

| Source codec | Action | Command fragment |
| --- | --- | --- |
| `ac3`, `dts` (core/HRA without XLL), `aac`, `pcm_*` | passthrough | tsMuxeR reads the source file directly |
| `dts` + DTS-HD extension (`profile` XLL/HRA) | extract core to temp `.dts` | `ffmpeg -i src -map 0:<i> -c:a copy -bsf:a dca_core -f dts out.dts` |
| `eac3` (Dolby Digital Plus / Atmos, has core) | extract AC-3 core to temp `.ac3` | `ffmpeg -i src -map 0:<i> -c:a copy -bsf:a eac3_core -f ac3 out.ac3` |
| `truehd` with separate `ac3` core stream reported by ffprobe | demux that stream to temp `.ac3` | `ffmpeg -i src -map 0:<ac3-index> -c:a copy -f ac3 out.ac3` |
| `truehd` without detectable core | refused in the UI (V12) | — |

Every extraction is verified afterwards with `ffprobe` — the produced file must
report the expected codec (`dts`/`ac3`), otherwise the job fails loudly instead of
handing tsMuxeR something it will silently mangle. `-map`, `-c:a copy` and the
bitstream filter are asserted by unit tests.

### Subtitle pipeline (`prep.py`)

| Source | Action |
| --- | --- |
| `hdmv_pgs_subtitle` | passthrough (`S_HDMV/PGS`) |
| `subrip`/`ass`/`ssa`/`mov_text` | `ffmpeg -i src -map 0:<i> -f srt out.srt`, then tsMuxeR renders PGS at mux time (`S_TEXT/UTF8` + font opts) |
| anything else (`dvd_subtitle`, `dvb_subtitle`, teletext …) | rejected in the UI with an explanatory tooltip |

If a future/local ffmpeg advertises a PGS encoder, the plan prefers
`ffmpeg -c:s pgssub` and emits `S_HDMV/PGS` on the produced `.sup`; the decision is
driven purely by `tooling.detect_capabilities()` so both branches are testable.

### Process handling (`process.py`, `progress.py`)

```python
class Runner(Protocol):
    def run(self, argv, *, cwd=None, on_line=None, on_progress=None, phase="") -> CompletedRun: ...
```

* `SubprocessRunner` — `subprocess.Popen`, line-buffered, used by tests/CLI.
* `QProcessRunner` — `QProcess`, `readyReadStandardOutput/Error` → `on_line`,
  `finished`/`errorOccurred` → result, `kill()` for cancel. Merged stderr, because
  tsMuxeR and ffmpeg both chat on stderr.
* `progress.py`: `parse_tsmuxer_line()`, `parse_ffmpeg_progress(key, value, total)`,
  `ProgressTracker` which turns either into `0.0–1.0` (or `None` when unknown).
  Parsers are written against **captured real output** (see `docs/CONTEXT.md`).

### Orchestration (`backend.py`, `jobs.py`)

`JobRunner` (QObject) steps, each emitting `stageChanged`, `progress`, `line`, and
ultimately `finished(JobResult)`:

1. `probe` → build `MediaInfo` (log the raw JSON).
2. `revalidate` → abort on error.
3. `prep.*` → audio cores and subtitle conversions, sequentially, temp dir
   `mkdtemp(prefix="avchd-forge-", dir=settings.temp_dir)`.
4. `mux` → write `<tmp>/job.meta`, run `tsMuxeR <meta> <output_dir>`.
5. `verify` → rename the tree to the AVCHD-on-FAT32 names the PS3 mounts from USB
   (`INDEX.BDM`, `MOVIEOBJ.BDM`, `*.MPL`, `*.CPI`, `*.MTS`; D22/§2.11 of
   `docs/CONTEXT.md`), then check all of them, at least one `BDMV/PLAYLIST/*.MPL`
   and `BDMV/STREAM/*.MTS`; every part ≤ split limit; warn on stray files.
6. `cleanup` → temp dir removed on success (kept on failure, path surfaced in UI and
   in the log with a "Open temp dir" button).

Cancellation kills the running child (not the whole group), marks the result
`cancelled`, keeps the temp dir, and reports which file was left behind.

### Drive awareness (`drives.py`)

`list_drives()` prefers `lsblk -J -o …` (device, mountpoint, fstype, label,
removable flag, size, model), falls back to `/proc/mounts` when `lsblk` is absent.
`assess_target(path) -> TargetReport` returns the containing mount, `fstype`,
`removable`, free bytes and warnings (V15, V16). FAT32 detection accepts
`vfat`/`msdos`; `exfat`/`ntfs` are reported as "not FAT32 — splitting is optional".
`existing_tree()` detects a tree already present (`BDMV/` or `AVCHD/BDMV/`) and
becomes a blocking V20 error (`TargetReport.errors`) — tsMuxeR reuses fixed
names, so a second run into the same folder would overwrite the first title.

### Settings (`settings.py`)

`QSettings("avchd-forge", "avchd-forge")` via a typed wrapper:

`ui/geometry`, `ui/window_state`, `ui/log_visible`, `paths/last_input_dir`,
`paths/last_output_dir`, `paths/temp_dir`, `tools/tsmuxer`, `tools/ffmpeg`,
`tools/ffprobe`, `mux/split_enabled`, `mux/split_size_mb`, `mux/font_name`,
`mux/font_size`, `langs/audio_priority`, `langs/subtitle_priority`,
`temp/keep_on_success`, `temp/keep_on_failure`.

### UI (`ui/`)

* **Input**: file picker button + `QLineEdit` (read-only) + drag-and-drop
  (`dragEnterEvent`/`dropEvent` accepting exactly one file, validation on drop).
* **Output**: directory picker with a drive combo listing detected removable
  drives plus a filesystem badge (`FAT32` / `not FAT32`) and inline warning.
* **Tracks**: three panes — video info (read-only, level/bitrate flags rendered as
  coloured text), audio table with checkboxes, subtitle table with checkboxes.
  Incompatible rows are disabled with tooltip text; text-subtitle rows show
  "→ PGS (converted)". Defaults: first *compatible* audio on, all PGS on, text subs
  off, forced-subs column surfaced.
* **Log**: dockable/collapsible pane (`QToolButton` toggle, state persisted) with
  monospace output, `Command:` lines highlighted, auto-scroll with a pause toggle,
  "Save log…" and "Copy" actions.
* **Run bar**: stage label, `QProgressBar` (busy state while probing/verifying),
  Start/Cancel, elapsed time, and after a run a summary with "Open output folder".
* **Menus**: File (Open, Open output, Quit), Tools (Preferences, Re-check external
  tools, Show tool versions), Help (About, log/temp dirs).
* **Startup gate**: missing/old `tsMuxeR`/`ffmpeg`/`ffprobe` produce a modal with
  the exact paths searched plus a "Set path…" shortcut into Preferences. `--help`
  style CLI: `avchd-forge [file] [--output DIR]`.

## 2. Order of work (each line = one conventional commit)

1. `chore: scaffold repository (license, gitignore, agent docs, plan)` ← this step.
2. `feat(core): add constants, error types and ffprobe parsing with fixtures`
3. `feat(core): validate media and track selections against AVCHD rules`
4. `feat(meta): generate tsMuxeR meta files (+ golden files)`
5. `feat(core): build a JobPlan from media, selection and options`
6. `feat(audio): ffmpeg argv for DTS-HD/E-AC-3/TrueHD core extraction`
7. `feat(subs): subtitle conversion argv with PGS-capability fallback`
8. `feat(process): Runner protocol, subprocess runner and progress parsers`
9. `feat(tools): tool discovery, version and capability probing`
10. `feat(tracks): mandatory tsMuxeR detection run and ffprobe↔track-id mapping`
11. `feat(backend): execute plans and verify the AVCHD output tree`
12. `feat(jobs): Qt job orchestration with cancel and temp-file policy`
13. `feat(settings): QSettings-backed preferences`
14. `feat(drives): removable drive and FAT32 detection`
15. `feat(ui): main window, track panes, collapsible log, drag-and-drop`
16. `feat(ui): preferences dialog and startup tool gate`
17. `docs: README, packaging metadata, requirements`
18. `test(e2e): real tsMuxeR + ffmpeg smoke run on generated media`
19. `fix(backend): ship the AVCHD-on-FAT32 (8.3) names the PS3 mounts from USB`

## 3. Verification strategy

* **Unit** — `pytest` over pure core: probe parsing, validation matrix, meta golden
  files, argv builders (asserting `-c:a copy` / `-c copy` and *no* encoder flags,
  i.e. proving "no transcode"), progress parsers, drive assessment with canned
  `lsblk` JSON. All external effects go through `FakeRunner`.
* **Integration (marked `e2e`, skipped by default)** — generate a 12-second
  1080p H.264 AC-3 + SRT sample with ffmpeg, run the real pipeline with the real
  tsMuxeR binary, then assert the renamed (`INDEX.BDM`/`*.MTS`, D22) BDMV tree,
  ffprobe the produced stream part (codec must still be `h264`, audio `ac3`) and
  check splitting with a small `--split-size`.
* **Manual smoke** — launch the GUI offscreen (`QT_QPA_PLATFORM=offscreen`) in CI
  style, and interactively on a desktop when available.

## 4. Deviations (approved by user)

1. **SRT/ASS → PGS via `-c:s pgssub` is impossible with stock FFmpeg.** FFmpeg ships
   a PGS *decoder* (`pgssub`) and a raw `sup` muxer, but no PGS *encoder* (checked
   on 7.1.5 and in upstream docs). Approved fallback: ffmpeg does text→text
   (`-f srt`) and **tsMuxeR renders the PGS** as part of the AVCHD mux — same
   no-burn-in guarantee, real PGS output, and it is what tsMuxeR's own AVCHD mode
   does. The `-c:s pgssub` path stays wired behind
   `tooling.detect_capabilities()` for builds that do have an encoder.
2. **`truehd_core` does not produce an AC-3 core.** Upstream documentation for
   `truehd_core` reads *"Extract the core from a TrueHD stream, dropping ATMOS
   data"* — it strips Atmos, not the AC-3 core; FFmpeg has no "AC-3 core extractor"
   for TrueHD. Approved behaviour: if ffprobe exposes the TrueHD track's embedded
   AC-3 core as its own `ac3` stream, demux that stream (still copy, no re-encode);
   otherwise the track is refused in the UI with an explicit reason instead of
   silently re-encoding. `dca_core` (DTS-HD → DTS) and `eac3_core` (DD+ → AC-3)
   behave exactly as the brief assumes and are used as specified.

## 5. Risks / open items

| Risk | Mitigation |
| --- | --- |
| tsMuxeR `track=N` numbering differs from ffprobe's stream index (MKV: N=index+1; MPEG-TS: N=PID; raw files: ignored) | resolved by D7/D8: mandatory `tracks.py` detection run, ordinal+codec-class matching, hard error on ambiguity; extracted cores always use raw-stream `track=0`; validated in the e2e step for both MKV and TS |
| tsMuxeR fails silently or crashes on bad input (phantom track, exit 0 with 0 frames, exit 139 on TS index) | post-mux composition verification (D4) plus preflight detection; both covered by unit tests using captured output |
| tsMuxeR version output differs between 2.6.x/2.7.x | parsers tolerate both (`% complete` and `N/M packets`), verified against captured output |
| AVCHD strictness vs. PS3 leniency (AAC, level 4.2, 24 Mbps) | implemented as warnings, never silent failures; documented in README |
| `lsblk` unavailable (non-Linux) | fallback path + manual warning text; documented as Linux-first |
| Very long paths / non-ASCII on FAT32 | meta quoting, temp dir under `$TMPDIR`, e2e case with a space in the path |
