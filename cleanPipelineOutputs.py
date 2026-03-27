#!/usr/bin/env python3
"""
Delete generated pipeline files so cases can be cleanly re-run.

By default all outputs AFTER prefetchLandfire are removed (LANDFIRE.tif is kept).
Use --from-step to remove only a specific step and everything after it.

Steps (in pipeline order)
--------------------------
  landfire_bands   inputs/{dem,slp,asp,fbfm40,cc,ch,cbh,cbd}.tif
  phi_adj          inputs/phi.tif  inputs/adj.tif
  weather          inputs/weather.wxs
  windninja        inputs/windninja/
  nelson           inputs/m1.tif  inputs/m10.tif  inputs/m100.tif
  barrier          inputs/barrier.tif
  elmfire_inputs   <case>/*.data
  elmfire_outputs  outputs/  scratch/

pipeline.log is always removed when any step is cleaned.

Usage
-----
  # Full reset (everything after LANDFIRE):
  python cleanPipelineOutputs.py

  # Reset only from WindNinja onwards:
  python cleanPipelineOutputs.py --from-step windninja

  # Dry run — show what would be deleted without deleting:
  python cleanPipelineOutputs.py --dry-run

  # Specific cases only:
  python cleanPipelineOutputs.py --cases 00001 00003
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pipelineConfig as cfg
from case_metadata import case_dirs

# ---------------------------------------------------------------------------
# Step definitions (ordered)
# ---------------------------------------------------------------------------

INPUTS = cfg.INPUTS_SUBDIR_NAME

# Each step: (name, list of paths relative to case_dir, list of glob patterns)
# Paths that are directories will be removed with shutil.rmtree.
_STEPS: list[tuple[str, list[str], list[str]]] = [
    (
        "landfire_bands",
        [f"{INPUTS}/{name}.tif" for name in cfg.LANDFIRE_BAND_FILE_NAMES],
        [],
    ),
    (
        "phi_adj",
        [f"{INPUTS}/{cfg.PHI_FILE_NAME}", f"{INPUTS}/{cfg.ADJ_FILE_NAME}"],
        [],
    ),
    (
        "weather",
        [f"{INPUTS}/{cfg.WXS_FILE_NAME}"],
        [],
    ),
    (
        "windninja",
        [f"{INPUTS}/{cfg.WINDNINJA_SUBDIR}"],  # directory
        [],
    ),
    (
        "nelson",
        [f"{INPUTS}/{name}.tif" for name in cfg.FMC_FILE_NAMES],
        [],
    ),
    (
        "barrier",
        [f"{INPUTS}/{cfg.BARRIER_FILE_NAME}"],
        [],
    ),
    (
        "elmfire_inputs",
        [],
        ["*.data"],  # glob in case root
    ),
    (
        "elmfire_outputs",
        [cfg.ELMFIRE_OUTPUTS_SUBDIR, cfg.ELMFIRE_SCRATCH_SUBDIR],
        [],
    ),
]

STEP_NAMES = [name for name, _, _ in _STEPS]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _targets_for_case(case_dir: Path, steps: list[tuple]) -> list[Path]:
    """Return all paths (files + dirs) that should be deleted for this case."""
    targets: list[Path] = []
    for _, rel_paths, globs in steps:
        for rel in rel_paths:
            targets.append(case_dir / rel)
        for pattern in globs:
            targets.extend(case_dir.glob(pattern))
    targets.append(case_dir / "pipeline.log")
    return targets


def _delete(path: Path, dry_run: bool) -> bool:
    """Delete a file or directory. Returns True if something was removed."""
    if not path.exists():
        return False
    label = f"  {'[DRY RUN] ' if dry_run else ''}delete  {path}"
    if path.is_dir():
        print(label + "/")
        if not dry_run:
            shutil.rmtree(path)
    else:
        print(label)
        if not dry_run:
            path.unlink()
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete pipeline outputs so cases can be re-run.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--case-root",
        default=str(cfg.FIRE_ROOT),
        help="Root directory containing numbered case folders.",
    )
    parser.add_argument(
        "--from-step",
        metavar="STEP",
        choices=STEP_NAMES,
        default=STEP_NAMES[0],
        help=(
            "Delete this step and all later steps. "
            f"Choices: {', '.join(STEP_NAMES)}"
        ),
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        metavar="FOLDER",
        help="Clean only these specific case folders (default: all).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without actually deleting.",
    )
    args = parser.parse_args()

    case_root = Path(args.case_root)
    if not case_root.is_dir():
        print(f"ERROR: case root not found: {case_root}", file=sys.stderr)
        sys.exit(1)

    if args.cases:
        folders = [case_root / name for name in args.cases]
    else:
        folders = case_dirs(case_root)

    if not folders:
        print("No case folders found.")
        return

    # Slice steps from the requested starting point onwards
    from_idx     = STEP_NAMES.index(args.from_step)
    active_steps = _STEPS[from_idx:]

    print(
        f"{'DRY RUN — ' if args.dry_run else ''}"
        f"Cleaning {len(folders)} case(s) from step '{args.from_step}' onwards\n"
        f"Steps: {', '.join(s[0] for s in active_steps)}\n"
    )

    total_removed = 0
    for folder in folders:
        if not folder.is_dir():
            print(f"  WARNING: {folder} not found, skipping.")
            continue

        targets  = _targets_for_case(folder, active_steps)
        removed  = sum(_delete(t, args.dry_run) for t in targets)
        if removed:
            total_removed += removed
            print(f"  [{folder.name}]  {removed} item(s) removed")

    action = "would remove" if args.dry_run else "removed"
    print(f"\nDone — {action} {total_removed} item(s) across {len(folders)} case(s).")


if __name__ == "__main__":
    main()
