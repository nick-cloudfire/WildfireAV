#!/usr/bin/env python3
"""
HPC per-case pipeline driver.

Runs all simulation-preparation and fire-model execution steps for one or more
case directories.  Each case directory must already contain the files produced
by ``setupPipeline.py`` (firescar.gpkg, ignition_point.gpkg, case_metadata.json).

Usage
-----
Single case::

    python runPipelineParallel.py /path/to/00001

All cases under FIRE_ROOT (defined in pipelineConfig)::

    python runPipelineParallel.py --case-root /scratch/nick/FirePairs

Per-case pipeline steps
-----------------------
Steps 1–8 are always executed in both wind-source modes.

  1.  getLandfireProductsForFireSim  – download LANDFIRE rasters
  2.  splitLandfireTifBands          – split multi-band LANDFIRE.tif
  3.  makePhiAndAdjFiles             – create adjacency / phi rasters
  4.  downloadWeatherData            – fetch ERA5 weather from OpenMeteo
  5a. [install mode] downloadAndRunWindninja – run WindNinja wind-field model
  5b. [farsite mode] SKIP WindNinja
  6.  applyNelsonModel               – compute dead-fuel moisture (Nelson C#)
  7.  getBarrierFile                 – rasterise roads / waterways as barriers
  8.  createElmfireInputFiles        – write Elmfire namelist (.data file)

  [install mode]  steps 9–11:
    9.  prepareFarsite               – prepare FARSITE inputs
   10.  runElmfireCase               – execute Elmfire
   11.  runFarsiteCase               – execute FARSITE via Wine

  [farsite mode]  steps 9–12:
    9.  prepareFarsite               – prepare FARSITE inputs
   10.  runFarsiteCase               – execute FARSITE via Wine (for wind fields)
   11.  farsiteWindToGeotiff         – extract ws.tif / wd.tif from FARSITE wind grids
   12.  runElmfireCase               – execute Elmfire (using FARSITE-derived winds)

WINDNINJA_SOURCE is set in pipelineConfig:
  "install"  → WindNinja is the wind source (default)
  "farsite"  → FARSITE provides wind grids; WindNinja is skipped
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
    """Run all pipeline steps for one case."""
    case_dir = Path(case_dir).absolute()
    log_file = case_dir / "pipeline.log"
    wind_source = pipelineConfig.WINDNINJA_SOURCE   # "install" | "farsite"

    with open(log_file, "w", encoding="utf-8", buffering=1) as log:
        out = log if log_only else Tee(sys.stdout, log)
        with redirect_stdout(out), redirect_stderr(out):
            print(f"=== CASE: {case_dir.name}  [wind_source={wind_source}] ===")

            import getLandfireProductsForFireSim as landfire
            print("\n=== STEP 1: download_landfire ===")
            landfire.main(case_dir)

            import splitLandfireTifBands as split_lf
            print("\n=== STEP 2: split_landfire_bands ===")
            split_lf.main(case_dir)

            import makePhiAndAdjFiles as adjphi
            print("\n=== STEP 3: make_adj_phi ===")
            adjphi.main(case_dir)

            import downloadWeatherData as weather
            print("\n=== STEP 4: download_weather_wxs ===")
            weather.main(case_dir)

            if wind_source == "install":
                if pipelineConfig.WINDNINJA_MODE == "wxModel":
                    import downloadAndRunWindninja_wxModel as wn
                else:
                    import downloadAndRunWindninja_WXS as wn
                print(f"\n=== STEP 5: windninja ({pipelineConfig.WINDNINJA_MODE}) ===")
                wn.main(case_dir)
            else:
                print("\n=== STEP 5: windninja SKIPPED (wind_source=farsite) ===")

            import applyNelsonModel as nelson
            print("\n=== STEP 6: apply_nelson_model ===")
            nelson.main(case_dir)

            import getBarrierFile as barrier
            print("\n=== STEP 7: create_barrier_file ===")
            barrier.main(case_dir)

            import createElmfireInputFiles as elm
            print("\n=== STEP 8: create_elmfire_input_files ===")
            elm.main(case_dir)

            import prepareFarsite as farsite_prep
            print("\n=== STEP 9: prepare_farsite ===")
            farsite_prep.main(case_dir)

            import runFarsiteCase as run_farsite
            import runElmfireCase as run_elm

            if wind_source == "farsite":
                print("\n=== STEP 10: run_farsite (wind source) ===")
                run_farsite.main(case_dir)

                import farsiteWindToGeotiff as farsite_wind
                print("\n=== STEP 11: farsite_wind_to_geotiff ===")
                farsite_wind.main(case_dir)

                print("\n=== STEP 12: run_elmfire ===")
                run_elm.main(case_dir)
            else:
                print("\n=== STEP 10: run_elmfire ===")
                run_elm.main(case_dir)

                print("\n=== STEP 11: run_farsite ===")
                run_farsite.main(case_dir)

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
