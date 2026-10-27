#!/usr/bin/env python3
"""
Delete what the per-case pipeline produced, from one step onwards, so cases
re-run cleanly.  Step names are those of the pipeline
(``python pipeline/runPipelineParallel.py --list-steps``).

By default everything after the LANDFIRE download is removed (LANDFIRE.tif,
case_metadata.json and the fire geometry are kept).  ws.tif / wd.tif are kept
unless --include-wind-tifs is given: WindNinja is the slowest step.

    python cleanPipelineOutputs.py --dry-run                       # show what would go
    python cleanPipelineOutputs.py                                 # everything after LANDFIRE.tif
    python cleanPipelineOutputs.py --from-step run_elmfire         # only the fire model outputs
    python cleanPipelineOutputs.py --from-step windninja --include-wind-tifs --cases 13

The shared HRRR cache (FIRE_ROOT/hrrr_cache) is never touched; delete it by hand.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pipelineConfig as cfg
from case_metadata import case_dirs

INPUTS = cfg.INPUTS_SUBDIR_NAME
WIND_TIFS = [f"{INPUTS}/{cfg.WS_TIF_NAME}", f"{INPUTS}/{cfg.WD_TIF_NAME}"]

# Pipeline step -> what it writes, relative to the case folder (globs allowed), in pipeline order.
OUTPUTS: dict[str, list[str]] = {
    "landfire":        ["LANDFIRE.tif", "LANDFIRE.tfw"],
    "split_bands":     [f"{INPUTS}/{name}.tif" for name in cfg.LANDFIRE_BAND_FILE_NAMES],
    "adj_phi":         [f"{INPUTS}/{cfg.ADJ_FILE_NAME}", f"{INPUTS}/{cfg.PHI_FILE_NAME}"],
    "weather":         [f"{INPUTS}/{cfg.WXS_FILE_NAME}"],
    "live_moisture":   [f"{INPUTS}/live_fuel_moisture.json"],
    "hrrr":            [f"{INPUTS}/{cfg.WINDNINJA_SUBDIR}/{cfg.HRRR_LOCAL_SUBDIR}"],
    "windninja":       [f"{INPUTS}/{cfg.WINDNINJA_SUBDIR}"],
    "nelson":          [f"{INPUTS}/{name}.tif" for name in cfg.FMC_FILE_NAMES],
    "barrier":         [f"{INPUTS}/{cfg.BARRIER_FILE_NAME}", f"{INPUTS}/{cfg.BARRIER_ROADS_CLIP_NAME}",
                        f"{INPUTS}/{cfg.BARRIER_WATER_CLIP_NAME}", f"{INPUTS}/{cfg.BARRIER_BACKUP_CLIP_NAME}"],
    "elmfire_inputs":  ["*.data"],
    "prepare_farsite": ["farsite"],
    "run_elmfire":     [cfg.ELMFIRE_OUTPUTS_SUBDIR, cfg.ELMFIRE_SCRATCH_SUBDIR],
    "run_farsite":     ["farsite/outputs"],
}
STEPS = list(OUTPUTS)
DEFAULT_FROM = "split_bands"


def targets(case_dir: Path, from_step: str = DEFAULT_FROM, include_wind_tifs: bool = False) -> list[Path]:
    """Existing paths written by from_step and every later step, plus the case log/status."""
    patterns = [p for step in STEPS[STEPS.index(from_step):] for p in OUTPUTS[step]]
    patterns += ["pipeline.log", "status.json"] + (WIND_TIFS if include_wind_tifs else [])
    return sorted({hit for p in patterns for hit in case_dir.glob(p)})


def delete(path: Path, dry_run: bool = False) -> None:
    if not dry_run:
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)


def clean(folders: list[Path], from_step: str = DEFAULT_FROM, include_wind_tifs: bool = False,
          dry_run: bool = False, verbose: bool = True) -> int:
    """Delete the outputs of from_step onwards in every folder; returns the number of items."""
    total = 0
    for folder in folders:
        found = targets(folder, from_step, include_wind_tifs)
        for path in found:
            if verbose:
                print(f"  {'[dry run] ' if dry_run else ''}delete {path}{'/' if path.is_dir() else ''}")
            delete(path, dry_run)
        total += len(found)
    return total


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--case-root", default=str(cfg.FIRE_ROOT), help="folder with the numbered case folders")
    p.add_argument("--from-step", choices=STEPS, default=DEFAULT_FROM, metavar="STEP",
                   help=f"delete this step's outputs and every later step's ({', '.join(STEPS)})")
    p.add_argument("--cases", nargs="+", metavar="ID", help="only these cases (leading zeros optional)")
    p.add_argument("--include-wind-tifs", action="store_true", help="also delete inputs/ws.tif and wd.tif")
    p.add_argument("--dry-run", action="store_true", help="only print what would be deleted")
    args = p.parse_args()

    folders = case_dirs(Path(args.case_root), args.cases)
    if not folders:
        sys.exit(f"No case folders found under {args.case_root}")
    n = clean(folders, args.from_step, args.include_wind_tifs, args.dry_run)
    print(f"\n{'Would remove' if args.dry_run else 'Removed'} {n} item(s) in {len(folders)} case(s).")


if __name__ == "__main__":
    main()
