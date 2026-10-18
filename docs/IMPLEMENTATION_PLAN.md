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
| `VideoTrack` | `index`, `codec`, `profile`, `level: float \| None`, `width`, `height`, `fps`, `pix_fmt`, `bitrate`, `field_order`, `duration`, `sps: SpsGeometry \| None` (+ `coded_resolution`, `restore_window()`, `output_resolution`) |
| `AudioTrack` | `index`, `codec` (`ac3`/`eac3`/`dts`/`truehd`/`aac`/`pcm_*`), `codec_long` (e.g. *DTS-HD MA*), `channels`, `channel_layout`, `sample_rate`, `bitrate`, `language`, `title`, `is_default`, `is_forced`, `has_embedded_core: bool \| None` |
| `SubtitleTrack` | `index`, `codec` (`hdmv_pgs_subtitle`/`subrip`/`ass`/…), `language`, `title`, `is_default`, `is_forced` |
| `MediaInfo` | `path`, `format_name`, `duration`, `size_bytes`, `video`, `audio[]`, `subtitles[]`, `raw` (kept for the log pane) |

Parsing is split in two: `build_ffprobe_argv(tool, path)` and
`parse_ffprobe_json(dict) -> MediaInfo`. `probe(path, runner)` just glues them.
Golden fixtures live in `tests/data/ffprobe/`.

The argv asks for `-show_data` so the video stream's `extradata` hexdump reaches
`sps.parse_sps` (`avchdforge.sps`, pure): coded size + conformance window, then
`window_restore()` decides whether a non-1080 display can be repaired losslessly
(§2.12).

### Validation (`validate.py`)

`validate_container(media)` and `validate_selection(media, selection)` return a
`ValidationReport(issues: tuple[Issue, ...])` with `severity in {error, warning}`.
`Issue` carries `code`, `message`, `track_index`.

| # | Rule | Severity |
| --- | --- | --- |
| V1 | input extension ∈ {`.mkv`, `.ts`, `.m2ts`, `.mpg`} | error |
| V2 | exactly one video stream | error |
| V3 | video codec is `h264`/AVC (HEVC, MPEG-2, VC-1 … rejected) | error |
| V4 | resolution is exactly 1920×1080 or 1280×720; a window crop whose coded rows can be restored is a warning (the plan repairs it, D25), a hard crop or an unreadable SPS is an error | warning / error |
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

### Video window repair (`prep.py`, `sps.py`)

Only for a display size that is not 1920×1080/1280×720 *and* whose coded frame
still contains the missing rows (`VideoTrack.restore_window()`, §2.12):

```
ffmpeg -i src -map 0:<video-index> -c:v copy \
  -bsf:v h264_metadata=crop_left=0:crop_right=0:crop_top=0:crop_bottom=<slack>,dump_extra \
  -f h264 out.264
```

The step is planned only when the ffmpeg build advertises **both** filters
(`prep.WINDOW_BSFS`); `dump_extra` is what puts the rewritten SPS/PPS into the
elementary stream, because `h264_metadata` alone emits a headerless one. The
produced file must probe as `h264` at the restored size (`check_prep_output`),
and tsMuxeR muxes it as `track=0` like an extracted audio core (D9). If the
restore is needed but the filters are missing, `build_plan` raises `PlanError`
instead of quietly muxing a broken stream.

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
3. `prep.*` → the video window repair, then audio cores and subtitle
   conversions, sequentially, temp dir
   `mkdtemp(prefix="avchd-forge-", dir=settings.temp_dir)`.
4. `mux` → the destination is `<output_dir>/AVCHD` unless the chosen folder is
   already named `AVCHD` (`plan.avchd_destination`, D23); create it, write
   `<tmp>/job.meta`, run `tsMuxeR <meta> <destination>`.
5. `verify` → rename the tree to the AVCHD-on-FAT32 names the PS3 mounts from USB
   (`INDEX.BDM`, `MOVIEOBJ.BDM`, `*.MPL`, `*.CPI`, `*.MTS`; D22/§2.11 of
   `docs/CONTEXT.md`), then check all of them, at least one `BDMV/PLAYLIST/*.MPL`
   and `BDMV/STREAM/*.MTS`; every part ≤ split limit; warn on stray files.
6. `cleanup` → temp dir removed on success (kept on failure, path surfaced in UI and
   in the log with a "Open temp dir" button).

Cancellation kills the running child (not the whole group), marks the result
`cancelled`, keeps the temp dir, and reports which file was left behind.

A season run (`JobRunner.start_season`, built from a `SeasonRequest`) plans every
episode first — each into its own scratch folder `<temp>/episode-NNN`, one
`JobPlan` whose `output_dir` is exactly `<scratch>/AVCHD` — then hands the batch
to `backend.execute_season`, which works one episode at a time: mux and verify
that tree on its own, add it to the season folder (`season.SeasonBuilder`, which
renumbers its clips and playlists; moving rather than copying unless the temp dir
is kept) and drop its scratch, then the next episode. One rename to the FAT32
names, one verification of the merged tree and one cleanup of the staging root
close the run (kept on failure/cancel, holding the episodes not yet merged;
`docs/CONTEXT.md` D26/D27/D29). The run bar weighs per episode `prep + mux` plus
its slice of one merge unit, and the episode transfer pumps the GUI's event loop
between its 4 MiB blocks so the window stays live and cancellable.

Terminal signals (`probed`, `finished`, `failed`, `cancelled`) are emitted *after*
the runner is released, so a handler may start the next run — the window's probe
queue reads a list of files that way.

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

* **Input**: an episode list (`QListWidget`, fixed height) holding one file per
  title, in play order, with *Add files…* (multi-select picker), *Remove*,
  *Move up* / *Move down* and drag-and-drop of one or more `.mkv/.ts` files.
  Files are probed one at a time; each row shows the duration and how many
  tracks the episode keeps, a probe that cannot answer marks its row, and the
  highlighted row is what the track view edits. One listed file is the
  single-title flow; two or more are a season — one `AVCHD/` folder, one title
  per episode (D26/D27).
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
20. `feat(core): create the AVCHD folder a PS3 mounts from USB`

## 3. Verification strategy

* **Unit** — `pytest` over pure core: probe parsing, validation matrix, meta golden
  files, argv builders (asserting `-c:a copy` / `-c copy` and *no* encoder flags,
  i.e. proving "no transcode"), progress parsers, drive assessment with canned
  `lsblk` JSON. All external effects go through `FakeRunner`.
* **Integration (marked `e2e`, skipped by default)** — generate a 12-second
  1080p H.264 AC-3 + SRT sample with ffmpeg, run the real pipeline with the real
  tsMuxeR binary, then assert the `AVCHD` folder (D23) and the renamed
  (`INDEX.BDM`/`*.MTS`, D22) BDMV tree, ffprobe the produced stream part (codec
  must still be `h264`, audio `ac3`) and check splitting with a small
  `--split-size`.
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

## 6. Season support (approved direction, 2026-10-18)

Goal: N episodes in one AVCHD folder, one title per episode, picked with the
player's own "Go To → Title X" (D26/§2.13 of `docs/CONTEXT.md`). The golden
rules do not change: no transcoding, no Qt in the core, pure argv/byte builders,
and nothing counts as done before a PS3 has played it.

Ordered work:

1. **Foundation + PS3 spike** *(done; console-verified 2026-10-18)* — `bdmv.py`
   (title table and movie objects; byte-identical to tsMuxeR for one title),
   `season.py` (`merge_avchd_trees`), golden fixtures in `tests/data/bdmv/`,
   e2e checks through libbluray's HDMV VM, and
   `scripts/build_season_spike.py` to produce the console tree from real episode
   files. The console listed the tree, played episode 1 on insert, reached
   episode 2 via "Go To → Title 2" and restarted title 1 from the Top Menu
   button (D26/§2.13); the same run also confirmed the D22 rename.
2. **GUI integration** *(done, 2026-10-18)* — the source row is an episode list
   (*Add files…*, a drop of several files, *Remove*, *Move up* / *Move down*);
   probes run one file at a time, every episode keeps its own `TrackSelection`,
   and Start builds a `SeasonRequest` for two or more files (`JobRequest` and
   `start_job` for one, so the single-title flow is untouched).
   `backend.execute_season` muxes each episode into its own scratch tree
   (`<temp>/episode-NNN/AVCHD`), verifies it there, merges with `move=True`
   unless the temp dir is kept, renames once and verifies the merged tree; the
   run bar weighs per episode `prep + mux` plus one merge unit (the merge's own
   staging behaviour was reworked in step 3). Per-episode chapters need nothing
   new — each episode's plan carries its own meta text and options. "Add
   episodes to an existing season folder" (the V20 revisit) stays deferred: a
   v1 season refuses an existing tree and never touches one.
3. **One episode at a time, and a temp-space check** *(done, 2026-10-18)* — the
   first GUI season run (13 × ~10.6 GiB, temp `/tmp` a 7.6 GiB tmpfs) froze the
   window for the whole merge and then died with ffmpeg's `228` (ENOSPC).
   `season.SeasonBuilder` builds the season as each episode's tree is verified,
   `execute_season` drops that episode's scratch immediately and calls
   `should_cancel`/`pump` between 4 MiB transfer blocks (`jobs.py` passes
   `QCoreApplication.processEvents`), so temp holds about one episode and Cancel
   works during the copy; the bar's single merge unit is sliced across the
   episodes, keeping its total unchanged. Before each prep/mux,
   `check_temp_space` compares the plan's own estimate with the free space of
   `plan.temp_dir` and fails with the folder, the numbers and the Preferences
   pointer (naming a tmpfs when that is what `/tmp` is); `looks_like_no_space`
   turns a tool's ENOSPC into the same error. Failure semantics: a season that
   fails or is cancelled part-way leaves a partial season in the output folder
   (delete it before a retry) and keeps the scratch of the episodes not merged;
   `keep_temp_on_success=True` still stages the whole season, so it still needs
   the room for all of it (D29 of `docs/CONTEXT.md`).
4. **Authored top menu** (only if the console's title search proves unusable) —
   needs an HDMV/IG compiler, which no open-source tool provides and tsMuxeR
   cannot ingest; the title-table layout from step 1 is unchanged by it, so the
   foundation keeps its value either way.
5. **Fallback** — one joined title with episode chapter marks (tsMuxeR's join
   syntax is confirmed and `custom_chapters` is wired), losing per-episode
   titles.

