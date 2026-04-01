# processScarsAndPoints.py
"""
Step 1 of setupPipeline: match MTBS burn perimeters to USFS ignition points.

Algorithm
---------
1. Load MTBS perimeters, filter by area threshold and fire-year range.
2. Load USFS ignition points, filter by year range.
3. Normalise fire names (uppercase, alphanumeric only) for name matching.
4. Inner-join on normalised name.
5. Filter by date proximity (|point_discovery - perim_ignition| <= DAY_TOLERANCE).
6. Spatial filter: point must lie within the matched polygon.

Outputs (written to the current working directory)
--------------------------------------------------
- perimeters_ignitions.gpkg  – matched perimeters
- all_ignitions.gpkg         – matched ignition points
"""

import re

import geopandas as gpd
import pandas as pd

import pipelineConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_field(field: str, gdf: gpd.GeoDataFrame) -> None:
    if field not in gdf.columns:
        raise ValueError(
            f"Field '{field}' not found. Available: {list(gdf.columns)}"
        )


def _normalize_name(name: str) -> str | None:
    """Return uppercase alphanumeric fire name, or None for invalid/unnamed."""
    if not isinstance(name, str):
        return None
    normed = re.sub(r"[^A-Z0-9]", "", name.upper())
    return None if normed == "UNNAMED" else normed


def _point_inside_polygon(row) -> bool:
    pt = row["geom_pt"]
    poly = row["geom_perim"]
    if pt is None or poly is None:
        return False
    try:
        return pt.within(poly)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    input_file         = pipelineConfig.MTBS_PERIMS_RAW
    output_perims_file = pipelineConfig.MTBS_PERIMS_WITH_IGNITIONS   # full path under FIRE_ROOT_LOGIN_NODE
    output_points_file = pipelineConfig.USFS_POINTS_MATCHED          # full path under FIRE_ROOT_LOGIN_NODE
    threshold          = pipelineConfig.MTBS_AREA_THRESHOLD_ACRES
    points_file        = pipelineConfig.USFS_POINTS_RAW
    acres_field        = pipelineConfig.MTBS_ACRES_FIELD
    perim_name_field   = pipelineConfig.PERIM_NAME_FIELD
    perim_date_field   = pipelineConfig.PERIM_DATE_FIELD
    point_name_field   = pipelineConfig.POINT_NAME_FIELD
    point_disc_field   = pipelineConfig.POINT_DISC_FIELD
    point_out_field    = pipelineConfig.POINT_OUT_FIELD
    min_year           = pipelineConfig.MIN_FIRE_YEAR
    max_year           = pipelineConfig.MAX_FIRE_YEAR
    day_tolerance      = pipelineConfig.DAY_TOLERANCE_DAYS

    # ------------------------------------------------------------------
    # 1. Load and filter perimeters
    # ------------------------------------------------------------------
    perims = gpd.read_file(input_file)
    for f in (acres_field, perim_name_field, perim_date_field):
        _check_field(f, perims)

    perims = perims[perims[acres_field] > threshold].copy()
    perims[perim_date_field] = pd.to_datetime(
        perims[perim_date_field], errors="coerce", utc=True
    )
    perims["ign_year"] = perims[perim_date_field].dt.year
    perims = perims[
        (perims["ign_year"] >= min_year) & (perims["ign_year"] <= max_year)
    ].copy()
    perims["name_norm"] = perims[perim_name_field].apply(_normalize_name)
    perims = perims.dropna(subset=["name_norm", perim_date_field]).copy()
    perims = perims.reset_index().rename(columns={"index": "perim_idx"})
    perims = perims.set_index("perim_idx", drop=False)
    print(f"Perimeters after area/year/name filter: {len(perims)}")
    if len(perims) == 0:
        raise RuntimeError(
            "No MTBS perimeters remain after filtering.\n"
            f"  Current settings:  area > {threshold} acres,  year {min_year}–{max_year}\n"
            "  To fix, relax one or more of these in pipelineConfig.py:\n"
            "    MTBS_AREA_THRESHOLD_ACRES  (lower the minimum burn area)\n"
            "    MIN_FIRE_YEAR / MAX_FIRE_YEAR  (widen the year range)"
        )

    # ------------------------------------------------------------------
    # 2. Load and filter ignition points
    # ------------------------------------------------------------------
    points = gpd.read_file(points_file)
    for f in (point_name_field, point_disc_field, point_out_field):
        _check_field(f, points)

    points[point_disc_field] = pd.to_datetime(
        points[point_disc_field], errors="coerce", utc=True
    )
    points[point_out_field] = pd.to_datetime(
        points[point_out_field], errors="coerce", utc=True
    )
    points["disc_year"] = points[point_disc_field].dt.year
    points = points[
        (points["disc_year"] >= min_year) & (points["disc_year"] <= max_year)
    ].copy()
    points["name_norm"] = points[point_name_field].apply(_normalize_name)
    points = points.dropna(
        subset=["name_norm", point_disc_field, point_out_field]
    ).copy()
    points = points.reset_index().rename(columns={"index": "pt_idx"})
    points = points.set_index("pt_idx", drop=False)
    print(f"Points after year/name filter: {len(points)}")
    if len(points) == 0:
        raise RuntimeError(
            "No USFS ignition points remain after filtering.\n"
            f"  Current settings:  year {min_year}–{max_year}\n"
            "  To fix, relax in pipelineConfig.py:\n"
            "    MIN_FIRE_YEAR / MAX_FIRE_YEAR  (widen the year range)"
        )

    if perims.crs != points.crs:
        print(f"Reprojecting points from {points.crs} to {perims.crs}")
        points = points.to_crs(perims.crs)

    # ------------------------------------------------------------------
    # 3. Name + date join
    # ------------------------------------------------------------------
    points["geom_pt"] = points.geometry
    points_df = gpd.GeoDataFrame(points.drop(columns="geometry"))

    perims["geom_perim"] = perims.geometry
    perims_df = gpd.GeoDataFrame(perims.drop(columns="geometry"))

    merged = points_df.merge(perims_df, on="name_norm", how="left")
    date_diff = (merged[point_disc_field] - merged[perim_date_field]).dt.days.abs()
    mask_time = merged[perim_date_field].notna() & (date_diff <= day_tolerance)
    merged_time = merged[mask_time].copy()
    print(f"Matches after name + date filter: {len(merged_time)}")
    if len(merged_time) == 0:
        raise RuntimeError(
            "No perimeter–point matches found after name and date filtering.\n"
            f"  Current settings:  DAY_TOLERANCE_DAYS = {day_tolerance}\n"
            "  To fix, relax in pipelineConfig.py:\n"
            "    DAY_TOLERANCE_DAYS  (allow a wider date window for matching)"
        )

    # ------------------------------------------------------------------
    # 4. Spatial filter: point must lie inside matched polygon
    # ------------------------------------------------------------------
    merged_time["inside"] = merged_time.apply(_point_inside_polygon, axis=1)
    merged_spatial = merged_time[merged_time["inside"]].copy()

    matched_pt_idxs = merged_spatial["pt_idx"].unique()
    points_matched = points_df.loc[matched_pt_idxs].copy()
    print(f"Unique matched points (after spatial filter): {len(points_matched)}")
    if len(points_matched) == 0:
        raise RuntimeError(
            "No matches survived the spatial filter (point must lie inside polygon).\n"
            f"  {len(merged_time)} name+date match(es) were found but none passed spatial check.\n"
            "  To fix, try relaxing in pipelineConfig.py:\n"
            "    DAY_TOLERANCE_DAYS  (wider date window may yield better spatial matches)\n"
            "    MTBS_AREA_THRESHOLD_ACRES  (smaller fires may have better point coverage)"
        )

    matched_perim_idxs = merged_spatial["perim_idx"].astype(int).unique()
    perims_matched = perims_df.loc[matched_perim_idxs].copy()
    print(f"Unique matched perimeters: {len(perims_matched)}")

    perims_matched.to_file(output_perims_file, index=False)
    print(f"Saved perimeters to: {output_perims_file}")

    points_matched.to_file(output_points_file, index=False)
    print(f"Saved points to: {output_points_file}")


if __name__ == "__main__":
    main()
