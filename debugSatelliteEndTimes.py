# -*- coding: utf-8 -*-
"""
Standalone debug script to inspect a coverage-based satellite end time
for a single wildfire case.

Workflow
--------
- Load satellite hotspots from a GeoPackage
- Load the burn polygon for a specified case folder (e.g. '00023')
- Filter hotspots inside the burn polygon and after discovery time
- Build a cumulative hotspot-area curve over time (buffered points,
  unioned and clipped to the burn polygon)
- Define effective area = min(burn area, final satellite coverage area)
- Find the time when cumulative area first reaches
  coverage_fraction * effective_area
- Plot:
    1) Cumulative area vs time with coverage threshold
    2) Burn polygon + hotspot footprint at threshold time, with
       hotspots before/after that time
"""

from pathlib import Path
import argparse

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import GeometryCollection
import matplotlib.pyplot as plt

import pipelineConfig


# ==========================
# CONFIG
# ==========================

FIRE_ROOT = Path(pipelineConfig.FIRE_ROOT)
MASTER_CSV = pipelineConfig.FIRE_SUMMARY_CSV
FOLDER_COL = pipelineConfig.COL_FOLDER
DISCOVERY_COL = pipelineConfig.COL_POINT_DISCOVERY

SATELLITE_GPKG = pipelineConfig.SATELLITE_GPKG
SATELLITE_LAYER = pipelineConfig.SATELLITE_LAYER_NAME
DATE_COL = pipelineConfig.SAT_DATE_COL
TIME_COL = pipelineConfig.SAT_TIME_COL

BURN_SHAPE_NAME = pipelineConfig.BURN_SHAPE_NAME

# Chain max gap (for the first continuous cluster)
MAX_GAP = pd.Timedelta(days=pipelineConfig.SAT_CHAIN_MAX_GAP_DAYS)

# Area-based coverage settings
COVERAGE_FRACTION = 0.85  # 85% of effective area

# Buffer distance around each hotspot point (in CRS units, e.g. meters)
HOTSPOT_BUFFER_DIST = getattr(pipelineConfig, "SAT_HOTSPOT_BUFFER_DIST", 500.0)


# ==========================
# HELPER FUNCTIONS
# ==========================

def build_satellite_datetime(df: pd.DataFrame) -> pd.Series:
    """
    Combine a date column (DATE_COL) and a time column (TIME_COL) into a single
    pandas datetime. Time is expected as HHMM, possibly not zero-padded.
    """
    time_str = df[TIME_COL].astype(str).str.zfill(4)
    date_str = df[DATE_COL].astype(str)
    dt_str = date_str + " " + time_str
    return pd.to_datetime(dt_str, errors="coerce")


def build_cumulative_coverage_chain(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    discovery_dt: pd.Timestamp,
    buffer_dist: float,
    max_gap: pd.Timedelta,
):
    """
    Build the first continuous (gap <= max_gap) cumulative hotspot-coverage chain.

    Returns
    -------
    times : list[pd.Timestamp]
    areas : list[float]
    cumulative_geom : shapely geometry
        Final union geometry, already clipped to burn_geom.
    """
    # Filter by time and sort
    pts = pts_in_burn[pts_in_burn["sat_datetime"] >= discovery_dt].copy()
    if pts.empty:
        return [], [], GeometryCollection()

    pts = pts.sort_values("sat_datetime").reset_index(drop=True)

    times: list[pd.Timestamp] = []
    areas: list[float] = []

    cumulative_geom = GeometryCollection()

    # First point
    first_time = pts.loc[0, "sat_datetime"]
    first_buf = pts.loc[0, "geometry"].buffer(buffer_dist)
    cumulative_geom = first_buf.intersection(burn_geom)
    current_area = cumulative_geom.area

    times.append(first_time)
    areas.append(current_area)

    last_time = first_time

    # Remaining points
    for i in range(1, len(pts)):
        t = pts.loc[i, "sat_datetime"]

        # Chain break
        if (t - last_time) > max_gap:
            break

        buf = pts.loc[i, "geometry"].buffer(buffer_dist)
        cumulative_geom = cumulative_geom.union(buf).intersection(burn_geom)
        current_area = cumulative_geom.area

        times.append(t)
        areas.append(current_area)
        last_time = t

    return times, areas, cumulative_geom


def find_smart_coverage_end_time(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    discovery_dt: pd.Timestamp,
    buffer_dist: float = HOTSPOT_BUFFER_DIST,
    max_gap: pd.Timedelta = MAX_GAP,
    coverage_fraction: float = COVERAGE_FRACTION,
):
    """
    Determine a coverage-based end time.

    End time = first time when cumulative hotspot coverage reaches
    `coverage_fraction` of effective_area, where:

        effective_area = min(burn_area, final_satellite_coverage_area)

    Returns
    -------
    end_time : pd.Timestamp or NaT
    times : list[pd.Timestamp]
    areas : list[float]
    target_area : float
    burn_area : float
    final_sat_area : float
    """
    times, areas, _ = build_cumulative_coverage_chain(
        pts_in_burn=pts_in_burn,
        burn_geom=burn_geom,
        discovery_dt=discovery_dt,
        buffer_dist=buffer_dist,
        max_gap=max_gap,
    )

    if not times:
        return pd.NaT, [], [], 0.0, 0.0, 0.0

    burn_area = burn_geom.area
    final_sat_area = areas[-1]

    effective_area = min(burn_area, final_sat_area)
    target_area = coverage_fraction * effective_area

    end_time = pd.NaT
    for t, a in zip(times, areas):
        if a >= target_area:
            end_time = t
            break

    if pd.isna(end_time):
        end_time = times[-1]

    return end_time, times, areas, target_area, burn_area, final_sat_area


# ==========================
# DEBUG / PLOTTING
# ==========================

def debug_plot_coverage_for_case(
    folder_id: int,
    coverage_fraction: float = COVERAGE_FRACTION,
    buffer_dist: float = HOTSPOT_BUFFER_DIST,
):
    """
    Debug-plot the coverage-based SatelliteEndTime for a single fire case.

    Plots:
      1) Cumulative hotspot area vs time (clipped to burn polygon).
      2) Burn polygon + cumulative hotspot footprint at the threshold time,
         with hotspots before/after that time.
    """

    # -------------------------
    # Load satellite points
    # -------------------------
    print("Loading satellite points from GeoPackage...")
    sat_gdf = gpd.read_file(SATELLITE_GPKG, layer=SATELLITE_LAYER)

    # Force CRS (same as your main script)
    sat_gdf = sat_gdf.set_crs("EPSG:5070", inplace=False, allow_override=True)
    if sat_gdf.crs is None:
        print("  Satellite CRS unknown; assuming EPSG:4326.")
        sat_gdf.set_crs("EPSG:4326", inplace=True)

    sat_gdf["sat_datetime"] = build_satellite_datetime(sat_gdf)
    sat_gdf = sat_gdf.dropna(subset=["sat_datetime"])

    print(f"Loaded {len(sat_gdf)} satellite points.\n")

    # -------------------------
    # Load master CSV and row for this folder
    # -------------------------
    print("Loading master CSV...")
    master = pd.read_csv(MASTER_CSV)

    if FOLDER_COL not in master.columns or DISCOVERY_COL not in master.columns:
        raise KeyError(f"Master CSV must contain {FOLDER_COL} and {DISCOVERY_COL}")

    folder_name = f"{int(folder_id):05d}"
    row_mask = master[FOLDER_COL].astype(int) == int(folder_id)
    if not row_mask.any():
        raise ValueError(f"No master record found for folder_id={folder_id}")

    row = master.loc[row_mask].iloc[0]

    disc_raw = row[DISCOVERY_COL]
    disc_dt = pd.to_datetime(disc_raw, errors="coerce")
    if disc_dt is None or pd.isna(disc_dt):
        raise ValueError(f"Could not parse discovery time for folder {folder_name}")

    if disc_dt.tzinfo is not None:
        disc_dt = disc_dt.tz_convert("UTC").tz_localize(None)

    print(f"Case {folder_name}: discovery time = {disc_dt}\n")

    # -------------------------
    # Load burn polygon (reproject to EPSG:5070 for area in m²)
    # -------------------------
    case_folder = FIRE_ROOT / folder_name
    burn_path = case_folder / BURN_SHAPE_NAME

    if not case_folder.exists():
        raise FileNotFoundError(f"Case folder does not exist: {case_folder}")
    if not burn_path.exists():
        raise FileNotFoundError(f"Burn shapefile not found: {burn_path}")

    burn_gdf = gpd.read_file(burn_path)
    if burn_gdf.empty:
        raise ValueError("Burn shapefile is empty.")

    if burn_gdf.crs is None:
        print("  Burn CRS unknown; assuming EPSG:4326.")
        burn_gdf = burn_gdf.set_crs("EPSG:4326")

    burn_gdf = burn_gdf.to_crs("EPSG:5070")
    burn_geom = burn_gdf.unary_union
    burn_crs = burn_gdf.crs

    # -------------------------
    # Filter satellite points to this burn polygon
    # -------------------------
    sat_in_crs = sat_gdf.to_crs(burn_crs)
    mask = sat_in_crs.geometry.within(burn_geom)
    pts_in_burn = sat_in_crs.loc[mask].copy()

    if pts_in_burn.empty:
        print("No satellite points inside burn area for this case.")
        return

    # -------------------------
    # Compute smart coverage end time
    # -------------------------
    end_time, times_list, area_list, target_area, burn_area, final_sat_area = \
        find_smart_coverage_end_time(
            pts_in_burn=pts_in_burn,
            burn_geom=burn_geom,
            discovery_dt=disc_dt,
            buffer_dist=buffer_dist,
            max_gap=MAX_GAP,
            coverage_fraction=coverage_fraction,
        )

    if pd.isna(end_time):
        print("No valid coverage-based end time could be computed.")
        return

    effective_area = min(burn_area, final_sat_area)

    print(f"Burn area (m²): {burn_area:.0f}")
    print(f"Final satellite coverage (m²): {final_sat_area:.0f}")
    print(f"Effective area = min(burn, sat_final) (m²): {effective_area:.0f}")
    print(f"Target area ({coverage_fraction*100:.0f}% of effective): {target_area:.0f}")
    print(f"Estimated coverage-based SatelliteEndTime for {folder_name}: {end_time}\n")

    # -------------------------
    # Plot 1: cumulative area vs time
    # -------------------------
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))

    ax_time = axes[0]
    times_arr = np.array(times_list)
    area_arr = np.array(area_list)

    ax_time.plot(times_arr, area_arr, marker="o")
    ax_time.axhline(
        target_area,
        linestyle="--",
        label=f"{coverage_fraction*100:.0f}% of min(burn, sat_final)",
    )
    ax_time.axvline(end_time, linestyle="--", label="Threshold time")

    ax_time.set_xlabel("Time")
    ax_time.set_ylabel("Cumulative hotspot area (clipped, m²)")
    ax_time.set_title(f"Cumulative hotspot area – case {folder_name}")
    ax_time.legend()
    ax_time.grid(True)

    # -------------------------
    # Plot 2: spatial footprint at threshold time
    # -------------------------
    ax_map = axes[1]

    # Split points by threshold time
    before_mask = pts_in_burn["sat_datetime"] <= end_time
    after_mask = pts_in_burn["sat_datetime"] > end_time

    pts_before = pts_in_burn.loc[before_mask]
    pts_after = pts_in_burn.loc[after_mask]

    # Rebuild union of buffers up to end_time (clipped to burn_geom)
    coverage_geom = GeometryCollection()
    for geom in pts_before.geometry:
        coverage_geom = coverage_geom.union(geom.buffer(buffer_dist))
    coverage_geom = coverage_geom.intersection(burn_geom)

    # 1) Burn polygon (actual burn scar) – thick black outline
    burn_gdf.plot(
        ax=ax_map,
        facecolor="none",
        edgecolor="black",
        linewidth=2.0,
        label="Burn polygon (actual scar)",
        zorder=3,
    )

    # 2) Hotspot coverage footprint (clip to burn) – semi-transparent fill
    gpd.GeoSeries([coverage_geom], crs=burn_crs).plot(
        ax=ax_map,
        alpha=0.4,
        edgecolor="none",
        label="Hotspot footprint ≤ threshold",
        zorder=1,
    )

    # 3) Hotspot points
    if not pts_before.empty:
        pts_before.plot(
            ax=ax_map,
            markersize=10,
            label="Hotspots ≤ threshold",
            color="blue",
            zorder=4,
        )
    if not pts_after.empty:
        pts_after.plot(
            ax=ax_map,
            markersize=10,
            label="Hotspots > threshold",
            color="red",
            zorder=5,
        )

    ax_map.set_aspect("equal", "box")
    ax_map.set_title(f"Spatial coverage at threshold time – case {folder_name}")
    ax_map.legend()
    ax_map.grid(True)

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    debug_plot_coverage_for_case(
        folder_id = 9,
        coverage_fraction = 0.85,
        buffer_dist = 200,
    )
