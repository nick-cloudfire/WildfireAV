#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.features import rasterize
import pipelineConfig as cfg

    # Read config
roads_gpkg = cfg.ROADS_GPKG
roads_layer = cfg.ROADS_LAYER
water_gpkg = cfg.WATER_GPKG
water_layer = cfg.WATER_LAYER
backup_water_shp = cfg.BACKUP_WATER_SHP
write_temp = cfg.WRITE_TEMP_CLIPS
keep_temp = cfg.KEEP_TEMP_CLIPS

all_touched = cfg.ALL_TOUCHED
dtype = cfg.DTYPE
nodata = cfg.NODATA

road_class_field = cfg.ROAD_CLASS_FIELD
water_class_field = cfg.WATER_CLASS_FIELD

road_widths_m = cfg.ROAD_WIDTHS_M
water_widths_m = cfg.WATER_WIDTHS_M

use_width_tag = cfg.USE_OSM_WIDTH_TAG
osm_width_field = cfg.OSM_WIDTH_FIELD

INPUTS_FOLDER = cfg.INPUTS_SUBDIR_NAME
DEM_NAME = cfg.LANDFIRE_BAND_FILE_NAMES[0] +".tif"
OUTPUT_NAME = cfg.BARRIER_FILE_NAME

    

# ------------------------------ ogr helpers --------------------------------

def _ogr2ogr_exists() -> bool:
    try:
        subprocess.run(["ogr2ogr", "--version"], check=True, capture_output=True, text=True)
        return True
    except Exception:
        return False


def clip_with_ogr2ogr(
    template_bounds,
    template_crs,
    in_path: str,
    in_layer: Optional[str],
    out_path: str,
    where: Optional[str] = None,
) -> None:
    """
    Clip a vector datasource to template bounds using ogr2ogr streaming.
    Writes out a GeoPackage at out_path.
    """
    if not _ogr2ogr_exists():
        raise RuntimeError(
            "ogr2ogr not found on PATH. Install GDAL (conda gdal) or run from OSGeo4W shell."
        )

    epsg = template_crs.to_epsg()
    if epsg is None:
        spat_srs_arg = ["-spat_srs", template_crs.to_wkt()]
    else:
        spat_srs_arg = ["-spat_srs", f"EPSG:{epsg}"]

    # bbox in template CRS
    spat_arg = [
        "-spat",
        str(template_bounds.left),
        str(template_bounds.bottom),
        str(template_bounds.right),
        str(template_bounds.top),
    ]

    cmd = ["ogr2ogr", "-f", "GPKG", out_path, in_path]
    if in_layer:
        cmd.append(in_layer)

    cmd += spat_arg + spat_srs_arg

    if where:
        cmd += ["-where", where]

    # overwrite
    if os.path.exists(out_path):
        os.remove(out_path)

    subprocess.run(cmd, check=True)



def read_and_bbox_clip(path: str, layer: Optional[str], crs, bounds) -> gpd.GeoDataFrame:
    """Read vector, reproject, and clip to bbox using fast .cx slicing."""
    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    if gdf.empty:
        return gdf
    gdf = gdf.to_crs(crs)
    return gdf.cx[bounds.left:bounds.right, bounds.bottom:bounds.top]


# ---------------------------- width inference ------------------------------

def _parse_width_value(v) -> Optional[float]:
    """Best-effort parse of OSM width-like values to meters."""
    if v is None:
        return None
    try:
        s = str(v).lower().strip()
        if not s:
            return None
        for t in ["meters", "meter", "metres", "metre"]:
            s = s.replace(t, "")
        s = s.strip()
        # keep only first token
        for sep in [";", ",", "/"]:
            if sep in s:
                s = s.split(sep)[0].strip()
        # strip trailing unit 'm'
        s = s.replace("m", "").strip()
        return float(s)
    except Exception:
        return None


def infer_effective_width_m(
    row,
    class_field: str,
    width_map: Dict[str, float],
    use_width_tag: bool,
    width_field: str,
) -> float:
    """Infer effective width in meters from tags."""
    if use_width_tag and (width_field in row):
        w = _parse_width_value(row.get(width_field))
        if w is not None and w > 0:
            return float(w)

    cls = row.get(class_field)
    if cls is None:
        return 0.0
    return float(width_map.get(str(cls), 0.0))


def dissolve_buffer_lines_by_class(
    gdf: gpd.GeoDataFrame,
    class_field: str,
    width_map: Dict[str, float],
) -> List[Tuple[dict, float]]:
    """
    For each class, dissolve/union line geometries and buffer by half width (m).
    Returns shapes for rasterize: [(geom_geojson, burn_value(width_m)), ...]
    """
    shapes = []
    if gdf.empty or class_field not in gdf.columns:
        return shapes

    gdf = gdf[[class_field, "geometry"]].copy()
    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]
    if gdf.empty:
        return shapes

    gdf[class_field] = gdf[class_field].astype(str)
    wanted = set(width_map.keys())
    gdf = gdf[gdf[class_field].isin(wanted)]
    if gdf.empty:
        return shapes

    for cls, w in width_map.items():
        sub = gdf[gdf[class_field] == cls]
        if sub.empty:
            continue

        try:
            geom_union = sub.geometry.union_all()
        except Exception:
            geom_union = sub.unary_union

        if geom_union is None or geom_union.is_empty:
            continue

        # Buffer by half the effective width so the footprint corresponds to width
        half = float(w) / 2.0
        if half <= 0:
            continue

        try:
            geom_buf = geom_union.buffer(half)
        except Exception:
            continue

        if geom_buf is None or geom_buf.is_empty:
            continue

        shapes.append((geom_buf.__geo_interface__, float(w)))

    return shapes

def buffer_lines_fixed_width(
    gdf: gpd.GeoDataFrame,
    width_m: float,
) -> List[Tuple[dict, float]]:
    """
    Buffer all line geometries by half width (m) and burn the fixed width value.
    Returns shapes for rasterize: [(geom_geojson, burn_value(width_m)), ...]
    """
    shapes = []
    if gdf is None or gdf.empty:
        return shapes

    gdf = gdf[["geometry"]].copy()
    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]
    if gdf.empty:
        return shapes

    half = float(width_m) / 2.0
    if half <= 0:
        return shapes

    try:
        geom_union = gdf.geometry.union_all()
    except Exception:
        geom_union = gdf.unary_union

    if geom_union is None or geom_union.is_empty:
        return shapes

    try:
        geom_buf = geom_union.buffer(half)
    except Exception:
        return shapes

    if geom_buf is None or geom_buf.is_empty:
        return shapes

    shapes.append((geom_buf.__geo_interface__, float(width_m)))
    return shapes


def select_backup_rivers_missing_from_primary(
    primary: gpd.GeoDataFrame,
    backup: gpd.GeoDataFrame,
    tol_m: float = 1.0,
) -> gpd.GeoDataFrame:
    """
    Return backup features that do NOT intersect primary features.
    tol_m buffers primary slightly to avoid tiny mismatch gaps.
    """
    if backup is None or backup.empty:
        return backup

    backup = backup[backup.geometry.notnull() & ~backup.geometry.is_empty].copy()
    if backup.empty:
        return backup

    if primary is None or primary.empty:
        return backup  # nothing in primary, so everything is missing

    primary = primary[primary.geometry.notnull() & ~primary.geometry.is_empty].copy()
    if primary.empty:
        return backup

    # build a single "existing" footprint with tolerance
    try:
        prim_union = primary.geometry.union_all()
    except Exception:
        prim_union = primary.unary_union

    if prim_union is None or prim_union.is_empty:
        return backup

    if tol_m and tol_m > 0:
        prim_union = prim_union.buffer(float(tol_m))

    # keep backup features that don't intersect the (buffered) primary
    mask_missing = ~backup.geometry.intersects(prim_union)
    return backup.loc[mask_missing].copy()

# ----------------------------- per-folder run ------------------------------

def process_one_folder(folder: Path) -> bool:
    """
    Process a single fire folder:
      template: folder/inputs/dem.tif
      output:   folder/inputs/barrier.tif

    Returns True if written, False if skipped.
    """
    inputs_dir = folder / INPUTS_FOLDER
    template_tif = inputs_dir / DEM_NAME
    output_tif = inputs_dir / OUTPUT_NAME
    if not roads_gpkg or not water_gpkg:
        raise RuntimeError("Config must define primary ROADS_GPKG and WATER_GPKG.")

    with rasterio.open(template_tif) as src:
        meta = src.meta.copy()
        transform = src.transform
        crs = src.crs
        shape = (src.height, src.width)
        bounds = src.bounds

    if crs is None:
        raise ValueError(f"Template raster has no CRS: {template_tif}")
    if crs.is_geographic:
        raise ValueError(
            f"Template CRS is geographic (degrees) for {template_tif}.\n"
            f"Barrier buffering assumes meters. Use a projected CRS template."
        )

    # ---- Prepare temp clip paths (folder-specific) ----
    tmp_roads = inputs_dir / "tmp_roads_clip.gpkg"
    tmp_water = inputs_dir / "tmp_waterways_clip.gpkg"
    tmp_backup_rivers = inputs_dir / "tmp_backup_rivers_clip.gpkg"
    roads_src = str(roads_gpkg)
    roads_layer_use = roads_layer
    water_src = str(water_gpkg)
    water_layer_use = water_layer

    if write_temp:
        clip_with_ogr2ogr(
            template_bounds=bounds,
            template_crs=crs,
            in_path=str(roads_gpkg),
            in_layer=roads_layer,
            out_path=str(tmp_roads),
            where=f"{road_class_field} IS NOT NULL",
        )
        clip_with_ogr2ogr(
            template_bounds=bounds,
            template_crs=crs,
            in_path=str(water_gpkg),
            in_layer=water_layer,
            out_path=str(tmp_water),
            where=f"{water_class_field} IS NOT NULL",
        )
        if backup_water_shp.exists():
            clip_with_ogr2ogr(
                template_bounds=bounds,
                template_crs=crs,
                in_path=str(backup_water_shp),
                in_layer=None,
                out_path=str(tmp_backup_rivers),
                where=None,
            )

        # let gpd infer layer name in clipped gpkg
        roads_src, roads_layer_use = str(tmp_roads), None
        water_src, water_layer_use = str(tmp_water), None

    # ---- Read clipped vectors ----
    roads = read_and_bbox_clip(roads_src, roads_layer_use, crs, bounds)
    water = read_and_bbox_clip(water_src, water_layer_use, crs, bounds)
    if backup_water_shp.exists():
        backup = read_and_bbox_clip(str(backup_water_shp), None, crs, bounds)

    # If you want to allow OSM width tag inference, compute an effective width field first
    # (not strictly required; we use width_map by class, and width tag only if enabled)
    # We still filter by known classes in dissolve_buffer_lines_by_class.

    # ---- Dissolve + buffer by class ----
    road_shapes = dissolve_buffer_lines_by_class(roads, road_class_field, road_widths_m)
    water_shapes = dissolve_buffer_lines_by_class(water, water_class_field, water_widths_m)

    # ---- Rasterize ----
    arr_roads = np.zeros(shape, dtype=dtype)
    arr_water = np.zeros(shape, dtype=dtype)

    if road_shapes:
        arr_roads = rasterize(
            shapes=road_shapes,
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype=dtype,
            all_touched=all_touched,
        )

    if water_shapes:
        arr_water = rasterize(
            shapes=water_shapes,
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype=dtype,
            all_touched=all_touched,
        )

    arr = np.maximum(arr_roads, arr_water)
    # ---- Add backup rivers only where primary water doesn't already exist ----
    # Tolerance to treat near-coincident lines as already present (meters)
    backup_match_tol_m = 1.0

    missing_backup = select_backup_rivers_missing_from_primary(
        primary=water,
        backup=backup,
        tol_m=backup_match_tol_m,
    )

    backup_width_m = 5.0
    backup_shapes = buffer_lines_fixed_width(missing_backup, width_m=backup_width_m)

    if backup_shapes:
        arr_backup = rasterize(
            shapes=backup_shapes,
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype=dtype,
            all_touched=all_touched,
        )
        arr = np.maximum(arr, arr_backup)

    # ---- Write output ----
    meta.update(count=1, dtype=dtype, nodata=nodata, compress="deflate", tiled=True)

    with rasterio.open(output_tif, "w", **meta) as dst:
        dst.write(arr, 1)

    if write_temp and not keep_temp:
        tmp_files = [tmp_roads, tmp_water]
        if backup_water_shp.exists():
            tmp_files.append(tmp_backup_rivers)

        for f in tmp_files:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass


    return True


def main() -> None:
    firepairs_root = Path(cfg.FIRE_ROOT)
    folders = sorted([p for p in firepairs_root.iterdir() if p.is_dir()])

    for folder in folders:
        print(f"Processing folder: {folder}")
        try:
            process_one_folder(folder)
        except Exception as e:
            # keep pipeline moving but report
            print(f"[barrier] ERROR in {folder.name}: {e}")

    print("[barrier] Done.")


if __name__ == "__main__":
    main()
