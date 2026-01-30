# -*- coding: utf-8 -*-
"""
Created on Thu Dec 11 15:34:48 2025

@author: NickKalogeropoulos
"""

import geopandas as gpd
import pandas as pd
import re
import pipelineConfig

# -----------------------------------------------------------
# Settings
# -----------------------------------------------------------
input_file          = pipelineConfig.MTBS_PERIMS_RAW   
output_perims_file  = pipelineConfig.MTBS_PERIMS_WITH_IGNITIONS  
threshold           = pipelineConfig.MTBS_AREA_THRESHOLD_ACRES  
ACRES_FIELD         = pipelineConfig.MTBS_ACRES_FIELD
points_file         = pipelineConfig.USFS_POINTS_RAW
output_points_file  = pipelineConfig.USFS_POINTS_MATCHED
PERIM_NAME_FIELD    = pipelineConfig.PERIM_NAME_FIELD
PERIM_DATE_FIELD    = pipelineConfig.PERIM_DATE_FIELD
POINT_NAME_FIELD    = pipelineConfig.POINT_NAME_FIELD
POINT_DISC_FIELD    = pipelineConfig.POINT_DISC_FIELD 
POINT_OUT_FIELD     = pipelineConfig.POINT_OUT_FIELD   
MIN_YEAR            = pipelineConfig.MIN_FIRE_YEAR                      
MAX_YEAR            = pipelineConfig.MAX_FIRE_YEAR  
DAY_TOLERANCE       = pipelineConfig.DAY_TOLERANCE_DAYS  

def check_field_exists(field: str, dataset: gpd.GeoDataFrame) -> None:
    if field not in dataset.columns:
        raise ValueError(f"Field '{field}' not found in the input shapefile.Available fields: {list(dataset.columns)}")

def normalize_name(name: str) -> str:
    if not isinstance(name, str):
        return None
    name = name.upper()
    name_norm = re.sub(r"[^A-Z0-9]", "", name)
    if name_norm == "UNNAMED":
        return None
    return name_norm

# -----------------------------------------------------------
# 0. Process Perimeters
# -----------------------------------------------------------

perims = gpd.read_file(input_file)
check_field_exists(ACRES_FIELD,perims)
check_field_exists(PERIM_NAME_FIELD, perims)
check_field_exists(PERIM_DATE_FIELD, perims)

perims_acres = perims[perims[ACRES_FIELD] > threshold].copy()

perims_acres[PERIM_DATE_FIELD] = pd.to_datetime(perims_acres[PERIM_DATE_FIELD], errors="coerce", utc=True)
perims_acres["ign_year"] = perims_acres[PERIM_DATE_FIELD].dt.year
perims_acres = perims_acres[(perims_acres["ign_year"] >= MIN_YEAR) & (perims_acres["ign_year"] <= MAX_YEAR)].copy()

perims_acres["name_norm"] = perims_acres[PERIM_NAME_FIELD].apply(normalize_name)
perims_valid = perims_acres.dropna(subset=["name_norm", PERIM_DATE_FIELD]).copy()

# Add an explicit perimeter index to track which perims get matched
perims_valid = perims_valid.reset_index().rename(columns={"index": "perim_idx"})
perims_valid = perims_valid.set_index("perim_idx", drop=False)

print(f"Perimeters with valid name within year and area range: {len(perims_valid)} of {len(perims)}")

del perims

# -----------------------------------------------------------
# 2. Process Ignition Points
# -----------------------------------------------------------
points = gpd.read_file(points_file)
check_field_exists(POINT_NAME_FIELD, points)
check_field_exists(POINT_DISC_FIELD, points)
check_field_exists(POINT_OUT_FIELD, points)

points[POINT_DISC_FIELD] = pd.to_datetime(points[POINT_DISC_FIELD], errors="coerce", utc=True)
points[POINT_OUT_FIELD] = pd.to_datetime(points[POINT_OUT_FIELD], errors="coerce", utc=True)

points["disc_year"] = points[POINT_DISC_FIELD].dt.year
points = points[(points["disc_year"] >= MIN_YEAR) & (points["disc_year"] <= MAX_YEAR)].copy()
points["name_norm"] = points[POINT_NAME_FIELD].apply(normalize_name)
points = points.dropna(subset=["name_norm"]).copy()
points_valid = points.dropna(subset=["name_norm", POINT_DISC_FIELD, POINT_OUT_FIELD]).copy()

print(f"Points with valid name within year range: {len(points_valid)} of {len(points)}")

# Ensure CRS matches for spatial tests
if perims_valid.crs != points_valid.crs:
    print(f"Reprojecting points from {points_valid.crs} to {perims_valid.crs}")
    points_valid = points_valid.to_crs(perims_valid.crs)

points_valid = points_valid.reset_index().rename(columns={"index": "pt_idx"})
points_valid = points_valid.set_index("pt_idx", drop=False)

# -----------------------------------------------------------
# 3. Create Merged Dataset
# -----------------------------------------------------------

# Save point and  geometry in a separate column
points_valid["geom_pt"] = points_valid.geometry
points_df = gpd.GeoDataFrame(points_valid.drop(columns="geometry"))
perims_valid["geom_perim"] = perims_valid.geometry
perims_df = gpd.GeoDataFrame(perims_valid.drop(columns="geometry"))
merged = points_df.merge(perims_df, on="name_norm", how="left")

date_diff = (merged[POINT_DISC_FIELD] - merged[PERIM_DATE_FIELD]).dt.days.abs()
mask_time = merged[PERIM_DATE_FIELD].notna() & (date_diff <= DAY_TOLERANCE)
merged_time_filtered = merged[mask_time].copy()

print(f"Point–perimeter matches after name and day filter: {len(merged_time_filtered)}")

# -----------------------------------------------------------
# 4. Spatial rule: point must be inside the matched polygon
# -----------------------------------------------------------
def point_inside_polygon(row):
    pt = row["geom_pt"]
    poly = row["geom_perim"]
    if pt is None or poly is None:
        return False
    try:
        return pt.within(poly)
    except Exception:
        return False

def main():
    merged_time_filtered["inside"] = merged_time_filtered.apply(point_inside_polygon, axis=1)
    merged_spatial = merged_time_filtered[merged_time_filtered["inside"]].copy()
    
    matched_point_idxs = merged_spatial["pt_idx"].unique()
    points_matched = points_df.loc[matched_point_idxs].copy()
    print(f"Unique points matched (after spatial filter): {len(points_matched)}")
    
    matchedperim_idxs = merged_spatial["perim_idx"].astype(int).unique()
    perims_matched = perims_df.loc[matchedperim_idxs].copy()
    print(f"Unique perimeters with at least one ignition point: {len(perims_matched)}")
    
    perims_matched.to_file(output_perims_file, index=False)
    print(f"Filtered perimeters saved to: {output_perims_file}")
    points_matched.to_file(output_points_file, index=False)
    print(f"Filtered points saved to: {output_points_file}")

if __name__ == '__main__':
    main()
