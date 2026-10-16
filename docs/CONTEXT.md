# Context log — decisions, evidence, environment

Kept so that a later contributor (or agent) does not have to re-derive facts by
experiment. Anything marked **(verified)** was reproduced on this machine with the
versions listed below; the raw commands are quoted so they can be re-run.

## 1. Environment (as set up for this repo)

| Thing | Version / location | Notes |
| --- | --- | --- |
| Python | 3.13.5 (`/usr/bin/python3`) | container image has **no** `pip` and no `ensurepip` |
| pip | 26.2.1 in `.venv` | bootstrapped with `python3 -m venv --without-pip .venv` + `get-pip.py` |
| PySide6 | 6.12.0 | installed in `.venv` |
| pytest / pytest-qt | 9.1.1 / 4.5.0 | installed in `.venv` |
| ffmpeg / ffprobe | 7.1.5 (Debian 13 build, `/usr/bin`) | `libx264`, `ac3`, `dca`, `eac3`, `aac`, `pcm_s16le`, `srt`, `ass` encoders available |
| tsMuxeR | 2.7.0 (`justdan96/tsMuxer` linux zip) | not packaged; dev copy used for verification, path set per-user in Preferences |

Test media is generated on demand into the git-ignored `samples/` (1080p H.264
High@4.1 + two AC-3 tracks + a small SRT); `scratch/` holds throwaway mux output.

## 2. External-tool facts established by experiment

### 2.1 tsMuxeR metadata format

* Meta files must end in `.meta` and be UTF-8; invocation is
  `tsMuxeR <meta> <out-file-or-dir>` **(verified)**.
* `MUXOPT --avchd` exists and produces an AVCHD disc tree. Tree observed
  **(verified)**:
  `BDMV/index.bdmv`, `BDMV/MovieObject.bdmv`, `BDMV/PLAYLIST/00000.mpls`,
  `BDMV/CLIPINF/00000.clpi`, `BDMV/STREAM/00000.m2ts`, `BDMV/AUXDATA`,
  `BDMV/BACKUP/`, plus `CERTIFICATE/BACKUP/`. Note the exact capitalisation
  `MovieObject.bdmv` — not `MOVIEOBJ.BDM`.
* `--split-size` accepts `KB/KiB/MB/MiB/GB/GiB`; `--split-size=1MB` on a 2.5 MB
  source produced `00000.m2ts` + `00001.m2ts` **(verified)**. `4096MB` is used for
  FAT32 (see §2.6 for the exact unit and split-point semantics).
* **`track=` semantics depend on the input.** tsMuxeR's detection mode
  (`tsMuxeR <file>`) prints a `Track ID:` for every stream it sees, and the meta
  `track=` value must equal that number:

  | Input | Detection `Track ID:` | Correct `track=` | Wrong value does |
  | --- | --- | --- | --- |
  | MKV | 1-based, in file order | `ffprobe_index + 1` | silently muxes a phantom 0-channel track |
  | MPEG-TS / M2TS | the **PID** (e.g. 256, 257) | the PID | **segfaults** (exit 139) |
  | raw elementary stream (`.ac3`, `.dts`, `.sup`, `.srt`) | no `Track ID:` line is printed | ignored | nothing — a bogus `track=7` still muxed correctly |

  All four rows were reproduced on this machine. Consequences: we never compute
  `track=` from the ffprobe index (verified only for MKV), we resolve it from a
  mandatory detection run (`docs/CONTEXT.md` §3 D7/D8), and raw temp files are
  always emitted with `track=0` for explicitness.
* With (MKV) video=ffprobe 0 and audio=ffprobe {1,2}, `track=2` selected the
  6-channel English AC-3 and `track=3` the 2-channel Japanese one **(verified)**.
* Audio track options include `lang`, `timeshift=±Nms`, `down-to-dts` (strip DTS-HD
  from a DTS track) and `down-to-ac3` (strip HD part of TrueHD) — tsMuxeR can do
  core extraction natively; the brief mandates ffmpeg bitstream filters instead, so
  we use ffmpeg and keep these as documented fallbacks.
* Subtitle (`PG`/`SRT`) options: `video-width`, `video-height`, `fps`, `default`,
  and for SRT also `font-name`, `font-size`, `font-color`, `font-italic`,
  `font-bold`, `font-underline`, `font-strikeout`, `bottom-offset`, `font-border`,
  `fadein-time`, `fadeout-time`, `line-spacing` **(verified in `--help`)**.
* Detection mode `tsMuxeR <file>` prints, per stream:
  `Track ID:`, `Stream type:`, `Stream ID:` (`V_MPEG4/ISO/AVC` …), `Stream info:`
  (profile/level/resolution/fps/bitrate/channels), `Stream lang:`, then a
  `Duration:` line. Captured sample lives in `tests/data/tsmuxer/detect_mkv.txt`.
* Success is signalled by the line `Mux successful complete`, progress by
  `NN.N% complete` lines (e.g. `22.2% complete`, `100.0% complete`) **(verified)**.
  A missing meta file exits `255`; a missing *input* file exits non-zero too.

### 2.2 tsMuxeR failure modes that force output verification

tsMuxeR 2.7.0 **exits 0 and claims success** in cases that silently produce wrong
output **(verified)**:

| Meta error | Observed result |
| --- | --- |
| `A_AC3 … track=9` (index does not exist) | no audio decoded; output `.m2ts` contains a **phantom `ac3` stream with 0 channels** |
| `V_MPEG4/ISO/AVC … track=2` pointing at an AC-3 stream | `Processed 0 video frames`, still exits 0 and writes a tree |
| `track=1`/`track=2` on an MPEG-TS input (i.e. sequential instead of PID) | **segmentation fault, exit 139**, no output written |

Consequence (design requirement, not optional): after muxing we **ffprobe the
produced `.m2ts` files and compare the stream composition against the plan**
(video codec/resolution, number and codecs of audio tracks, subtitle codecs), and
reject zero-channel audio streams. `Mux successful complete` alone is not proof.

### 2.3 FFmpeg capabilities relevant to the brief

* No PGS encoder in stock FFmpeg (7.1.5): `-encoders` lists `dvdsub`, `dvbsub`,
  `ass`, `srt` … but no `pgssub`; `ffmpeg -decoders` *does* list `pgssub`, and a raw
  `sup` **muxer** exists. Therefore `-c:s pgssub` cannot convert SRT → PGS.
* `dca_core` — *"Extract the core from a DCA/DTS stream, dropping extensions such as
  DTS-HD"* (upstream docs). Verified working: plain DTS in → DTS out, unchanged size.
* `eac3_core` — *"Extract the core from a E-AC-3 stream, dropping extra channels"*
  (upstream docs); this is the DD+/Atmos → AC-3-core path.
* `truehd_core` — *"Extract the core from a TrueHD stream, dropping ATMOS data"*
  (upstream docs). It does **not** yield an AC-3 core; there is no FFmpeg AC-3-core
  extractor for TrueHD. Our TrueHD strategy therefore is: use the AC-3 stream that
  ffprobe exposes separately when the TrueHD track carries an embedded core,
  otherwise refuse the track with an explicit reason.
* FFmpeg's experimental `truehd` **encoder** fails in this build (`return code -22`),
  so TrueHD end-to-end tests are impossible locally; that path is covered by unit
  tests over canned ffprobe output.
* Subtitle/text handling: SRT/ASS → SRT via `-f srt -c:s srt` is lossless enough for
  rendering, and tsMuxeR needs UTF-8/16/32 SRT input.
* E-AC-3 without an AC-3 core: `-bsf:a eac3_core -f ac3` **fails at the muxer**
  ("ac3 muxer supports only codec ac3 for type audio") and leaves behind a file
  that ffprobe reports as `ac3` with **0 channels** — verified with a
  lavfi-generated DD+ stream. Two independent guards therefore fire for a
  coreless source: ffmpeg's non-zero exit and `prep.check_prep_output`'s
  channel check. This is the expected, explicit failure rather than a silent
  wrong mux.

### 2.4 SRT → PGS actually works (chosen fallback, verified)

`S_TEXT/UTF8, "sub.srt", track=0, lang=eng, video-width=1920, video-height=1080,
fps=23.976, font-name="DejaVu Sans", font-size=65, bottom-offset=24,
font-color=0xEBFFFFFF` produced an output `.m2ts` whose ffprobe stream list
contains `hdmv_pgs_subtitle` **(verified)** — real bitmap PGS, no burn-in, no
re-encode of video.

### 2.5 Path handling

Paths containing spaces and non-ASCII characters (`scratch/dir with spaces/ünïcØde/
säm ple.mkv`) mux correctly when the meta file is written as UTF-8 with quoted,
escaped paths **(verified)**.

### 2.6 What `--split-size` really means

* **`MB` is 10⁶ bytes, not MiB.** `--help` on this build prints only the version
  line, so the authority is the upstream source
  (`justdan96/tsMuxer`, `tsMuxer/tsMuxer.cpp::parseMuxOpt`), where the size table is
  `KB=1000, KiB=1024, MB=1000000, MiB=1048576, …` **(source-verified)**.
* **Splitting happens at the next I-frame after the target is exceeded**, so chunks
  overshoot the target by however long the current GOP still runs **(verified)**:

  | `--split-size` | chunk sizes observed (bytes) |
  | --- | --- |
  | `1MB` | 1,585,152 + 1,388,544 |
  | `500KB` | 909,312 + 681,984 + 663,552 + 731,136 |

  Consequences encoded in `backend.py`: the target is computed with
  `SPLIT_MB_BYTES = 1_000_000`, a chunk may exceed the target by up to
  `SPLIT_OVERSHOOT_ALLOWANCE = 32 MiB` before the job fails, and the hard FAT32
  ceiling is `FAT32_MAX_BYTES = 4_294_967_295`. The default 4096 MB target sits
  ~199 MB below that ceiling, so even a 32 MiB overshoot cannot reach it.
* **Every split part repeats all streams**, so verifying the *first* `.m2ts` is
  enough to prove the stream composition of the whole tree **(verified)**.
* One full backend run — ffmpeg SRT → SRT prep steps, tsMuxeR mux of
  H.264 + 2×AC-3 + 2 SRT tracks, output verification — produced
  `BDMV/STREAM/00000.m2ts` whose ffprobe stream list is
  `H264, AC3 6ch, AC3 2ch, HDMV_PGS_SUBTITLE, HDMV_PGS_SUBTITLE` **(verified)**;
  with `keep_temp_on_success` the temp dir holds exactly the two prepared
  `sub-*.srt` files plus `tsmuxer-meta.txt`.

### 2.7 What `lsblk` does and does not tell us

Captured from util-linux 2.41.5 (`lsblk -J -o NAME,MOUNTPOINT,FSTYPE,LABEL,RM,SIZE,MODEL,UUID`),
shape preserved in `tests/data/drives/lsblk.json` **(verified)**:

* Partitions are nested under their disk (`children`); `model` and the `rm`
  (*removable*) flag appear on the **parent** only, so `drives.py` inherits both
  down the tree — a partition of a USB stick is on a removable device by
  definition.
* Unmounted partitions have `mountpoint: null` and must be skipped: there is no
  directory to write into. Swap is not unmounted but "mounted" at `[SWAP]`, also
  skipped.
* Sizes are human strings (`"447.1G"`) in **binary** powers (G = GiB); `-b` would
  print bytes. The lists also carry snap/ISO loop devices (`squashfs`), which are
  skipped by name prefix and fstype.
* `lsblk` only knows *block devices*: a `tmpfs` (`/tmp`, `/run`) is invisible, so
  `assess_target` may name a block-device mount while `shutil.disk_usage` reports
  the tmpfs's free space. The free-space figure is the authoritative one for V16.

### 2.8 The GUI path, live-verified

`MainWindow` driven offscreen (`QT_QPA_PLATFORM=offscreen`,
`AVCHD_FORGE_TSMUXER=/tmp/opencode/tsmuxer/linux/tsMuxeR`) through the real
pipeline, on `e2e-src.mkv` (1080p H.264, 2×AC-3, 2×SubRip) **(verified)**:

* `check_tools()` resolved tsMuxeR 2.7.0, ffmpeg and ffprobe 7.1.5-0+deb13u1;
  `refresh_drives()` listed this machine's four mounted filesystems and the
  output badge said `not FAT32 (EXT4)` with the V15 warning inline.
* Default selection from the probe was `audio=(1,)`, subtitles empty (no PGS in
  the source); Start was enabled only with a probe, an output folder and a
  resolved toolchain.
* Both SubRip tracks ticked in the window → the job ran ffmpeg SRT prep steps →
  tsMuxeR → verification, and the produced `.m2ts` reports
  `H264, AC3 6ch, AC3 2ch, HDMV_PGS_SUBTITLE, HDMV_PGS_SUBTITLE`.
* A `QTimer.singleShot(250, window.cancel)` fired *during the ffmpeg prep step*
  (nested event loop, D13) and surfaced as `Cancelled normalise SubRip to UTF-8
  for tsMuxeR`, with the temp dir kept, nothing in the output dir, and Start
  enabled again.

### 2.9 Preferences and the startup gate, live-verified

Step 16 driven offscreen against the real toolchain (tsMuxeR 2.7.0,
ffmpeg/ffprobe 7.1.5-0+deb13u1) **(verified)**:

* A missing `tsMuxeR` (override pointing at `/opt/nope/tsMuxeR`, with
  `$AVCHD_FORGE_TSMUXER` unset — the env variable is an explicit override too and
  would have won) gave `ok=False`, `missing=['tsMuxeR']`, Start disabled with
  "External tools missing — Tools ▸ Preferences… can set their paths", and the
  gate behaviour of D18: no *Continue*, *Re-check* as the default, *Quit* on Esc,
  and `tsMuxeR:\n    /opt/nope/tsMuxeR (configured path)` in the details area.
* Preferences' *Check now*, given the same real path typed into the box, ran the
  six real queries through `QProcessRunner` and showed
  `tsMuxeR 2.7.0 — /tmp/…/tsMuxeR` while the window's `toolchain` stayed `None`:
  `check_tools(adopt=False)` answers without adopting (D19).
* With `ffmpeg` pointed at the tsMuxeR binary the report came back `ok` **with a
  warning** ("ffmpeg did not report a version"): the gate offered *Continue* and
  `ensure_tools()` returned `True` once a timer clicked it.
* A `QTimer`-driven accept of the Preferences dialog (splitting off, audio
  priority `jpn`) was applied, the tools re-checked, and the target re-assessed
  ("FAT32 splitting stays off").
* The split spin's maximum is **4261 MB** (`(4 GiB − 32 MiB) ÷ 10⁶`), above the
  4096 default. The first cut of the dialog used a literal 4095, which silently
  clamped the default the moment the dialog was opened; the dialog now reads
  `AppSettings.stored_split_size_mb()`, so a custom size also survives splitting
  being switched off (D19).

### 2.10 The committed e2e smoke test (`tests/test_e2e.py`)

Step 18 turns the earlier ad-hoc live runs into two tests marked `e2e`, excluded
from the default run (`567 passed, 2 deselected`) **(verified)**:

* The sample (12 s, 1080p24 H.264 High@4.1, AC-3 5.1 eng + AC-3 2.0 jpn, one
  SubRip) is generated by the installed ffmpeg, and the tools are located with
  the same `locate_tool` the application uses, so `$AVCHD_FORGE_TSMUXER` points
  the test at an unpacked tsMuxeR. A missing tool skips the module with the
  condensed search list, and an ffmpeg without `libx264` skips the sample.
* With tsMuxeR 2.7.0 / ffmpeg 7.1.5: the default 4096 MB split produces exactly
  one `.m2ts` plus `index.bdmv`, `MovieObject.bdmv`, a playlist and
  `CERTIFICATE/`; the temp dir is removed on success; the produced stream
  reports `H264 1920x1080, AC3 6ch, AC3 2ch, HDMV_PGS_SUBTITLE` — the video is
  still H.264 and the SubRip arrived as PGS.
* `--split-size=1` on the same sample produces 3 parts, each below
  1 MB + `SPLIT_OVERSHOOT_ALLOWANCE` (D11). Both tests take ~3 s together.

## 3. Decision log

| # | Date | Decision | Why |
| --- | --- | --- | --- |
| D1 | 2026-10-15 | SRT/ASS → PGS is done by tsMuxeR rendering, with an ffmpeg `-c:s pgssub` fast path guarded by capability detection | approved user answer; stock FFmpeg has no PGS encoder |
| D2 | 2026-10-15 | TrueHD handling: embedded AC-3 core only, otherwise refuse | `truehd_core` semantics make the brief's assumption impossible; re-encoding is forbidden |
| D3 | 2026-10-15 | Extracted cores are always written to temp files and referenced as `track=0` | removes 1-based/0-based index ambiguity and survives tsMuxeR's silent bad-index behaviour |
| D4 | 2026-10-15 | Post-mux composition verification is mandatory | tsMuxeR exits 0 while producing wrong output (§2.2) |
| D5 | 2026-10-15 | `track=` is never derived arithmetically from the ffprobe index | index+1 holds for MKV only; TS needs PIDs and wrong values either fake a track or segfault (§2.1) |
| D6 | 2026-10-15 | Core logic stays Qt-free behind a `Runner` protocol | tests need no Qt, no processes; only the GUI path uses `QProcess` |
| D7 | 2026-10-15 | A tsMuxeR **detection run on the source is a mandatory preflight**; its `Track ID:` values are the `track=` numbers | the only authority on how tsMuxeR numbers the input |
| D8 | 2026-10-15 | Track resolution aligns ffprobe streams with detection entries per media kind, monotonic in file order, and only when the codec **family** matches (`A_AC3` covers AC-3/AC-3+/TrueHD, `pcm_*` counts as LPCM); anything unmapped is a hard error | guessing wrong drops audio silently or crashes tsMuxeR |
| D9 | 2026-10-15 | Extracted cores/`.srt` are written to temp and referenced as raw elementary streams (`track=0`, ignored by tsMuxeR) | removes the TS/MKV numbering question entirely for prepared tracks |
| D10 | 2026-10-15 | A `JobPlan` is a pure function of media + selection + detection + options, and carries one expected codec family (and channel count) per stream of the finished disc | the post-mux check (D4) needs a machine-checkable expectation, and keeping the plan pure makes it unit-testable without running any tool |
| D11 | 2026-10-15 | Split verification measures with `SPLIT_MB_BYTES = 10⁶` and fails a chunk only when it exceeds target + 32 MiB | tsMuxeR splits at keyframes and `MB` is decimal (§2.6); a hard `≤ target` assertion would fail on every real file |
| D12 | 2026-10-15 | `execute_plan` takes an explicit `owns_temp` flag; `jobs.py` passes `True` for the `mkdtemp` directory it created, and a cancellation always keeps the temp dir even when failures are dropped | inferring ownership from "did the directory exist" is wrong for the GUI path (the job pre-creates it); keeping evidence on cancel is the documented temp policy |
| D13 | 2026-10-15 | `JobRunner` runs on the GUI thread inside `QProcessRunner`'s nested event loop — no worker thread, no `moveToThread` | the nested loop keeps delivering input events, which is *why* Cancel works mid-mux; verified live: a timer that fired during the mux killed the child and surfaced as a cancellation with the temp dir kept |
| D14 | 2026-10-15 | Drives come from `lsblk -J` (identity, label, removable, model, size) with `/proc/mounts` as the fallback; a partition inherits its disk's `rm`/`model`; `MB`-style sizes are binary (lsblk `G` = GiB) | `/proc/mounts` alone cannot tell a USB stick from an internal disk, and lsblk reports `rm`/`model` only on the parent device; verified against util-linux 2.41.5 (`sha256` of the shape in `tests/data/drives/lsblk.json`) |
| D15 | 2026-10-15 | A finished/failed/cancelled run is reported **in the run bar and the log pane only** — no modal dialog is ever opened from a job signal; modal dialogs exist only behind menu actions | a `QMessageBox` inside `QProcessRunner`'s nested event loop re-enters the event loop from within a job, and GUI tests cannot dismiss it; the full text is one tooltip away |
| D16 | 2026-10-15 | The log pane is a collapsible, resizable `QSplitter` pane with its own `QToolButton`, not a `QDockWidget`; geometry, window state and log visibility persist via `AppSettings` | one collapse mechanism (the button the plan asks for) instead of two competing ones; `saveState()` stays wired in case real docks arrive later |
| D17 | 2026-10-15 | Start is blocked only by missing tools, unusable media, no output folder or a read-only target; V15/V16 target warnings never block; the free-space estimate is `int(media.size_bytes * OUTPUT_OVERHEAD)` until a `JobPlan` exists | warnings must inform, not refuse (the brief), and the source size is the only estimate available before the plan is built; the mux re-packages the same streams, so the source size is the right ballpark |
| D18 | 2026-10-15 | The startup gate is a `QMessageBox` that answers with a plain string (`recheck` / `preferences` / `continue` / `quit`); *Continue* exists **only** when the report is usable, the searched locations live in the details area, and anything that is not one of the buttons (Esc, the window manager) means *quit*; `main()` exits `1` when the gate is quit | a warned-but-working toolchain (old ffmpeg) must be continuable while a missing tsMuxeR must not, and guessing "continue" on a broken toolchain produces a half-written disc with no explanation; strings keep the loop drivable from `main()` and from tests without a modal event loop |
| D19 | 2026-10-15 | Preferences edits *stored* state and never adopts it before OK: `MainWindow.check_tools(overrides, *, adopt=False)` answers without touching the window (so an unsaved typo cannot disable Start), and the split size is read/written as `AppSettings.stored_split_size_mb()` — independent of `split_enabled` — with the spin's maximum derived from `(FAT32_MAX_BYTES - SPLIT_OVERSHOOT_ALLOWANCE) // SPLIT_MB_BYTES` | an open dialog is not a decision yet; and editing the *effective* split size (which is `None` while splitting is off) reset a custom value to the default on every open-and-OK — the same reasoning as "V15/V16 inform, they do not block" |
| D20 | 2026-10-16 | A non-standard video resolution stays a hard V4 error by default; the Muxing-tab preference `mux/allow_nonstandard_resolution` downgrades it to a warning, and the flag travels in `PlanOptions` so `build_job_plan`/`plan_for_source`/`build_plan` re-validate with the same policy; changing it re-checks the loaded source in `open_preferences` | cropped 1.85:1 BDRemux releases (1920x1036/1038) are common and often play on the PS3, but they are outside AVCHD and cannot be padded losslessly (that needs a re-encode); a default-off opt-in keeps the strict guarantee while letting the user take the risk knowingly |
| D21 | 2026-10-16 | An output folder that already contains an AVCHD tree (`BDMV/` or `AVCHD/BDMV/`, rule V20) blocks a job: `TargetReport.errors` disables Start, and `build_job_plan` raises `ExistingTreeError` before any tool runs. This extends D17's blocker list; V15/V16 stay warnings. | tsMuxeR writes fixed names (`index.bdmv`, `00000.m2ts`, …), so a second run into the same folder silently overwrites the first title; a stock PS3 recognises only one AVCHD structure per drive, so one folder per title is the layout that keeps every movie on the stick |

## 4. Open items

* `tests/data/tsmuxer/detect_mkv.txt` must be refreshed if a future tsMuxeR changes
  the detection layout — the parser is deliberately lenient about spacing.
* AVCHD legality of AAC and of level 4.2 is treated as a warning, not an error
  (PS3 plays both); revisit only if users report rejections.
* Non-standard resolutions ride the same philosophy behind the default-off
  `mux/allow_nonstandard_resolution` opt-in (D20). The PS3 verdict is
  unverified — ideally one cropped (1920x1036/1038) sample gets tested live and
  the outcome recorded here.
* Core extraction cannot be exercised end-to-end with generated media: ffmpeg's
  own `eac3` encoder writes no AC-3 core (so `eac3_core` has nothing to emit,
  checked on 7.1.5) and there is no DTS-HD or TrueHD *encoder* at all. Those two
  paths are pinned by argv/unit tests only; a real Blu-ray sample would be
  needed for a live run.
* Windows/macOS drive detection is best-effort via `os.listdrives`/`lsblk` fallback.
