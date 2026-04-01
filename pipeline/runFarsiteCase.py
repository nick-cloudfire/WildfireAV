#!/usr/bin/env python3
"""
Step 11 of runPipelineParallel: execute FARSITE via Wine for one case.

Prerequisites (produced by step 9 – prepareFarsite):
    <case_dir>/farsite/farsite.input
    <case_dir>/farsite/landscape.lcp
    <case_dir>/farsite/ignition.shp
    <case_dir>/farsite/barrier.shp   (if USE_BARRIER is True)
    <case_dir>/farsite/outputs/      (directory must exist)

What this script does
---------------------
1.  Converts all required paths to Wine Windows paths  (Z:\...)
2.  Writes  <case_dir>/farsite/farsite_wine.txt  with one absolute-path command line
3.  Sets GDAL_DATA and PROJ_LIB to the Wine equivalents of the SDK's data dirs
4.  Runs  wine <FB_BIN>/TestFARSITE.exe farsite_wine.txt
5.  Skips the case when  <outputs>/farsite_Arrival Time.tif  already exists.

Standalone usage (process all cases under FIRE_ROOT):
    python runFarsiteCase.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pipelineConfig as cfg
from parallel_api import run_subprocess

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FIRE_ROOT    = Path(cfg.FIRE_ROOT)
FB_BIN       = Path(cfg.FARSITE_FB_DIR) / "bin"
FARSITE_EXE  = FB_BIN / cfg.FARSITE_EXE_NAME

ARRIVAL_TIME_TIF = "farsite_Arrival Time.tif"   # completion sentinel
WIND_GRIDS_TIF   = "farsite_WindGrids.tif"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_wine_path(linux_path: Path) -> str:
    """Convert an absolute Linux path to a Wine Z:\\ Windows path."""
    return "Z:\\" + str(linux_path.absolute()).replace("/", "\\")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_farsite(case_dir: Path) -> None:
    """Run FARSITE via Wine for *case_dir*.  Skips if outputs already exist."""
    case_dir    = Path(case_dir).absolute()
    farsite_dir = case_dir / "farsite"
    outputs_dir = farsite_dir / "outputs"
    sentinel    = outputs_dir / ARRIVAL_TIME_TIF

    if sentinel.exists():
        print(f"  Skipped — FARSITE outputs already exist.")
        return

    farsite_input = farsite_dir / "farsite.input"
    if not farsite_input.exists():
        raise FileNotFoundError(
            f"farsite.input not found in {farsite_dir}. "
            "Run prepareFarsite (step 9) first."
        )

    # ---- build Wine-path command line -----------------------------------
    barrier_shp = farsite_dir / "barrier.shp"
    barrier_arg = _to_wine_path(barrier_shp) if barrier_shp.exists() else "0"

    cmd_line = " ".join([
        _to_wine_path(farsite_dir / "landscape.lcp"),
        _to_wine_path(farsite_dir / "farsite.input"),
        _to_wine_path(farsite_dir / "ignition.shp"),
        barrier_arg,
        _to_wine_path(outputs_dir / "farsite"),   # output base name (no extension)
        "2",                                       # output type: GeoTIFF
    ])

    wine_cmd_file = farsite_dir / "farsite_wine.txt"
    wine_cmd_file.write_text(cmd_line + "\n")

    # ---- Wine environment -----------------------------------------------
    env = os.environ.copy()
    env["GDAL_DATA"] = _to_wine_path(FB_BIN / "gdal-data")
    env["PROJ_LIB"]  = _to_wine_path(FB_BIN / "proj9" / "share")
    env["WINEDEBUG"] = "-all"   # suppress Wine internals noise

    # ---- run ------------------------------------------------------------
    print(f"  Running: wine {FARSITE_EXE.name} farsite_wine.txt")
    run_subprocess(
        ["wine", str(FARSITE_EXE), str(wine_cmd_file)],
        cwd=str(farsite_dir),
        env=env,
    )

    if sentinel.exists():
        print(f"  FARSITE complete — outputs in {outputs_dir}")
    else:
        raise RuntimeError(
            f"FARSITE finished but '{ARRIVAL_TIME_TIF}' was not created. "
            "Check the Wine output above for errors."
        )

    # ---- clean up outputs -----------------------------------------------
    # Always keep the arrival-time sentinel.
    # Keep WindGrids only when farsiteWindToGeotiff will need them later
    # (WINDNINJA_SOURCE == "farsite").  In "install" mode WindNinja already
    # produced ws.tif/wd.tif so WindGrids are not needed.
    keep = {ARRIVAL_TIME_TIF}
    if cfg.WINDNINJA_SOURCE == "farsite":
        keep.add(WIND_GRIDS_TIF)

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
