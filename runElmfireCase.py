#!/usr/bin/env python3
"""
Step 9 of runPipelineParallel: execute the Elmfire fire-spread simulation.

Finds the ``*.data`` namelist file in the case directory and runs the Elmfire
executable (configurable via ``pipelineConfig.ELMFIRE_EXE``).

Outputs (written by Elmfire itself)
------------------------------------
- outputs/time_of_arrival_<HHHH>.tif
"""

from pathlib import Path

import pipelineConfig as cfg
from parallel_api import run_subprocess

FIRE_ROOT  = cfg.FIRE_ROOT
ELMFIRE_EXE = cfg.ELMFIRE_EXE


def run_elmfire(case_dir: Path) -> None:
    """Run Elmfire on the *.data file found in *case_dir*."""
    case_dir  = Path(case_dir)
    outputs   = case_dir / cfg.ELMFIRE_OUTPUTS_SUBDIR
    if outputs.is_dir() and any(outputs.glob("time_of_arrival_*.tif")):
        print(f"  Skipped — time_of_arrival output already exists.")
        return
    data_files = list(case_dir.glob("*.data"))
    if not data_files:
        raise FileNotFoundError(f"No *.data namelist file found in {case_dir}")
    data_file = data_files[0]

    print(f"  Running: {ELMFIRE_EXE} {data_file.name}")
    run_subprocess(
        [ELMFIRE_EXE, data_file.name],
        cwd=case_dir,
    )


def main(case_dir=None) -> None:
    if case_dir is not None:
        run_elmfire(Path(case_dir))
        return

    root = Path(FIRE_ROOT)
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and folder.name.isdigit():
            print(f"\nFolder {folder.name}:")
            run_elmfire(folder)


if __name__ == "__main__":
    main()
