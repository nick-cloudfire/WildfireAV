# separateScarsAndPointsToCases.py
"""
Step 2 of setupPipeline: create numbered case folders from matched pairs.

Reads the matched perimeters and ignition points produced by
processScarsAndPoints.py, then for each perimeter–point pair:
- Creates a numbered folder (00001, 00002, …) under FIRE_ROOT_LOGIN_NODE.
- Writes firescar.gpkg and ignition_point.gpkg into the folder.
- Writes case_metadata.json.

Where multiple ignition points match the same perimeter the one closest in
time to the perimeter ignition date is chosen.

Output
------
- FIRE_ROOT_LOGIN_NODE/00001/, 00002/, … (one folder per fire case)
- FIRE_ROOT_LOGIN_NODE/fire_pairs_summary.csv
"""

from pathlib import Path

import geopandas as gpd
import pandas as pd

import pipelineConfig
from case_metadata import write_case_metadata

# ---------------------------------------------------------------------------
# Config (all paths are absolute, resolved from pipelineConfig)
# ---------------------------------------------------------------------------

PERIMS_FILE         = pipelineConfig.MTBS_PERIMS_WITH_IGNITIONS   # full path
POINTS_FILE         = pipelineConfig.USFS_POINTS_MATCHED           # full path
OUTPUT_ROOT         = Path(pipelineConfig.FIRE_ROOT_LOGIN_NODE)
SUMMARY_CSV         = pipelineConfig.FIRE_SUMMARY_CSV_PATH         # full path

PERIM_NAME_FIELD    = pipelineConfig.PERIM_NAME_FIELD
PERIM_IGN_FIELD     = pipelineConfig.PERIM_DATE_FIELD
PERIM_AREA_FIELD    = pipelineConfig.MTBS_ACRES_FIELD
POINT_NAME_FIELD    = pipelineConfig.POINT_NAME_FIELD
POINT_IGN_FIELD     = pipelineConfig.POINT_DISC_FIELD
POINT_OUT_FIELD     = pipelineConfig.POINT_OUT_FIELD
FIRESCAR_NAME       = pipelineConfig.BURN_SHAPE_NAME
IGNITION_POINT_NAME = pipelineConfig.IGNITION_POINT_SHP_NAME


def main() -> None:
    perims = gpd.read_file(PERIMS_FILE)
    points = gpd.read_file(POINTS_FILE)

    perims[PERIM_IGN_FIELD] = pd.to_datetime(
        perims[PERIM_IGN_FIELD], errors="coerce", utc=True
    )
    points[POINT_IGN_FIELD] = pd.to_datetime(
        points[POINT_IGN_FIELD], errors="coerce", utc=True
    )
    points[POINT_OUT_FIELD] = pd.to_datetime(
        points[POINT_OUT_FIELD], errors="coerce", utc=True
    )

    if perims.crs != points.crs:
        print(f"Reprojecting points from {points.crs} to {perims.crs}")
        points = points.to_crs(perims.crs)

    # Spatial join + pick best (closest-in-time) point per perimeter
    joined = gpd.sjoin(points, perims, how="inner", predicate="within")
    joined["time_diff"] = (joined[POINT_IGN_FIELD] - joined[PERIM_IGN_FIELD]).abs()
    joined = joined.dropna(subset=[POINT_IGN_FIELD, PERIM_IGN_FIELD, "time_diff"]).copy()
    if joined.empty:
        print("No point–polygon pairs found. Nothing to do.")
        return

    best_pairs = (
        joined.sort_values(["perim_idx", "time_diff"])
        .drop_duplicates(subset="perim_idx", keep="first")
        .copy()
    )
    print(f"Best-matched pairs: {len(best_pairs)} of {len(perims)} perimeters")

    pad_width = max(5, len(str(len(best_pairs))))
    summary_rows = []

    for i, row in enumerate(best_pairs.itertuples(index=False), start=1):
        folder_name = f"{i:0{pad_width}d}"
        folder_path = OUTPUT_ROOT / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)

        scar_gdf = perims[perims["perim_idx"] == row.perim_idx].copy()
        if scar_gdf.empty:
            print(f"  Warning: no perimeter for perim_idx={row.perim_idx}, skipping {folder_name}")
            continue

        point_gdf = points[points["pt_idx"] == row.pt_idx].copy()
        if point_gdf.empty:
            print(f"  Warning: no point for pt_idx={row.pt_idx}, skipping {folder_name}")
            continue

        scar_gdf.to_file(
            folder_path / FIRESCAR_NAME, layer="firescar", driver="GPKG", index=False
        )
        point_gdf.to_file(
            folder_path / IGNITION_POINT_NAME, layer="ignition_point", driver="GPKG", index=False
        )

        metadata = {
            pipelineConfig.COL_FOLDER: folder_name,
            "perim_idx":        int(row.perim_idx),
            "perim_name":       scar_gdf[PERIM_NAME_FIELD].values[0],
            "perim_ignition":   scar_gdf[PERIM_IGN_FIELD].values[0],
            "perim_area_acres": scar_gdf[PERIM_AREA_FIELD].values[0],
            "point_name":       point_gdf[POINT_NAME_FIELD].values[0],
            pipelineConfig.COL_POINT_DISCOVERY: point_gdf[POINT_IGN_FIELD].values[0],
            pipelineConfig.COL_POINT_FIREOUT:   point_gdf[POINT_OUT_FIELD].values[0],
        }
        write_case_metadata(folder_path, metadata)
        summary_rows.append(metadata)

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_df.sort_values(pipelineConfig.COL_FOLDER, inplace=True)
        summary_df.to_csv(SUMMARY_CSV, index=False)
        print(f"Summary CSV written to: {SUMMARY_CSV}")
    else:
        print("No pairs written; summary CSV not created.")


if __name__ == "__main__":
    main()
