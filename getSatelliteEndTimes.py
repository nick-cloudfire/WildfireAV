# -*- coding: utf-8 -*-
"""
Compute SatelliteIgnitionTime and SatelliteEndTime for each fire case
using satellite points stored in a GeoPackage and a master CSV.

For each folder 00001, 00002, ... under FIRE_ROOT:
- Read burn area polygon from burn_area.shp
- Read satellite points from a GeoPackage (with geometry)
- Filter points inside burn polygon
- Determine SatelliteIgnitionTime = earliest satellite detection point inside burn
  within 7 days AFTER the CSV ignition time
- Compute two satellite-based end times relative to SatelliteIgnitionTime:
    1) SatelliteEnd_chain: last time in first continuous chain (<= MAX_GAP)
    2) SatelliteEnd_coverage: time to reach COVERAGE_FRACTION of
       min(burn_area, final_satellite_coverage_area)
- SatelliteEndTime = SatelliteEnd_coverage
- EventEndTime = min(SatelliteEndTime, point_fireout) if point_fireout exists
- Save everything back to the master CSV (OUTPUT_CSV).

Speed improvements:
- Reproject satellite points to EPSG:5070 ONCE (no per-fire reprojection)
- Build spatial index ONCE, bbox-prefilter candidates per fire
- Time-filter candidates before within() (often big win)
- Faster coverage end-time via batched unions (avoids union+intersection every point)
"""

from pathlib import Path
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.ops import unary_union
from pandas.api.types import is_datetime64_any_dtype
import pipelineConfig
import os


# ==========================
# USER SETTINGS / CONFIG
# ==========================

FIRE_ROOT               = Path(pipelineConfig.FIRE_ROOT)
MASTER_CSV              = pipelineConfig.FIRE_SUMMARY_CSV
OUTPUT_CSV              = pipelineConfig.FIRE_SUMMARY_WITH_SATELLITE_CSV

FOLDER_COL              = pipelineConfig.COL_FOLDER
IGNITION_COL            = pipelineConfig.COL_IGNITION_TIME
SAT_IGNITION_COL        = pipelineConfig.COL_SATELLITE_IGNITION
SAT_END_COL             = pipelineConfig.COL_SATELLITE_END
FIREOUT_COL             = pipelineConfig.COL_POINT_FIREOUT
EVENT_END_COL           = pipelineConfig.EVENT_END_COL

SATELLITE_GPKG          = pipelineConfig.SATELLITE_GPKG
SATELLITE_LAYER         = pipelineConfig.SATELLITE_LAYER_NAME
DATE_COL                = pipelineConfig.SAT_DATE_COL
TIME_COL                = pipelineConfig.SAT_TIME_COL
COL_SAT_CHAIN_END_TIME  = pipelineConfig.COL_SAT_CHAIN_END_TIME
COL_SAT_END_AREA        = pipelineConfig.COL_SAT_END_AREA

BURN_SHAPE_NAME         = pipelineConfig.BURN_SHAPE_NAME

MAX_GAP                 = pd.Timedelta(days=pipelineConfig.SAT_CHAIN_MAX_GAP_DAYS)
HOTSPOT_BUFFER_DIST     = pipelineConfig.SAT_HOTSPOT_BUFFER_DIST  # meters in EPSG:5070
COVERAGE_FRACTION       = pipelineConfig.COVERAGE_FRACTION          # e.g. 0.85
UNION_BLOCK_SIZE        = 64
BUFFER_RESOLUTION       = 8
SAT_IGNITION_WINDOW     = pd.Timedelta(days=pipelineConfig.SAT_IGNITION_WINDOW_DAYS)

CASE_SAT_GPKG_NAME      = pipelineConfig.CASE_SAT_GPKG_NAME 

# ==========================
# HELPERS
# ==========================

def build_satellite_datetime(df: pd.DataFrame) -> pd.Series:
    time_str = df[TIME_COL].astype(str).str.zfill(4)
    date_str = df[DATE_COL].astype(str)
    dt_str = date_str + " " + time_str
    return pd.to_datetime(dt_str, errors="coerce")


def find_chain_end(times: pd.Series, start_dt: pd.Timestamp, max_gap: pd.Timedelta) -> pd.Timestamp:
    times = times[times >= start_dt]
    if times.empty:
        return pd.NaT
    times = times.sort_values().reset_index(drop=True)
    
    last = times.iloc[0]
    for t in times.iloc[1:]:
        if (t - last) <= max_gap:
            last = t
        else:
            break

    return last


def _union_list(geom_list):
    geom_list = [x for x in geom_list if x is not None and (not x.is_empty)]
    if not geom_list:
        return None
    return unary_union(geom_list)

def build_acq_datetime(df: pd.DataFrame, date_col="ACQ_DATE", time_col="ACQ_TIME") -> pd.Series:
    date_str = (
        df[date_col]
        .astype(str)
        .str.slice(0, 10)   # strip any ' 00:00:00'
    )

    time_str = (
        df[time_col]
        .astype(str)
        .str.zfill(4)
    )

    dt_str = date_str + " " + time_str
    return pd.to_datetime(dt_str, errors="coerce")

def safe_to_gpkg(
    gdf: gpd.GeoDataFrame,
    out_path: Path,
    layer: str,
    overwrite_layer: bool = False,
    force_wgs84: bool = False,
):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    g = gdf.copy()

    if force_wgs84:
        g = g.to_crs("EPSG:4326")
    if g.crs is None:
        raise ValueError(f"{layer}: missing CRS")

    # geometry cleanup
    g = g[~g.geometry.isna()].copy()
    g = g[~g.geometry.is_empty].copy()
    if g.empty:
        print(f"[safe_to_gpkg] {layer}: nothing to write; skipping")
        return

    # sanitize columns
    geom_col = g.geometry.name
    for c in g.columns:
        if c == geom_col:
            continue
        if is_datetime64_any_dtype(g[c]):
            s = pd.to_datetime(g[c], errors="coerce")
            if s.dt.tz is not None:
                s = s.dt.tz_convert(None)
            g[c] = s.astype("datetime64[ns]")

        # drop non-scalar object columns
        if g[c].dtype == "object":
            sample = g[c].dropna().head(10).tolist()
            if any(isinstance(v, (list, dict, tuple, set)) for v in sample):
                g = g.drop(columns=[c])

    if overwrite_layer and out_path.exists():
        out_path.unlink()

    # pyogrio append semantics
    append = out_path.exists()

    try:
        g.to_file(
            out_path,
            layer=layer,
            driver="GPKG",
            engine="pyogrio",
            append=append,
            index=False,
        )
    except Exception:
        # if append failed due to half-created/corrupt file, recreate once
        if out_path.exists():
            out_path.unlink()
        g.to_file(
            out_path,
            layer=layer,
            driver="GPKG",
            engine="pyogrio",
            append=False,
            index=False,
        )

def time_to_reach_coverage_fraction(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    start_dt: pd.Timestamp,
    buffer_dist: float,
    max_gap: pd.Timedelta,
    coverage_fraction: float,
    block_size: int,
    buffer_resolution: int,
):
    """
    Return the earliest time when cumulative hotspot coverage reaches
    coverage_fraction * min(burn_area, final_satellite_coverage_area)
    within the first continuous chain after start_dt.
    """
    pts = pts_in_burn[pts_in_burn["sat_datetime"] >= start_dt].copy()
    if pts.empty:
        return pd.NaT, float(burn_geom.area), 0.0

    pts = pts.sort_values("sat_datetime").reset_index(drop=True)

    chain_idx = [0]
    last_t = pts.loc[0, "sat_datetime"]
    for i in range(1, len(pts)):
        t = pts.loc[i, "sat_datetime"]
        if (t - last_t) > max_gap:
            break
        chain_idx.append(i)
        last_t = t
    pts = pts.loc[chain_idx].reset_index(drop=True)

    times = pts["sat_datetime"].tolist()
    geoms = pts.geometry.values

    buff_points = []
    for g in geoms:
        buf = g.buffer(buffer_dist, resolution=buffer_resolution)
        buff_points.append(buf)

    if all(cb is None for cb in buff_points):
        return pd.NaT, float(burn_geom.area), 0.0

    final_union = _union_list(buff_points)
    final_sat_area = 0.0 if final_union is None else float(final_union.area)

    burn_area = float(burn_geom.area)
    effective_area = min(burn_area, final_sat_area)
    target_area = coverage_fraction * effective_area

    if target_area <= 0.0:
        return pd.NaT, final_sat_area

    cumulative = None
    n = len(buff_points)

    for b0 in range(0, n, block_size):
        b1 = min(b0 + block_size, n)
        block_union = _union_list(buff_points[b0:b1])
        if block_union is None:
            continue

        trial = block_union if cumulative is None else unary_union([cumulative, block_union])
        if float(trial.area) < target_area:
            cumulative = trial
            continue

        local = cumulative
        for i in range(b0, b1):
            cb = buff_points[i]
            if cb is None:
                continue
            local = cb if local is None else unary_union([local, cb])
            if float(local.area) >= target_area:
                return times[i], final_sat_area

        return times[b1 - 1], final_sat_area

    return times[-1], final_sat_area

# ==========================
# MAIN
# ==========================

def main():
    
    print("Loading satellite points from GeoPackage...")
    sat_gdf = gpd.read_file(SATELLITE_GPKG, layer=SATELLITE_LAYER)
    sat_gdf = sat_gdf.set_crs("EPSG:5070", inplace=False, allow_override=True)
    sat_gdf["sat_datetime"] = build_satellite_datetime(sat_gdf)
    sat_gdf = sat_gdf.dropna(subset=["sat_datetime"])
    sat_sindex = sat_gdf.sindex

    print(f"Loaded {len(sat_gdf)} satellite points.\n")

    # --- Load master CSV ---
    master = pd.read_csv(MASTER_CSV)

    # Validate required columns
    for col in (FOLDER_COL, IGNITION_COL, FIREOUT_COL):
        if col not in master.columns:
            raise KeyError(f"Column '{col}' not found in {MASTER_CSV}")

    # Create / reset columns
    master[SAT_IGNITION_COL] = pd.NaT
    master[COL_SAT_CHAIN_END_TIME] = pd.NaT
    master[COL_SAT_END_AREA] = pd.NaT
    master[SAT_END_COL] = pd.NaT
    master[EVENT_END_COL] = pd.NaT

    print(f"Loaded {len(master)} master records.\n")

    # --- Iterate over fires ---
    for idx, row in master.iterrows():
        folder_id = int(row[FOLDER_COL])
        folder_name = f"{folder_id:05d}"
        case_folder = FIRE_ROOT / folder_name
        burn_path = case_folder / BURN_SHAPE_NAME

        print(f"[{idx}] Folder {folder_name}...")

        if not case_folder.exists():
            print(f"  Case folder does not exist: {case_folder}, skipping.")
            continue
        if not burn_path.exists():
            print(f"  Burn shapefile not found: {burn_path}, skipping.")
            continue

        ignition_dt = pd.to_datetime(row[IGNITION_COL], errors="raise")
        burn_gdf = gpd.read_file(burn_path)
        if burn_gdf.empty:
            print("  Burn shapefile is empty, skipping.")
            continue
        if burn_gdf.crs is None:
            print("  Burn CRS unknown; assuming EPSG:4326.")
            burn_gdf = burn_gdf.set_crs("EPSG:4326")

        burn_gdf = burn_gdf.to_crs("EPSG:5070")
        burn_geom = burn_gdf.union_all()

        # bbox prefilter
        candidate_idx = list(sat_sindex.intersection(burn_geom.bounds))
        if not candidate_idx:
            print("  No satellite candidates in bbox.")
            continue

        candidates = sat_gdf.iloc[candidate_idx]
        candidates = candidates[candidates["sat_datetime"] >= ignition_dt]
        if candidates.empty:
            print("  No satellite points after CSV ignition time in bbox.")
            continue

        pts_in_burn = candidates[candidates.geometry.within(burn_geom)]
        if pts_in_burn.empty:
            print("  No satellite points inside burn area.")
            continue

        # --- Satellite ignition time by 5% area coverage (after scar ignition) ---
        sat_ignition_dt, final_sat_area = time_to_reach_coverage_fraction(
            pts_in_burn=pts_in_burn,
            burn_geom=burn_geom,
            start_dt=ignition_dt - pd.Timedelta(days=1),              
            buffer_dist=HOTSPOT_BUFFER_DIST,
            max_gap=MAX_GAP,
            coverage_fraction=0.05,            # 5% coverage
            block_size=UNION_BLOCK_SIZE,
            buffer_resolution=BUFFER_RESOLUTION,
        )
        if pd.isna(sat_ignition_dt):
            print("  No valid satellite ignition time (5% coverage).")
            continue

        master.at[idx, SAT_IGNITION_COL] = sat_ignition_dt
        start_dt = sat_ignition_dt
        
        out_gpkg = case_folder / CASE_SAT_GPKG_NAME
        if out_gpkg.exists():
            out_gpkg.unlink()
        pts_export = pts_in_burn.copy()
        pts_export["folder_name"] = folder_name
        pts_export["ignition_dt"] = ignition_dt
        pts_export["sat_ignition_dt"] = start_dt
        if {"ACQ_DATE", "ACQ_TIME"}.issubset(pts_export.columns):
            pts_export["ACQ_DATETIME"] = build_acq_datetime(pts_export)
        safe_to_gpkg(
                pts_export,
                out_path=out_gpkg,
                layer="points_in_burn",
                overwrite_layer=False,
                force_wgs84=False
            )
        print(f"  Saved satellite points to {out_gpkg}")


        chain_end_time = find_chain_end(pts_in_burn["sat_datetime"], start_dt, MAX_GAP)
        master.at[idx, COL_SAT_CHAIN_END_TIME] = chain_end_time

        # ---- Coverage end time ----
        coverage_end_time, final_sat_area = time_to_reach_coverage_fraction(
            pts_in_burn=pts_in_burn,
            burn_geom=burn_geom,
            start_dt=start_dt,
            buffer_dist=HOTSPOT_BUFFER_DIST,
            max_gap=MAX_GAP,
            coverage_fraction=COVERAGE_FRACTION,
            block_size=UNION_BLOCK_SIZE,
            buffer_resolution=BUFFER_RESOLUTION,
        )

        master.at[idx, COL_SAT_END_AREA] = coverage_end_time
        end_time = coverage_end_time
        if pd.isna(end_time):
            print("  No valid coverage-based end time.")
            continue

        end_time = pd.to_datetime(end_time)
        master.at[idx, SAT_END_COL] = end_time

        fireout_raw = row[FIREOUT_COL]
        fireout_dt = pd.to_datetime(fireout_raw, errors="coerce")

        if pd.isna(fireout_dt):
            final_end = end_time
        else:
            if getattr(fireout_dt, "tzinfo", None) is not None:
                fireout_dt = fireout_dt.tz_convert("UTC").tz_localize(None)
            if getattr(end_time, "tzinfo", None) is not None:
                end_time = end_time.tz_convert("UTC").tz_localize(None)
            final_end = min(end_time, fireout_dt)

        master.at[idx, EVENT_END_COL] = final_end

    print(f"\nSaving updated master to {OUTPUT_CSV}...")
    master.to_csv(OUTPUT_CSV, index=False)

if __name__ == "__main__":
    main()
