#!/usr/bin/env python3
"""
HPC per-case pipeline driver.

Runs all simulation-preparation and Elmfire-execution steps for one or more
case directories.  Each case directory must already contain the files produced
by ``setupPipeline.py`` (firescar.gpkg, ignition_point.gpkg, case_metadata.json).

Usage
-----
Single case::

    python runPipelineParallel.py /path/to/00001

All cases under FIRE_ROOT (defined in pipelineConfig)::

    python runPipelineParallel.py --case-root /scratch/nick/FirePairs

Per-case pipeline steps (executed sequentially within a case)
-------------------------------------------------------------
1.  getLandfireProductsForFireSim  – download LANDFIRE rasters
2.  splitLandfireTifBands          – split multi-band LANDFIRE.tif
3.  makePhiAndAdjFiles             – create adjacency / phi rasters
4.  downloadWeatherData            – fetch ERA5 weather from OpenMeteo
5.  downloadAndRunWindninja        – run WindNinja wind-field model
6.  applyNelsonModel               – compute dead-fuel moisture (Nelson C#)
7.  getBarrierFile                 – rasterise roads / waterways as barriers
8.  createElmfireInputFiles        – write Elmfire namelist (.data file)
9.  runElmfireCase                 – execute Elmfire
"""

from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import argparse
import sys

import pipelineConfig
from case_metadata import case_dirs
from parallel_api import Tee


def process_case(case_dir: Path, log_only: bool = False) -> None:
    """Run all pipeline steps for one case.

    Parameters
    ----------
    case_dir:
        Path to the case directory (e.g. .../FirePairs/00001).
    log_only:
        When True, all output goes only to pipeline.log and is suppressed from
        the calling process's stdout.  Use this when running under runBatch.py
        so that parallel cases do not interleave their output on the console.
    """
    case_dir = Path(case_dir).absolute()
    log_file = case_dir / "pipeline.log"
    with open(log_file, "w", encoding="utf-8", buffering=1) as log:
        out = log if log_only else Tee(sys.stdout, log)
        with redirect_stdout(out), redirect_stderr(out):
            print(f"=== CASE: {case_dir.name} ===")

            import getLandfireProductsForFireSim as landfire
            print("\n=== STEP 1/9: download_landfire ===")
            landfire.main(case_dir)

            import splitLandfireTifBands as split_lf
            print("\n=== STEP 2/9: split_landfire_bands ===")
            split_lf.main(case_dir)

            import makePhiAndAdjFiles as adjphi
            print("\n=== STEP 3/9: make_adj_phi ===")
            adjphi.main(case_dir)

            import downloadWeatherData as weather
            print("\n=== STEP 4/9: download_weather_wxs ===")
            weather.main(case_dir)

            if pipelineConfig.WINDNINJA_MODE == "wxModel":
                import downloadAndRunWindninja_wxModel as wn
            else:
                import downloadAndRunWindninja_WXS as wn
            print(f"\n=== STEP 5/9: windninja ({pipelineConfig.WINDNINJA_MODE}) ===")
            wn.main(case_dir)

            import applyNelsonModel as nelson
            print("\n=== STEP 6/9: apply_nelson_model ===")
            nelson.main(case_dir)

            import getBarrierFile as barrier
            print("\n=== STEP 7/9: create_barrier_file ===")
            barrier.main(case_dir)

            import createElmfireInputFiles as elm
            print("\n=== STEP 8/9: create_elmfire_input_files ===")
            elm.main(case_dir)

            import runElmfireCase as run_elm
            print("\n=== STEP 9/9: run_elmfire ===")
            run_elm.main(case_dir)

            print("\nCASE COMPLETED.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the per-case HPC pipeline for one or all cases.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "case_dir",
        nargs="?",
        help="Path to a single case directory (e.g. /scratch/nick/FirePairs/00001).",
    )
    parser.add_argument(
        "--case-root",
        default=str(pipelineConfig.FIRE_ROOT),
        help="Root directory containing numbered case folders (run all cases).",
    )
    return parser.parse_args()


def main(case_dir: Path | None = None) -> None:
    if case_dir is not None:
        process_case(Path(case_dir))
        return

    args = parse_args()
    if args.case_dir:
        process_case(Path(args.case_dir))
        return

    root = Path(args.case_root)
    for folder in case_dirs(root):
        process_case(folder)


if __name__ == "__main__":
    main()
