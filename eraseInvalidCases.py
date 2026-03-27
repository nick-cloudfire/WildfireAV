#!/usr/bin/env python3
"""
Step 4 of setupPipeline: remove cases whose satellite-derived duration is too short.

A case is kept only when:
  SatelliteEndTime - SatelliteIgnitionTime >= MIN_HOURS_DURATION hours
  (and both timestamps are present)

Invalid case folders are deleted from disk and the summary CSV is updated
in-place to remove those rows.

Input / output
--------------
- Reads and overwrites: FIRE_SUMMARY_SAT_CSV_PATH
- Deletes: FIRE_ROOT_LOGIN_NODE/<folder_name>/ for invalid cases
"""

from pathlib import Path
import shutil

import pandas as pd

import pipelineConfig as cfg

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIRE_ROOT           = Path(cfg.FIRE_ROOT_LOGIN_NODE)
INFO_CSV            = cfg.FIRE_SUMMARY_SAT_CSV_PATH     # full path
COL_FOLDER          = cfg.COL_FOLDER
COL_SAT_IGNITION    = cfg.COL_SATELLITE_IGNITION
COL_SAT_END         = cfg.COL_SATELLITE_END
COL_SAT_CHAIN_END   = cfg.COL_SAT_CHAIN_END_TIME
COL_POINT_DISCOVERY = cfg.COL_POINT_DISCOVERY
COL_POINT_FIREOUT   = cfg.COL_POINT_FIREOUT
EVENT_END_COL       = cfg.EVENT_END_COL
MIN_HOURS           = cfg.MIN_HOURS_DURATION


def _load_df() -> pd.DataFrame:
    parse_cols = [
        "perim_ignition",
        COL_POINT_DISCOVERY,
        COL_POINT_FIREOUT,
        COL_SAT_IGNITION,
        COL_SAT_CHAIN_END,
        COL_SAT_END,
        EVENT_END_COL,
    ]
    return pd.read_csv(INFO_CSV, parse_dates=[c for c in parse_cols if c])


def main() -> pd.DataFrame:
    if not INFO_CSV.exists():
        raise FileNotFoundError(f"Summary CSV not found: {INFO_CSV}")

    df = _load_df()
    if COL_FOLDER not in df.columns:
        raise KeyError(f"Missing column '{COL_FOLDER}' in {INFO_CSV}")

    df[COL_FOLDER] = df[COL_FOLDER].astype(str).str.zfill(5)

    keep_mask = []
    for _, row in df.iterrows():
        folder_name = row[COL_FOLDER]
        sat_ign = pd.to_datetime(row.get(COL_SAT_IGNITION), errors="coerce")
        sat_end = pd.to_datetime(row.get(COL_SAT_END),     errors="coerce")
        valid = (
            pd.notna(sat_ign)
            and pd.notna(sat_end)
            and (sat_end - sat_ign) >= pd.Timedelta(hours=MIN_HOURS)
        )
        keep_mask.append(valid)
        if not valid:
            folder = FIRE_ROOT / folder_name
            if folder.exists():
                print(f"  Removing invalid case: {folder}")
                shutil.rmtree(folder)

    filtered = df.loc[keep_mask].copy().reset_index(drop=True)
    filtered.to_csv(INFO_CSV, index=False)
    print(f"Kept {len(filtered)} valid cases out of {len(df)}.")
    return filtered


if __name__ == "__main__":
    main()
