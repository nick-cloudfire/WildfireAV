"""
Step 3 of setupPipeline: compute SatelliteIgnitionTime and SatelliteEndTime
for each fire case using satellite hotspot data.

Algorithm per case (see satellite.py for the coverage curve)
------------------
1. Load the burn polygon (firescar.gpkg) in EPSG:5070 and keep the hotspots
   inside it detected after point_discovery - 1 day.
2. SatelliteIgnitionTime = when coverage first reaches 5 % of the effective
   burn area.
3. SatelliteEnd_chain    = last detection of the unbroken chain that starts
   at SatelliteIgnitionTime.
4. SatelliteEndTime (= SatelliteEnd_coverage) = when coverage, counted from
   SatelliteIgnitionTime, first reaches COVERAGE_FRACTION.
5. EventEndTime = min(SatelliteEndTime, point_fireout).

Outputs
-------
- fire_pairs_summary_with_satellite.csv  (updated master CSV)
- <case_dir>/satellite_points.gpkg       (per-case hotspot points inside burn)

The satellite layer is loaded and indexed once; cases run in parallel
threads (SETUP_PIPELINE_MAX_WORKERS) that only read it.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype

import pipelineConfig
import satellite


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIRE_ROOT           = Path(pipelineConfig.FIRE_ROOT)
MASTER_CSV          = pipelineConfig.FIRE_SUMMARY_CSV_PATH      # full path under FIRE_ROOT
OUTPUT_CSV          = pipelineConfig.FIRE_SUMMARY_SAT_CSV_PATH  # full path under FIRE_ROOT

FOLDER_COL          = pipelineConfig.COL_FOLDER
IGNITION_COL        = pipelineConfig.COL_IGNITION_TIME
SAT_IGNITION_COL    = pipelineConfig.COL_SATELLITE_IGNITION
SAT_END_COL         = pipelineConfig.COL_SATELLITE_END
FIREOUT_COL         = pipelineConfig.COL_POINT_FIREOUT
EVENT_END_COL       = pipelineConfig.EVENT_END_COL
COL_SAT_CHAIN_END   = pipelineConfig.COL_SAT_CHAIN_END_TIME
COL_SAT_END_AREA    = pipelineConfig.COL_SAT_END_AREA

SATELLITE_GPKG      = pipelineConfig.SATELLITE_GPKG
SATELLITE_LAYER     = pipelineConfig.SATELLITE_LAYER_NAME
BURN_SHAPE_NAME     = pipelineConfig.BURN_SHAPE_NAME
CASE_SAT_GPKG_NAME  = pipelineConfig.CASE_SAT_GPKG_NAME

COVERAGE_FRACTION   = pipelineConfig.COVERAGE_FRACTION
IGNITION_FRACTION   = 0.05      # coverage fraction that marks the satellite ignition time

MAX_WORKERS         = pipelineConfig.SETUP_PIPELINE_MAX_WORKERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_to_gpkg(gdf: gpd.GeoDataFrame, out_path: Path, layer: str) -> None:
    """Write a GeoDataFrame to a GeoPackage layer, sanitising column types."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    g = gdf[~gdf.geometry.isna() & ~gdf.geometry.is_empty].copy()
    if g.empty:
        return

    # Sanitise datetime columns for GPKG compatibility
    geom_col = g.geometry.name
    for c in list(g.columns):
        if c == geom_col:
            continue
        if is_datetime64_any_dtype(g[c]):
            s = pd.to_datetime(g[c], errors="coerce")
            if s.dt.tz is not None:
                s = s.dt.tz_convert(None)
            g[c] = s.astype("datetime64[ns]")
        # Drop columns with non-scalar objects (lists, dicts, etc.)
        if g[c].dtype == "object":
            sample = g[c].dropna().head(10).tolist()
            if any(isinstance(v, (list, dict, tuple, set)) for v in sample):
                g = g.drop(columns=[c])

    tmp = out_path.with_name(f".{out_path.stem}.partial.gpkg")   # never leave a half-written gpkg
    tmp.unlink(missing_ok=True)
    g.to_file(tmp, layer=layer, driver="GPKG", engine="pyogrio", index=False)
    os.replace(tmp, out_path)


# ---------------------------------------------------------------------------
# Per-case worker (called from thread pool)
# ---------------------------------------------------------------------------

def _fmt_dt(ts) -> str:
    """Format a timestamp as 'YYYY-MM-DD HH:MM', or '—' if NaT."""
    try:
        return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _process_case(
    idx: int,
    row: pd.Series,
    sat_gdf: gpd.GeoDataFrame,
    sat_sindex,
) -> tuple[dict | None, str]:
    """
    Compute satellite times for one case row.

    Returns (updates, status_str) where:
      updates    – dict {column: value} to apply to master, or None to skip
      status_str – compact one-line summary for console output
    sat_gdf and sat_sindex are read-only; thread-safe.
    """
    folder_id   = int(row[FOLDER_COL])
    folder_name = f"{folder_id:05d}"
    case_folder = FIRE_ROOT / folder_name
    burn_path   = case_folder / BURN_SHAPE_NAME

    if not case_folder.exists():
        return None, "SKIP  case folder missing"
    if not burn_path.exists():
        return None, "SKIP  burn shapefile missing"

    ignition_dt = pd.to_datetime(row[IGNITION_COL], errors="raise")

    burn_gdf = gpd.read_file(burn_path)
    if burn_gdf.empty:
        return None, "SKIP  burn polygon empty"
    if burn_gdf.crs is None:
        burn_gdf = burn_gdf.set_crs("EPSG:4326")
    burn_gdf  = burn_gdf.to_crs(satellite.AREA_CRS)
    burn_geom = burn_gdf.union_all()

    # Bounding-box + time pre-filter
    candidate_idx = list(sat_sindex.intersection(burn_geom.bounds))
    if not candidate_idx:
        return None, "SKIP  no satellite candidates in bbox"
    candidates = sat_gdf.iloc[candidate_idx]
    candidates = candidates[candidates["sat_datetime"] >= ignition_dt]
    if candidates.empty:
        return None, "SKIP  no satellite points after ignition"

    pts_in_burn = candidates[candidates.geometry.within(burn_geom)]
    if pts_in_burn.empty:
        return None, "SKIP  no satellite points inside burn"

    # --- Satellite ignition time: 5 % coverage ---
    sat_ignition_dt = satellite.coverage_curve(
        pts_in_burn, burn_geom, ignition_dt - pd.Timedelta(days=1)).time_to_fraction(IGNITION_FRACTION)
    if pd.isna(sat_ignition_dt):
        return None, "SKIP  no sat ignition (5% coverage threshold)"
    updates = {SAT_IGNITION_COL: sat_ignition_dt}

    # Save per-case satellite points (each case writes to its own folder — no collision)
    pts_export = pts_in_burn.copy()
    pts_export["folder_name"]     = folder_name
    pts_export["ignition_dt"]     = ignition_dt
    pts_export["sat_ignition_dt"] = sat_ignition_dt
    _safe_to_gpkg(pts_export, case_folder / CASE_SAT_GPKG_NAME, layer="points_in_burn")

    # --- Chain end and coverage end, counted from the satellite ignition ---
    chain = satellite.first_chain(pts_in_burn, sat_ignition_dt)
    updates[COL_SAT_CHAIN_END] = chain["sat_datetime"].max()
    coverage_end = satellite.coverage_curve(pts_in_burn, burn_geom, sat_ignition_dt
                                            ).time_to_fraction(COVERAGE_FRACTION)
    updates[COL_SAT_END_AREA] = coverage_end
    if pd.isna(coverage_end):
        return updates, f"PARTIAL  sat={_fmt_dt(sat_ignition_dt)}  no coverage end"

    end_time = pd.to_datetime(coverage_end)
    updates[SAT_END_COL] = end_time

    # --- Event end = min(satellite end, point fireout) ---
    fireout_dt = pd.to_datetime(row[FIREOUT_COL], errors="coerce")
    if pd.isna(fireout_dt):
        final_end = end_time
    else:
        if fireout_dt.tzinfo is not None:
            fireout_dt = fireout_dt.tz_convert("UTC").tz_localize(None)
        if end_time.tzinfo is not None:
            end_time = end_time.tz_convert("UTC").tz_localize(None)
        final_end = min(end_time, fireout_dt)

    updates[EVENT_END_COL] = final_end

    duration_d = (final_end - sat_ignition_dt).total_seconds() / 86400
    status = (
        f"OK    {_fmt_dt(sat_ignition_dt)} → {_fmt_dt(final_end)}"
        f"  ({duration_d:.1f}d)"
    )
    return updates, status


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Loading satellite points …")
    # Only detections that can belong to a case: from a month before the first fire year on
    first_day = f"{pipelineConfig.MIN_FIRE_YEAR - 1}-12-01"
    sat_gdf = gpd.read_file(SATELLITE_GPKG, layer=SATELLITE_LAYER,
                            columns=[pipelineConfig.SAT_DATE_COL, pipelineConfig.SAT_TIME_COL],
                            where=f"{pipelineConfig.SAT_DATE_COL} >= '{first_day}'").to_crs(satellite.AREA_CRS)
    sat_gdf["sat_datetime"] = satellite.detection_times(sat_gdf)
    sat_gdf = sat_gdf.dropna(subset=["sat_datetime"])
    sat_sindex = sat_gdf.sindex
    print(f"Loaded {len(sat_gdf)} satellite points.\n")

    master = pd.read_csv(MASTER_CSV)
    for col in (FOLDER_COL, IGNITION_COL, FIREOUT_COL):
        if col not in master.columns:
            raise KeyError(f"Column '{col}' not found in {MASTER_CSV}")

    # Initialise output columns
    for col in (SAT_IGNITION_COL, COL_SAT_CHAIN_END, COL_SAT_END_AREA,
                SAT_END_COL, EVENT_END_COL):
        master[col] = pd.NaT

    n = len(master)
    w = len(str(n))   # digit width for progress counter
    print(f"Processing {n} cases with up to {MAX_WORKERS} parallel workers …\n")

    # Run per-case processing in parallel; sat_gdf/sat_sindex are read-only.
    case_results: dict[int, dict] = {}
    _print_lock = threading.Lock()

    def _worker(item: tuple[int, pd.Series]) -> tuple[int, dict | None, str]:
        idx, row = item
        updates, status = _process_case(idx, row, sat_gdf, sat_sindex)
        return idx, updates, status

    done = 0
    n_ok = n_skip = n_partial = n_err = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_worker, (idx, row)): idx
                for idx, row in master.iterrows()}
        for fut in as_completed(futs):
            idx = futs[fut]
            done += 1
            folder_id = master.at[idx, FOLDER_COL]
            try:
                _, updates, status = fut.result()
                if updates:
                    case_results[idx] = updates
                tag = status.split()[0]
                if tag == "OK":      n_ok      += 1
                elif tag == "SKIP":  n_skip    += 1
                else:                n_partial += 1
            except Exception as e:
                status = f"ERROR  {type(e).__name__}: {e}"
                n_err += 1
            with _print_lock:
                print(f"[{done:{w}d}/{n}] {folder_id}  {status}")

    # Apply all results to master in the main thread (no concurrent writes)
    for idx, updates in case_results.items():
        for col, val in updates.items():
            master.at[idx, col] = val

    print(
        f"\nDone — {n_ok} OK, {n_skip} skipped"
        + (f", {n_partial} partial" if n_partial else "")
        + (f", {n_err} errors" if n_err else "")
    )
    print(f"Saving updated master to {OUTPUT_CSV} …")
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_CSV.with_name(f".{OUTPUT_CSV.name}.partial")
    master.to_csv(tmp, index=False)
    os.replace(tmp, OUTPUT_CSV)


if __name__ == "__main__":
    main()
