#!/usr/bin/env python3
"""Regenerate the golden meta files in ``tests/data/meta``.

Run after an intentional change to the meta format, then *read the diff* before
committing: these files are the contract with tsMuxeR.

Usage: .venv/bin/python scripts/update_golden_metas.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from avchdforge.meta import render_meta  # noqa: E402
from meta_jobs import JOBS  # noqa: E402

TARGET = ROOT / "tests" / "data" / "meta"


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, factory in JOBS.items():
        path = TARGET / f"{name}.meta"
        path.write_text(render_meta(factory()), encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
