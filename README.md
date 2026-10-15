# avchd-forge

Turn AVC BDRemux releases (`.mkv` / `.ts`) into a **PS3-compatible AVCHD `BDMV/`
folder structure** on an external USB drive — remux only, never re-encode.

avchd-forge is a small PySide6 desktop application that:

* probes your source with `ffprobe` and shows Video / Audio / Subtitle panes,
* lets you pick exactly which audio and subtitle tracks go onto the disc,
* extracts DTS/AC-3 **cores** with `ffmpeg` bitstream filters when a track is HD
  (DTS-HD MA, E-AC-3/Atmos) — bit-for-bit, no re-encode,
* writes a tsMuxeR `.meta` file and muxes an AVCHD tree, splitting the output at
  4096 MB so it survives FAT32 (default on),
* verifies the produced `BDMV/` tree and its stream composition before declaring
  success.

Detailed usage, limitations and troubleshooting are documented further down; the
authoritative design notes live in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
and [`docs/CONTEXT.md`](docs/CONTEXT.md).

## Requirements

* Python 3.10+
* `tsMuxeR` (tested with 2.7.0), `ffmpeg` and `ffprobe` on `PATH` (or set
  explicitly in Preferences). The app checks for them at startup and tells you
  what is missing.

## Install and run

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/avchd-forge            # or: .venv/bin/python -m avchdforge
.venv/bin/pytest                 # unit tests (no external tools needed)
```

## Status

Early development: the core (probe → validate → plan → meta → mux → verify) and its
unit tests come first, then the GUI on top. See the plan for the exact order.

## License

MIT — see [`LICENSE`](LICENSE).
