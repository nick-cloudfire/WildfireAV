#!/usr/bin/env python3
"""
Step 11 of runPipelineParallel: execute FARSITE (Linux native) for one case.

Prerequisites (produced by step 9 – prepareFarsite):
    <case_dir>/farsite/farsite.input
    <case_dir>/farsite/landscape.lcp
    <case_dir>/farsite/ignition.shp
    <case_dir>/farsite/barrier.shp   (if USE_BARRIER is True)
    <case_dir>/farsite/outputs/      (directory must exist)

What this script does
---------------------
1.  Writes  <case_dir>/farsite/farsite_linux.txt  with one absolute-path command line
2.  Runs  <FARSITE_EXE> farsite_linux.txt
3.  Skips the case when  <outputs>/farsite_ArrivalTime.asc  already exists.

Standalone usage (process all cases under FIRE_ROOT):
    python runFarsiteCase.py
"""

from __future__ import annotations

from pathlib import Path

import pipelineConfig as cfg
from parallel_api import run_subprocess

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FIRE_ROOT    = Path(cfg.FIRE_ROOT)
FARSITE_EXE  = Path(cfg.FARSITE_FB_DIR) / cfg.FARSITE_EXE_NAME

ARRIVAL_TIME_ASC = "farsite_ArrivalTime.asc"   # completion sentinel


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_farsite(case_dir: Path) -> None:
    """Run FARSITE for *case_dir*.  Skips if outputs already exist."""
    case_dir    = Path(case_dir).absolute()
    farsite_dir = case_dir / "farsite"
    outputs_dir = farsite_dir / "outputs"
    sentinel    = outputs_dir / ARRIVAL_TIME_ASC

    if sentinel.exists():
        print(f"  Skipped — FARSITE outputs already exist.")
        return

    farsite_input = farsite_dir / "farsite.input"
    if not farsite_input.exists():
        raise FileNotFoundError(
            f"farsite.input not found in {farsite_dir}. "
            "Run prepareFarsite (step 9) first."
        )

    # ---- build command file ---------------------------------------------
    barrier_shp = farsite_dir / "barrier.shp"
    barrier_arg = str(barrier_shp) if barrier_shp.exists() else "0"

    cmd_line = " ".join([
        str(farsite_dir / "landscape.lcp"),
        str(farsite_dir / "farsite.input"),
        str(farsite_dir / "ignition.shp"),
        barrier_arg,
        str(outputs_dir / "farsite"),   # output base name (no extension)
        "1",                             # output type: ASCII grid (.asc)
    ])

    cmd_file = farsite_dir / "farsite_linux.txt"
    cmd_file.write_text(cmd_line + "\n")

    # ---- run ------------------------------------------------------------
    print(f"  Running: {FARSITE_EXE.name} farsite_linux.txt")
    run_subprocess(
        [str(FARSITE_EXE), str(cmd_file)],
        cwd=str(farsite_dir),
    )

    if sentinel.exists():
        print(f"  FARSITE complete — outputs in {outputs_dir}")
    else:
        raise RuntimeError(
            f"FARSITE finished but '{ARRIVAL_TIME_ASC}' was not created. "
            "Check the output above for errors."
        )

    # ---- clean up outputs -----------------------------------------------
    keep = {ARRIVAL_TIME_ASC}

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
