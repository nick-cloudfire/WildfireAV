#!/usr/bin/env python3
"""
Step 7 of runPipelineParallel: create the barrier raster for each case.

Rasterises OSM road and waterway vectors as barrier widths (m).  Where the
primary OSM waterway dataset has no coverage, the backup river shapefile is
used instead.

Output per case
---------------
- inputs/barrier.tif  – float32 raster; pixel value = effective width in metres
                        (0 where no barrier feature is present)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize

import pipelineConfig as cfg

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROADS_GPKG          = cfg.ROADS_GPKG
ROADS_LAYER         = cfg.ROADS_LAYER
WATER_GPKG          = cfg.WATER_GPKG
WATER_LAYER         = cfg.WATER_LAYER
BACKUP_WATER_GPKG    = cfg.BACKUP_WATER_GPKG
WRITE_TEMP          = cfg.WRITE_TEMP_CLIPS
KEEP_TEMP           = cfg.KEEP_TEMP_CLIPS
ALL_TOUCHED         = cfg.ALL_TOUCHED
DTYPE               = cfg.RASTER_DTYPE
NODATA              = cfg.RASTER_NODATA
ROAD_CLASS_FIELD    = cfg.ROAD_CLASS_FIELD
WATER_CLASS_FIELD   = cfg.WATER_CLASS_FIELD
OSM_WIDTH_FIELD     = cfg.OSM_WIDTH_FIELD
USE_WIDTH_TAG       = cfg.USE_OSM_WIDTH_TAG
ROAD_WIDTHS_M       = cfg.ROAD_WIDTHS_M
WATER_WIDTHS_M      = cfg.WATER_WIDTHS_M
BACKUP_WIDTH_M      = cfg.BARRIER_BACKUP_WATER_WIDTH_M
BACKUP_MATCH_TOL_M  = cfg.BARRIER_BACKUP_MATCH_TOL_M

INPUTS_FOLDER = cfg.INPUTS_SUBDIR_NAME
DEM_NAME      = cfg.LANDFIRE_BAND_FILE_NAMES[0] + ".tif"
OUTPUT_NAME   = cfg.BARRIER_FILE_NAME


# ---------------------------------------------------------------------------
# ogr2ogr clipping
# ---------------------------------------------------------------------------

def _ogr2ogr_exists() -> bool:
    try:
        subprocess.run(["ogr2ogr", "--version"], check=True, capture_output=True)
        return True
    except Exception:
        return False


def _clip_with_ogr2ogr(
    template_bounds,
    template_crs,
    in_path: str,
    in_layer: Optional[str],
    out_path: str,
    where: Optional[str] = None,
) -> None:
    if not _ogr2ogr_exists():
        raise RuntimeError(
            "ogr2ogr not found on PATH. Install GDAL (conda gdal) or run from OSGeo4W shell."
        )

    epsg = template_crs.to_epsg()
    spat_srs = ["-spat_srs", f"EPSG:{epsg}"] if epsg else ["-spat_srs", template_crs.to_wkt()]
    spat = [
        "-spat",
        str(template_bounds.left), str(template_bounds.bottom),
        str(template_bounds.right), str(template_bounds.top),
    ]

    cmd = ["ogr2ogr", "-f", "GPKG", out_path, in_path]
    if in_layer:
        cmd.append(in_layer)
    cmd += spat + spat_srs
    if where:
        cmd += ["-where", where]

    if os.path.exists(out_path):
        os.remove(out_path)
    subprocess.run(cmd, check=True)


def _read_and_clip(path: str, layer: Optional[str], crs, bounds) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    if gdf.empty:
        return gdf
    gdf = gdf.to_crs(crs)
    return gdf.cx[bounds.left:bounds.right, bounds.bottom:bounds.top]


# ---------------------------------------------------------------------------
# Width inference
# ---------------------------------------------------------------------------

def _parse_width(v) -> Optional[float]:
    if v is None:
        return None
    try:
        s = str(v).lower().strip()
        for unit in ("meters", "meter", "metres", "metre"):
            s = s.replace(unit, "")
        for sep in (";", ",", "/"):
            if sep in s:
                s = s.split(sep)[0]
        s = s.replace("m", "").strip()
        return float(s) if s else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _dissolve_buffer_by_class(
    gdf: gpd.GeoDataFrame,
    class_field: str,
    width_map: Dict[str, float],
) -> List[Tuple[dict, float]]:
    """Dissolve lines by class and buffer by half-width; return rasterize shapes."""
    shapes = []
    if gdf.empty or class_field not in gdf.columns:
        return shapes

    gdf = gdf[[class_field, "geometry"]].copy()
    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]
    if gdf.empty:
        return shapes

    gdf[class_field] = gdf[class_field].astype(str)
    gdf = gdf[gdf[class_field].isin(width_map)]
    if gdf.empty:
        return shapes

    for cls, w in width_map.items():
        sub = gdf[gdf[class_field] == cls]
        if sub.empty:
            continue
        try:
            union = sub.geometry.union_all()
        except Exception:
            union = sub.unary_union
        if union is None or union.is_empty:
            continue
        half = float(w) / 2.0
        if half <= 0:
            continue
        try:
            buf = union.buffer(half)
        except Exception:
            continue
        if buf is None or buf.is_empty:
            continue
        shapes.append((buf.__geo_interface__, float(w)))

    return shapes


def _buffer_fixed_width(
    gdf: gpd.GeoDataFrame,
    width_m: float,
) -> List[Tuple[dict, float]]:
    """Buffer all geometries by half of *width_m*; return rasterize shapes."""
    if gdf is None or gdf.empty:
        return []
    gdf = gdf[["geometry"]].copy()
    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]
    if gdf.empty:
        return []
    half = float(width_m) / 2.0
    if half <= 0:
        return []
    try:
        union = gdf.geometry.union_all()
    except Exception:
        union = gdf.unary_union
    if union is None or union.is_empty:
        return []
    try:
        buf = union.buffer(half)
    except Exception:
        return []
    if buf is None or buf.is_empty:
        return []
    return [(buf.__geo_interface__, float(width_m))]


def _backup_rivers_not_in_primary(
    primary: gpd.GeoDataFrame,
    backup: gpd.GeoDataFrame,
    tol_m: float,
) -> gpd.GeoDataFrame:
    """Return backup features that do not intersect primary features."""
    if backup is None or backup.empty:
        return backup
    backup = backup[backup.geometry.notnull() & ~backup.geometry.is_empty].copy()
    if backup.empty:
        return backup
    if primary is None or primary.empty:
        return backup
    primary = primary[primary.geometry.notnull() & ~primary.geometry.is_empty].copy()
    if primary.empty:
        return backup
    try:
        prim_union = primary.geometry.union_all()
    except Exception:
        prim_union = primary.unary_union
    if prim_union is None or prim_union.is_empty:
        return backup
    if tol_m > 0:
        prim_union = prim_union.buffer(float(tol_m))
    return backup.loc[~backup.geometry.intersects(prim_union)].copy()


# ---------------------------------------------------------------------------
# Per-folder processor
# ---------------------------------------------------------------------------

def process_one_folder(folder: Path) -> bool:
    inputs_dir   = folder / INPUTS_FOLDER
    template_tif = inputs_dir / DEM_NAME
    output_tif   = inputs_dir / OUTPUT_NAME

    if not ROADS_GPKG or not WATER_GPKG:
        raise RuntimeError("ROADS_GPKG and WATER_GPKG must be set in pipelineConfig.")

    if output_tif.exists():
        print(f"  Skipped — {output_tif.name} already exists.")
        return True

    with rasterio.open(template_tif) as src:
        meta      = src.meta.copy()
        transform = src.transform
        crs       = src.crs
        shape     = (src.height, src.width)
        bounds    = src.bounds

    if crs is None:
        raise ValueError(f"Template raster has no CRS: {template_tif}")
    if crs.is_geographic:
        raise ValueError(
            f"Template CRS is geographic (degrees) for {template_tif}. "
            "Barrier buffering requires a projected CRS (metres)."
        )

    # Temporary clip paths
    tmp_roads   = inputs_dir / cfg.BARRIER_ROADS_CLIP_NAME
    tmp_water   = inputs_dir / cfg.BARRIER_WATER_CLIP_NAME
    tmp_backup  = inputs_dir / cfg.BARRIER_BACKUP_CLIP_NAME

    roads_src  = str(ROADS_GPKG)
    roads_lyr  = ROADS_LAYER
    water_src  = str(WATER_GPKG)
    water_lyr  = WATER_LAYER

    if WRITE_TEMP:
        if not tmp_roads.exists():
            _clip_with_ogr2ogr(bounds, crs, str(ROADS_GPKG), ROADS_LAYER,
                                str(tmp_roads), where=f"{ROAD_CLASS_FIELD} IS NOT NULL")
        if not tmp_water.exists():
            _clip_with_ogr2ogr(bounds, crs, str(WATER_GPKG), WATER_LAYER,
                                str(tmp_water), where=f"{WATER_CLASS_FIELD} IS NOT NULL")
        if BACKUP_WATER_GPKG.exists() and not tmp_backup.exists():
            _clip_with_ogr2ogr(bounds, crs, str(BACKUP_WATER_GPKG), None, str(tmp_backup))
        roads_src, roads_lyr = str(tmp_roads), None
        water_src, water_lyr = str(tmp_water), None

    roads  = _read_and_clip(roads_src, roads_lyr, crs, bounds)
    water  = _read_and_clip(water_src, water_lyr, crs, bounds)
    backup = gpd.GeoDataFrame(geometry=[], crs=crs)
    if BACKUP_WATER_GPKG.exists():
        backup = _read_and_clip(str(BACKUP_WATER_GPKG), None, crs, bounds)

    road_shapes  = _dissolve_buffer_by_class(roads,  ROAD_CLASS_FIELD,  ROAD_WIDTHS_M)
    water_shapes = _dissolve_buffer_by_class(water,  WATER_CLASS_FIELD, WATER_WIDTHS_M)

    def _rasterize(shapes):
        if not shapes:
            return np.zeros(shape, dtype=DTYPE)
        return rasterize(
            shapes=shapes, out_shape=shape, transform=transform,
            fill=0, dtype=DTYPE, all_touched=ALL_TOUCHED,
        )

    arr = np.maximum(_rasterize(road_shapes), _rasterize(water_shapes))

    missing_backup = _backup_rivers_not_in_primary(water, backup, BACKUP_MATCH_TOL_M)
    backup_shapes  = _buffer_fixed_width(missing_backup, BACKUP_WIDTH_M)
    if backup_shapes:
        arr = np.maximum(arr, _rasterize(backup_shapes))

    meta.update(count=1, dtype=DTYPE, nodata=NODATA, compress="deflate", tiled=True)
    with rasterio.open(output_tif, "w", **meta) as dst:
        dst.write(arr, 1)

    if WRITE_TEMP and not KEEP_TEMP:
        for tmp in (tmp_roads, tmp_water, tmp_backup):
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    return True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(case_dir=None) -> None:
    if case_dir is not None:
        folders = [Path(case_dir)]
    else:
        firepairs_root = Path(cfg.FIRE_ROOT)
        folders = sorted(
            p for p in firepairs_root.iterdir() if p.is_dir() and p.name.isdigit()
        )

    for folder in folders:
        print(f"Processing: {folder}")
        try:
            process_one_folder(folder)
        except Exception as e:
            print(f"  ERROR in {folder.name}: {type(e).__name__}: {e}")

    print("[barrier] Done.")


if __name__ == "__main__":
    main()
