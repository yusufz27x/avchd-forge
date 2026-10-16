# avchd-forge

Turn an AVC BDRemux release (`.mkv` / `.ts`) into a **PS3-compatible AVCHD
`BDMV/` folder structure** on an external (usually FAT32) USB drive — remux only,
the video is never re-encoded.

avchd-forge is a PySide6 desktop application that:

* probes the source with `ffprobe` and shows Video / Audio / Subtitle panes,
* lets you pick exactly which audio and subtitle tracks go onto the disc,
* extracts DTS or AC-3 **cores** with `ffmpeg` bitstream filters when the selected
  track is an HD flavour (DTS-HD MA, E-AC-3/Atmos) — bitstream-lossless, no
  re-encode,
* writes a tsMuxeR `.meta` file and muxes an AVCHD tree, splitting the output at
  4096 MB so it survives FAT32 (default on),
* verifies the produced `BDMV/` tree and its stream composition before declaring
  success.

## How it works

Every run follows the same pipeline, and every command it runs is printed in the
log pane:

1. **probe** — `ffprobe -print_format json -show_format -show_streams` → stream
   inventory.
2. **validate** — the compatibility rules below, as errors (block) or warnings
   (inform only).
3. **prep** — selected DTS-HD/TrueHD/E-AC-3 tracks are losslessly core-extracted,
   and text subtitles are converted, with `ffmpeg` into a private temp directory.
4. **mux** — a `.meta` file is generated (`MUXOPT --avchd … --split-size=4096MB`)
   and `tsMuxeR <meta> <output>` writes the `BDMV/` + `CERTIFICATE/` tree into
   an `AVCHD` folder it creates inside the chosen folder (unless that folder is
   already one), optionally split every N MB.
5. **verify** — tsMuxeR's tree is renamed to the AVCHD-on-FAT32 (8.3) names the
   PS3 mounts from USB (`INDEX.BDM`, `MOVIEOBJ.BDM`, `*.MPL`, `*.CPI`, `*.MTS`),
   all required files must exist, every stream part must respect the split
   limit, and the streams inside must match the plan (codec family, channel
   count).
6. **cleanup** — the temp directory is removed on success and **kept on
   failure**, with an *Open temp dir* button and the exact command/exit code in
   the log.

## Requirements

* **Python 3.10+** with PySide6 (installed by pip — see below).
* **tsMuxeR** (tested with 2.7.0) — the remux backend.
* **ffmpeg** and **ffprobe** (tested with 7.1.x; 5.0+ recommended) — probing,
  core extraction, subtitle conversion.

The three external binaries are not installable via pip. avchd-forge locates
them on `PATH`, or you can set explicit paths in *Tools ▸ Preferences…*
(stored with `QSettings`). The environment variables `AVCHD_FORGE_TSMUXER`,
`AVCHD_FORGE_FFMPEG` and `AVCHD_FORGE_FFPROBE` also work and win over `PATH`;
an explicit path — from Preferences or the environment — is never silently
replaced by a look-alike found later.

At startup the app checks all three and opens a gate: when a tool is missing you
can open *Preferences…* (it lands on the External tools tab), *Re-check*, or
*Quit*; *Continue* is offered only when the toolchain is usable (a
warned-but-working ffmpeg can be continued).

## Install and run

Runtime only:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # or: pip install .
```

Development install:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Then:

```bash
.venv/bin/avchd-forge                          # empty window
.venv/bin/avchd-forge movie.mkv                # open (and probe) a source
.venv/bin/avchd-forge movie.mkv --output /media/usb
.venv/bin/python -m avchdforge --version
```

The same thing launches from any of those; `--output` only pre-selects the
folder, and the source can also be dragged onto the window.

## Using the window

* **Source** — *Open…* or drag exactly one `.mkv`/`.ts`/`.m2ts`/`.mpg`/`.mpeg`
  file onto the window. Probing runs in the background and fills the panes; the
  log shows the raw `ffprobe` JSON.
* **Tracks** — the video pane shows codec/level/resolution/bitrate with
  incompatible values highlighted; audio and subtitle tables have checkboxes.
  Defaults: the first compatible audio track is ticked, PGS subtitles are
  ticked, text subtitles are off (they cost a conversion pass), forced-subtitle
  flags are shown per row. Incompatible rows are disabled and explain why in
  their tooltip.
* **Output** — pick a folder or a detected removable drive from the combo. The
  badge shows `FAT32`, `exFAT`, `NTFS`, … and inline warnings tell you when the
  split setting and the filesystem disagree (V15) or when the estimate may not
  fit in the free space (V16). Warnings never block; a read-only target or a
  folder that already holds an AVCHD tree does (V20). The app writes into an
  `AVCHD` folder inside the chosen folder — the one the PS3 mounts from a USB
  drive root — creating it if needed (a folder already named `AVCHD` is used
  as-is).
* **Run bar** — Start/Cancel, current stage, progress bar and elapsed time. When
  the run ends the bar says what happened; success offers *Open output folder*,
  failure offers the kept *Open temp dir*.
* **Log pane** — collapsible and resizable, monospace, streaming `tsMuxeR` /
  `ffmpeg` output with `Command:` lines kept. *Copy* and *Save log…* are in its
  toolbar.
* **Preferences** (*Tools ▸ Preferences…*, also via the startup gate) — tool
  paths with a *Check now* probe of the installed versions, output/temp folder,
  split size, subtitle font, and audio/subtitle language priorities that drive
  the default tick marks. Nothing is adopted until you press OK.

## What the PS3 accepts (compatibility rules)

**Video — never re-encoded.** Hard errors: not exactly one video stream, codec
not H.264/AVC, resolution not exactly 1920×1080 or 1280×720. Warnings: H.264
level above 4.1, 1080-line frame rate above 30 (720-line above 60), interlaced
input, bitrate above 24 Mbps.

**Audio — copied, or core-extracted.** No full audio re-encode ever happens:

| Source track | What avchd-forge does |
| --- | --- |
| AC-3, DTS, LPCM, AAC | copied as-is |
| DTS-HD MA / HRA / DTS:X | DTS core extracted (`dca_core`), then copied |
| E-AC-3 / Dolby Atmos (DD+) | AC-3 core extracted (`eac3_core`), then copied |
| TrueHD | the AC-3 core ffprobe exposes inside the TrueHD stream is demuxed and copied; if there is no embedded core the track is refused with a reason instead of being re-encoded |
| anything else (FLAC, Opus, …) | refused, with the reason in the row tooltip |

**Subtitles — copied or converted, never burned in.** PGS is copied untouched.
SRT/ASS are converted to UTF-8 SRT with `ffmpeg` *(text→text, no video touched)*
and then rendered to PGS by tsMuxeR's own AVCHD mux — stock ffmpeg ships a PGS
decoder but no PGS encoder, so this is the approved fallback; see
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) §4.

## FAT32 and splitting

The PS3 reads FAT32 and caps files at 4 GiB, so splitting is **on by default at
4096 MB** (`tsMuxeR --split-size`; additional `00001.MTS` parts are normal for
the PS3). If the target filesystem is exFAT/NTFS/ext4 the app warns that
splitting is unnecessary (V15) but never blocks — and if splitting is left on,
the extra parts still play.

AVCHD on FAT32 uses legacy 8.3 file names, and a PS3 only mounts an AVCHD
structure from USB when they are present — with the longer optical-media names
tsMuxeR writes, the drive's files stay browsable and the stream parts still play
as ordinary video, but no AVCHD entry appears in the XMB. After a successful
mux, avchd-forge renames the tree (`BACKUP/` included, no file content touched)
and verifies the result:

```
<output>/AVCHD/BDMV/INDEX.BDM
              /BDMV/MOVIEOBJ.BDM
              /BDMV/PLAYLIST/*.MPL
              /BDMV/STREAM/*.MTS
              /CERTIFICATE/…
```

The app creates the `AVCHD` folder itself: point it at the drive root (or at any
empty folder you want a title in) and the tree is written into `<folder>/AVCHD`,
which is exactly where the PS3 mounts it from. Pointing directly at an existing
`AVCHD` folder works too — the name is recognised case-insensitively and nothing
is nested twice.

## Several movies on one drive

A stock PS3 recognises **one AVCHD structure per drive**, so exactly one folder
may be called `AVCHD` at the root. Give every movie its own folder and point the
app at it — each run creates the `AVCHD` folder inside:

```
<drive>/AVCHD/          ← the movie the PS3 sees now (BDMV/ + CERTIFICATE/)
<drive>/Title B/AVCHD/  ← full tree, waiting
<drive>/Title C/AVCHD/  ← full tree, waiting
```

To switch titles, move the folders — instant on FAT32, only directory entries
change; re-insert the drive and the PS3 shows the new title:

```bash
mkdir "Title A"                # park the current title
mv AVCHD "Title A/AVCHD"
mv "Title B/AVCHD" AVCHD       # activate Title B
```

(A tree that sits directly in a folder — made by an older version or another
tool — is activated by renaming that folder itself to `AVCHD`.)

avchd-forge refuses to start when the chosen output folder already contains a
`BDMV` tree (`BDMV/` or `AVCHD/BDMV/`) — the mux reuses fixed file names
(`INDEX.BDM`, `00000.MTS`, …) and a second run would overwrite the first movie.
Multi-title discs (several playlists inside one BDMV, the multiAVCHD approach)
are not supported.

## Troubleshooting

* **Start refuses to enable** — look at the status bar: tools unresolved, no
  source or an unusable one, no output folder, a read-only target, or an output
  folder that already holds an AVCHD tree (choose an empty folder).
* **No AVCHD entry on the PS3** — the tree must sit in a folder called `AVCHD`
  at the root of a FAT32 device (avchd-forge creates it; pointing the app at the
  drive root is enough) and carry the 8.3 names it writes (`INDEX.BDM`,
  `*.MTS`, …). If the stream parts play from the file browser (Triangle ▸
  *Display All*) but no AVCHD icon appears, the folder name/case or the file
  names are off.
* **A run failed** — the temp directory is kept; *Open temp dir* shows the
  intermediate files, and the log has the exact argv, working directory, exit
  code and full output of the failing command.
* **Progress looks stuck** — the log pane keeps streaming; probing and tsMuxeR's
  first pass can take a while on multi-GB sources.
* **Wrong tool found** — set the path in Preferences; `PATH` order is only used
  when nothing explicit was configured.

## Development

```
src/avchdforge/     Qt-free core: probe · validate · meta · plan · prep ·
                    backend · drives · tracks · tooling
                    Qt layer: process · jobs · settings · ui/
tests/              pytest; every external process is mocked (FakeRunner)
docs/               IMPLEMENTATION_PLAN.md, CONTEXT.md (decisions + evidence)
scripts/            golden-file updater, test-fixture generator
```

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest        # unit tests, no tools needed
.venv/bin/pytest -m e2e                           # real tsMuxeR/ffmpeg smoke test
.venv/bin/ruff check src tests                    # lint
.venv/bin/python scripts/update_golden_metas.py   # refresh meta/plan goldens
```

`tests/test_qt_process.py` is the only default-run module that starts child
processes; everything else drives the pure core through a recording
`FakeRunner`, so the suite runs headless and without tsMuxeR/ffmpeg installed.
Real-tool tests are marked `e2e` and excluded by default.

## License

MIT — see [`LICENSE`](LICENSE).
