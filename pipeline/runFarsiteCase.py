#!/usr/bin/env python3
"""
Step 11 of runPipelineParallel: execute FARSITE (Wine) for one case.

Prerequisites (produced by step 9 – prepareFarsite):
    <case_dir>/farsite/farsite.txt
    <case_dir>/farsite/farsite.input
    <case_dir>/farsite/landscape.lcp
    <case_dir>/farsite/ignition.shp
    <case_dir>/farsite/barrier.shp   (if USE_BARRIER is True)
    <case_dir>/farsite/outputs/      (directory must exist)

What this script does
---------------------
1.  Runs  WINEDEBUG=-all wine64 <FARSITE_EXE> farsite.txt  from within farsite_dir
2.  Skips the case when  <outputs>/farsite_Arrival Time.tif  already exists.

Standalone usage (process all cases under FIRE_ROOT):
    python runFarsiteCase.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pipelineConfig as cfg
from parallel_api import run_subprocess

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FIRE_ROOT    = Path(cfg.FIRE_ROOT)
FARSITE_EXE  = Path(cfg.FARSITE_FB_DIR) / cfg.FARSITE_EXE_NAME

ARRIVAL_TIME_TIF = "farsite_Arrival Time.tif"   # completion sentinel


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_farsite(case_dir: Path) -> None:
    """Run FARSITE for *case_dir*.  Skips if outputs already exist."""
    case_dir    = Path(case_dir).absolute()
    farsite_dir = case_dir / "farsite"
    outputs_dir = farsite_dir / "outputs"
    sentinel    = outputs_dir / ARRIVAL_TIME_TIF

    if sentinel.exists():
        print(f"  Skipped — FARSITE outputs already exist.")
        return

    cmd_file = farsite_dir / "farsite.txt"
    if not cmd_file.exists():
        raise FileNotFoundError(
            f"farsite.txt not found in {farsite_dir}. "
            "Run prepareFarsite (step 9) first."
        )

    # ---- run ------------------------------------------------------------
    wine_env = {**os.environ, "WINEDEBUG": "-all"}
    print(f"  Running: wine64 {FARSITE_EXE.name} farsite.txt")
    run_subprocess(
        ["wine64", str(FARSITE_EXE), "farsite.txt"],
        cwd=str(farsite_dir),
        env=wine_env,
    )

    if sentinel.exists():
        print(f"  FARSITE complete — outputs in {outputs_dir}")
    else:
        raise RuntimeError(
            f"FARSITE finished but '{ARRIVAL_TIME_TIF}' was not created. "
            "Check the output above for errors."
        )

    # ---- clean up outputs -----------------------------------------------
    keep = {ARRIVAL_TIME_TIF}

    removed = 0
    for f in outputs_dir.iterdir():
        if f.is_file() and f.name not in keep:
            f.unlink()
            removed += 1
    print(f"  Cleaned farsite outputs: kept {sorted(keep)}, removed {removed} file(s)")


def main(case_dir=None) -> None:
    if case_dir is not None:
        run_farsite(Path(case_dir))
        return

    for folder in sorted(FIRE_ROOT.iterdir()):
        if folder.is_dir() and folder.name.isdigit():
            print(f"\nFolder {folder.name}:")
            run_farsite(folder)


if __name__ == "__main__":
    main()
