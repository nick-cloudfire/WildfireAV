#!/usr/bin/env python3
"""
Check that every case's hourly inputs share one clock: band 1 of ws.tif (wind)
and m1.tif (dead fuel moisture) must both be valid at the ignition hour,
floor(SatelliteIgnitionTime).  Uses the band times the pipeline records.

    python tools/checkHourlyInputs.py              # all cases
    python tools/checkHourlyInputs.py 4 12         # these cases
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "pipeline")]

import pipelineConfig as cfg
from case_metadata import case_dirs, read_case_metadata
from common import ignition_hour, read_band_times


def _check(case_dir: Path) -> tuple[str, str]:
    try:
        first = ignition_hour(read_case_metadata(case_dir))
    except (OSError, ValueError) as e:
        return "-", f"no ignition time ({e})"
    problems, notes = [], []
    for label, name, step in (("ws", cfg.WS_TIF_NAME, "windninja"), ("m1", f"{cfg.FMC_FILE_NAMES[0]}.tif", "nelson")):
        tif = case_dir / cfg.INPUTS_SUBDIR_NAME / name
        if not tif.exists():
            notes.append(f"{label} not made yet")
            continue
        times = read_band_times(tif)
        if times is None:
            problems.append(f"{label} has no band times (re-run from {step})")
        elif times[0] != first:
            problems.append(f"{label} starts {times[0]:%m-%d %H:%M} (re-run from {step})")
        else:
            notes.append(f"{label} {len(times)} h")
    return f"{first:%Y-%m-%d %H:%M}", "; ".join(problems) or "OK  " + ", ".join(notes)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("cases", nargs="*", help="case ids (leading zeros optional); default all")
    args = p.parse_args()
    rows = [(d.name, *_check(d)) for d in case_dirs(cfg.FIRE_ROOT, args.cases or None)]
    print(f"{'CASE':<7} {'IGNITION HOUR':<17} STATUS")
    for case, first, status in rows:
        print(f"{case:<7} {first:<17} {status}")
    bad = sum(not s.startswith("OK") for _, _, s in rows)
    print(f"\n{len(rows) - bad} OK, {bad} need attention")


if __name__ == "__main__":
    main()
