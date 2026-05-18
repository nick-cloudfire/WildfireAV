# getSatelliteEndTimes.py
"""
Step 3 of setupPipeline: compute SatelliteIgnitionTime and SatelliteEndTime
for each fire case using satellite hotspot data.

Algorithm per case
------------------
1. Load burn polygon (firescar.gpkg) and reproject to EPSG:5070.
2. Spatially filter satellite hotspot points to those inside the burn.
3. SatelliteIgnitionTime = earliest time to reach 5% cumulative hotspot
   coverage of the effective burn area (starting from point_discovery).
4. SatelliteEnd_chain = last timestamp in the first unbroken chain of
   detections (gap <= SAT_CHAIN_MAX_GAP_DAYS) after SatelliteIgnitionTime.
5. SatelliteEnd_coverage = earliest time to reach COVERAGE_FRACTION of
   effective burn area after SatelliteIgnitionTime.
6. SatelliteEndTime = SatelliteEnd_coverage.
7. EventEndTime = min(SatelliteEndTime, point_fireout).

Outputs
-------
- fire_pairs_summary_with_satellite.csv  (updated master CSV)
- <case_dir>/satellite_points.gpkg       (per-case hotspot points inside burn)

Performance notes
-----------------
- Satellite points are reprojected to EPSG:5070 ONCE and a spatial index
  is built ONCE across all cases.
- Bounding-box pre-filter plus time filter before the expensive within() check.
- Coverage computation uses batched unary_union to avoid repeated per-point unions.
- Per-case processing runs in parallel (up to SETUP_PIPELINE_MAX_WORKERS threads).
  The shared sat_gdf / sat_sindex are read-only so threading is safe.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype
from shapely.ops import unary_union

import pipelineConfig


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIRE_ROOT           = Path(pipelineConfig.FIRE_ROOT)
MASTER_CSV          = pipelineConfig.FIRE_SUMMARY_CSV_PATH      # full path under FIRE_ROOT_LOGIN_NODE
OUTPUT_CSV          = pipelineConfig.FIRE_SUMMARY_SAT_CSV_PATH  # full path under FIRE_ROOT_LOGIN_NODE

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
DATE_COL            = pipelineConfig.SAT_DATE_COL
TIME_COL            = pipelineConfig.SAT_TIME_COL
BURN_SHAPE_NAME     = pipelineConfig.BURN_SHAPE_NAME
CASE_SAT_GPKG_NAME  = pipelineConfig.CASE_SAT_GPKG_NAME

MAX_GAP             = pd.Timedelta(days=pipelineConfig.SAT_CHAIN_MAX_GAP_DAYS)
HOTSPOT_BUFFER_DIST = pipelineConfig.SAT_HOTSPOT_BUFFER_DIST
COVERAGE_FRACTION   = pipelineConfig.COVERAGE_FRACTION
SAT_IGNITION_WINDOW = pd.Timedelta(days=pipelineConfig.SAT_IGNITION_WINDOW_DAYS)
UNION_BLOCK_SIZE    = pipelineConfig.SAT_UNION_BLOCK_SIZE
BUFFER_RESOLUTION   = pipelineConfig.SAT_BUFFER_RESOLUTION

MAX_WORKERS         = pipelineConfig.SETUP_PIPELINE_MAX_WORKERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_sat_datetime(df: pd.DataFrame) -> pd.Series:
    """Combine ACQ_DATE and ACQ_TIME (HHMM) into a UTC-naive datetime Series."""
    date_str = df[DATE_COL].astype(str).str.slice(0, 10)
    time_str = df[TIME_COL].astype(str).str.zfill(4)
    return pd.to_datetime(date_str + " " + time_str, errors="coerce")


def _find_chain_end(
    times: pd.Series,
    start_dt: pd.Timestamp,
    max_gap: pd.Timedelta,
) -> pd.Timestamp:
    """Return the last timestamp in the first unbroken chain after *start_dt*."""
    times = times[times >= start_dt].sort_values().reset_index(drop=True)
    if times.empty:
        return pd.NaT
    last = times.iloc[0]
    for t in times.iloc[1:]:
        if (t - last) <= max_gap:
            last = t
        else:
            break
    return last


def _union_geoms(geom_list):
    geom_list = [g for g in geom_list if g is not None and not g.is_empty]
    return unary_union(geom_list) if geom_list else None


def _time_to_coverage_fraction(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    start_dt: pd.Timestamp,
    buffer_dist: float,
    max_gap: pd.Timedelta,
    coverage_fraction: float,
    block_size: int,
    buffer_resolution: int,
) -> tuple[pd.Timestamp, float]:
    """
    Return (timestamp, final_satellite_area_m2) at which the cumulative
    buffered hotspot area first reaches *coverage_fraction* of the effective
    burn area within the first continuous chain after *start_dt*.

    The second return value is always the total satellite coverage area at the
    end of the chain (used by callers to compare with the burn area).
    """
    pts = pts_in_burn[pts_in_burn["sat_datetime"] >= start_dt].copy()
    if pts.empty:
        return pd.NaT, float(burn_geom.area)

    pts = pts.sort_values("sat_datetime").reset_index(drop=True)

    # Restrict to first unbroken chain
    chain_idx = [0]
    last_t = pts.loc[0, "sat_datetime"]
    for i in range(1, len(pts)):
        t = pts.loc[i, "sat_datetime"]
        if (t - last_t) > max_gap:
            break
        chain_idx.append(i)
        last_t = t
    pts = pts.loc[chain_idx].reset_index(drop=True)

    times  = pts["sat_datetime"].tolist()
    buffers = [
        g.buffer(buffer_dist, resolution=buffer_resolution)
        for g in pts.geometry.values
    ]

    final_union    = _union_geoms(buffers)
    final_sat_area = 0.0 if final_union is None else float(final_union.area)

    effective_area = min(float(burn_geom.area), final_sat_area)
    target_area    = coverage_fraction * effective_area

    if target_area <= 0.0:
        return pd.NaT, final_sat_area

    cumulative = None
    n = len(buffers)

    for b0 in range(0, n, block_size):
        b1 = min(b0 + block_size, n)
        block_union = _union_geoms(buffers[b0:b1])
        if block_union is None:
            continue

        trial = block_union if cumulative is None else unary_union([cumulative, block_union])
        if float(trial.area) < target_area:
            cumulative = trial
            continue

        # Threshold crossed somewhere in this block – find the exact point
        local = cumulative
        for i in range(b0, b1):
            buf = buffers[i]
            if buf is None:
                continue
            local = buf if local is None else unary_union([local, buf])
            if float(local.area) >= target_area:
                return times[i], final_sat_area

        return times[b1 - 1], final_sat_area

    return times[-1], final_sat_area


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

    if out_path.exists():
        out_path.unlink()

    g.to_file(out_path, layer=layer, driver="GPKG", engine="pyogrio", index=False)


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
    burn_gdf  = burn_gdf.to_crs("EPSG:5070")
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

    # --- Satellite ignition time (5% coverage) ---
    sat_ignition_dt, _ = _time_to_coverage_fraction(
        pts_in_burn=pts_in_burn,
        burn_geom=burn_geom,
        start_dt=ignition_dt - pd.Timedelta(days=1),
        buffer_dist=HOTSPOT_BUFFER_DIST,
        max_gap=MAX_GAP,
        coverage_fraction=0.05,
        block_size=UNION_BLOCK_SIZE,
        buffer_resolution=BUFFER_RESOLUTION,
    )
    if pd.isna(sat_ignition_dt):
        return None, "SKIP  no sat ignition (5% coverage threshold)"

    start_dt = sat_ignition_dt
    updates  = {SAT_IGNITION_COL: sat_ignition_dt}

    # Save per-case satellite points (each case writes to its own folder — no collision)
    pts_export = pts_in_burn.copy()
    pts_export["folder_name"]     = folder_name
    pts_export["ignition_dt"]     = ignition_dt
    pts_export["sat_ignition_dt"] = start_dt
    if {"ACQ_DATE", "ACQ_TIME"}.issubset(pts_export.columns):
        pts_export["ACQ_DATETIME"] = _build_sat_datetime(pts_export)
    _safe_to_gpkg(pts_export, case_folder / CASE_SAT_GPKG_NAME, layer="points_in_burn")

    # --- Chain end time ---
    chain_end = _find_chain_end(pts_in_burn["sat_datetime"], start_dt, MAX_GAP)
    updates[COL_SAT_CHAIN_END] = chain_end

    # --- Coverage end time ---
    coverage_end, _ = _time_to_coverage_fraction(
        pts_in_burn=pts_in_burn,
        burn_geom=burn_geom,
        start_dt=start_dt,
        buffer_dist=HOTSPOT_BUFFER_DIST,
        max_gap=MAX_GAP,
        coverage_fraction=COVERAGE_FRACTION,
        block_size=UNION_BLOCK_SIZE,
        buffer_resolution=BUFFER_RESOLUTION,
    )

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
    sat_gdf = gpd.read_file(SATELLITE_GPKG, layer=SATELLITE_LAYER)
    sat_gdf = sat_gdf.set_crs("EPSG:5070", inplace=False, allow_override=True)
    sat_gdf["sat_datetime"] = _build_sat_datetime(sat_gdf)
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
    master.to_csv(OUTPUT_CSV, index=False)


if __name__ == "__main__":
    main()
