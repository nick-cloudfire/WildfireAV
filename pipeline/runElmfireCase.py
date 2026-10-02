#!/usr/bin/env python3
"""
Step "run_elmfire": run ELMFIRE on the case's ``*.data`` namelist.

Output (written by ELMFIRE): outputs/time_of_arrival_*.tif
"""

import time
from pathlib import Path

import pipelineConfig as cfg
from common import fmt_duration, for_each_case, skipped
from parallel_api import run_subprocess


def _toa(case_dir: Path) -> list[Path]:
    outputs = case_dir / cfg.ELMFIRE_OUTPUTS_SUBDIR
    return sorted(outputs.glob("time_of_arrival_*.tif")) if outputs.is_dir() else []


def run_elmfire(case_dir: Path):
    case_dir = Path(case_dir)
    if _toa(case_dir):
        return skipped("time_of_arrival output already exists")
    data_files = sorted(case_dir.glob("*.data"))
    if not data_files:
        raise FileNotFoundError(f"no *.data namelist in {case_dir} — run the elmfire_inputs step first")

    print(f"  Running: {cfg.ELMFIRE_EXE} {data_files[0].name}")
    t0 = time.monotonic()
    run_subprocess([cfg.ELMFIRE_EXE, data_files[0].name], cwd=case_dir)
    if not _toa(case_dir):
        raise RuntimeError(f"ELMFIRE exited 0 but wrote no time_of_arrival_*.tif in "
                           f"{case_dir / cfg.ELMFIRE_OUTPUTS_SUBDIR}")
    print(f"  ELMFIRE finished in {fmt_duration(time.monotonic() - t0)}: {_toa(case_dir)[0].name}")


def main(case_dir=None):
    return for_each_case(run_elmfire, case_dir)


if __name__ == "__main__":
    main()
