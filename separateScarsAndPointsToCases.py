# -*- coding: utf-8 -*-
"""
Created on Thu Dec 11 15:34:48 2025

@author: NickKalogeropoulos
"""

import os
import geopandas as gpd
import pandas as pd
import pipelineConfig

PERIMS_FILE         = pipelineConfig.MTBS_PERIMS_WITH_IGNITIONS
POINTS_FILE         = pipelineConfig.USFS_POINTS_MATCHED
OUTPUT_ROOT         = pipelineConfig.FIRE_ROOT
SUMMARY_CSV         = pipelineConfig.FIRE_SUMMARY_CSV
PERIM_NAME_FIELD    = pipelineConfig.PERIM_NAME_FIELD
PERIM_IGN_FIELD     = pipelineConfig.PERIM_DATE_FIELD           # polygon ignition datetime
PERIM_AREA_FIELD    = pipelineConfig.MTBS_ACRES_FIELD          # polygon area in acres (MTBS standard)
POINT_NAME_FIELD    = pipelineConfig.POINT_NAME_FIELD
POINT_IGN_FIELD     = pipelineConfig.POINT_DISC_FIELD           # point discovery datetime
POINT_OUT_FIELD     = pipelineConfig.POINT_OUT_FIELD            # point fire-out datetime
FIRESCAR_NAME       = pipelineConfig.BURN_SHAPE_NAME
IGNITION_POINT_NAME = pipelineConfig.IGNITION_POINT_SHP_NAME

def main():
    perims = gpd.read_file(PERIMS_FILE)
    points = gpd.read_file(POINTS_FILE)

    perims[PERIM_IGN_FIELD] = pd.to_datetime(
        perims[PERIM_IGN_FIELD],
        errors="coerce",
        utc=True,
    )

    points[POINT_IGN_FIELD] = pd.to_datetime(
        points[POINT_IGN_FIELD],
        errors="coerce",
        utc=True,
    )

    points[POINT_OUT_FIELD] = pd.to_datetime(
        points[POINT_OUT_FIELD],
        errors="coerce",
        utc=True,
    )

    # Ensure CRS matches for spatial operations
    if perims.crs != points.crs:
        print(f"Reprojecting points from {points.crs} to {perims.crs}")
        points = points.to_crs(perims.crs)

    joined = gpd.sjoin(points, perims, how="inner", predicate="within")
    joined["time_diff"] = (joined[POINT_IGN_FIELD] - joined[PERIM_IGN_FIELD]).abs()
    joined = joined.dropna(subset=[POINT_IGN_FIELD, PERIM_IGN_FIELD, "time_diff"]).copy()
    if joined.empty:
        print("No point–polygon pairs after time filtering. Nothing to do.")
        return
    joined_sorted = joined.sort_values(["perim_idx", "time_diff"])
    best_pairs = joined_sorted.drop_duplicates(subset="perim_idx", keep="first").copy()
    print(f"Perimeters with at least one matched ignition point: {len(best_pairs)} of {len(perims)}")

    summary_rows = []
    pad_width = len(str(len(best_pairs))) if len(best_pairs) > 0 else 5
    pad_width = max(pad_width, 5)

    for i, row in enumerate(best_pairs.itertuples(index=False), start=1):
        folder_name = f"{i:0{pad_width}d}"
        folder_path = OUTPUT_ROOT / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)

        scar_gdf = perims[perims["perim_idx"] == row.perim_idx].copy()
        if scar_gdf.empty:
            print(f"Warning: no perimeter found for perim_idx={row.perim_idx}, skipping folder {folder_name}")
            continue
        point_gdf = points[points['pt_idx'] == row.pt_idx].copy()
        if point_gdf.empty:
            print(f"Warning: no ignition point found for pt_idx={row.pt_idx}, skipping folder {folder_name}")
            continue
        
        scar_gdf.to_file(folder_path / FIRESCAR_NAME, layer="firescar", driver="GPKG", index=False)
        point_gdf.to_file(folder_path / IGNITION_POINT_NAME, layer="ignition_point", driver="GPKG", index=False)

        summary_rows.append({
            "folder": folder_name,
            "perim_idx": int(row.perim_idx),
            "perim_name": scar_gdf[PERIM_NAME_FIELD].values[0],
            "perim_ignition": scar_gdf[PERIM_IGN_FIELD].values[0],
            "perim_area_acres": scar_gdf[PERIM_AREA_FIELD].values[0],
            "point_name": point_gdf[POINT_NAME_FIELD].values[0],
            "point_discovery": point_gdf[POINT_IGN_FIELD].values[0],
            "point_fireout": point_gdf[POINT_OUT_FIELD].values[0],
        })

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_df.sort_values("folder", inplace=True)
        summary_df.to_csv(SUMMARY_CSV, index=False)
        print(f"Summary CSV written to: {SUMMARY_CSV}")
    else:
        print("No pairs written; summary CSV not created.")

if __name__ == "__main__":
    main()
