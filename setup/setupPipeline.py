#!/usr/bin/env python3
"""
Local (login-node) pipeline setup driver.

Runs once before copying case folders to the HPC scratch directory.
Produces numbered case folders and the satellite-enriched summary CSV
that the HPC pipeline (runPipelineParallel.py) expects.

Steps
-----
1.  processScarsAndPoints        – match MTBS perimeters to USFS ignition points
2.  separateScarsAndPointsToCases – split into numbered case folders
3.  getSatelliteEndTimes         – compute satellite-based start/end times
4.  eraseInvalidCases            – remove cases shorter than MIN_HOURS_DURATION
5.  write_metadata_from_summary  – write case_metadata.json to each folder

Outputs (written to FIRE_ROOT_LOGIN_NODE)
-----------------------------------------
- perimeters_ignitions.gpkg
- all_ignitions.gpkg
- fire_pairs_summary.csv
- fire_pairs_summary_with_satellite.csv
- 00001/, 00002/, …  (case folders with firescar.gpkg, ignition_point.gpkg,
                      satellite_points.gpkg, case_metadata.json)

Run
---
    python setupPipeline.py           # normal run
    python setupPipeline.py --clean   # delete all generated output first
"""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import argparse
import shutil
import sys

import pipelineConfig as cfg
from case_metadata import write_metadata_from_summary
from parallel_api import Tee

FIRE_ROOT     = Path(cfg.FIRE_ROOT)
SUMMARY_CSV   = cfg.FIRE_SUMMARY_CSV_PATH
SAT_CSV       = cfg.FIRE_SUMMARY_SAT_CSV_PATH


def clean_generated() -> None:
    """Delete all generated case folders and summary CSVs under FIRE_ROOT."""
    print(f"\n*** CLEAN MODE: deleting generated output under {FIRE_ROOT} ***")
    if not FIRE_ROOT.exists():
        print(f"  FIRE_ROOT does not exist: {FIRE_ROOT}")
        return
    for child in FIRE_ROOT.iterdir():
        if child.is_dir() and child.name.isdigit() and len(child.name) == 5:
            print(f"  Removing {child.name}/")
            shutil.rmtree(child, ignore_errors=True)
    for path in (SUMMARY_CSV, SAT_CSV,
                 cfg.MTBS_PERIMS_WITH_IGNITIONS, cfg.USFS_POINTS_MATCHED):
        if path.exists():
            print(f"  Removing {path.name}")
            path.unlink()
    print("*** CLEAN COMPLETE ***\n")


def main() -> None:
    import processScarsAndPoints as step1
    print("\n=== STEP 1/5: process_scars_and_points ===")
    step1.main()

    import separateScarsAndPointsToCases as step2
    print("\n=== STEP 2/5: separate_scars_to_cases ===")
    step2.main()

    import getSatelliteEndTimes as step3
    print("\n=== STEP 3/5: get_satellite_end_times ===")
    step3.main()

    import eraseInvalidCases as step4
    print("\n=== STEP 4/5: erase_invalid_cases ===")
    step4.main()

    print("\n=== STEP 5/5: write_case_metadata ===")
    count = write_metadata_from_summary(SAT_CSV, FIRE_ROOT)
    print(f"Wrote case metadata for {count} cases.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local pipeline setup.")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete all generated case folders and intermediate files before running.",
    )
    args = parser.parse_args()

    if args.clean:
        clean_generated()

    LOG_FILE = FIRE_ROOT / "pipeline_setup.log"
    FIRE_ROOT.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as log:
        tee = Tee(sys.stdout, log)
        with redirect_stdout(tee), redirect_stderr(tee):
            main()
