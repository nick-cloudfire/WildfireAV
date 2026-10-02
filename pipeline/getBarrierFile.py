#!/usr/bin/env python3
"""
Step "barrier": rasterise roads and waterways -> inputs/barrier.tif.

Each OSM road / waterway class is buffered by half its nominal width
(``ROAD_WIDTHS_M`` / ``WATER_WIDTHS_M``) and burned onto the DEM grid; pixel
value = barrier width in metres (0 = no barrier).  Backup rivers fill in where
the primary waterway dataset has no feature nearby.

The national datasets are first clipped to the case extent with ogr2ogr
(``inputs/tmp_*_clip.gpkg``); FARSITE's barrier.shp is built from those clips.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize

import pipelineConfig as cfg
from common import atomic_write, for_each_case, require, skipped

DEM_NAME = cfg.LANDFIRE_BAND_FILE_NAMES[0] + ".tif"


def _clip(bounds, crs, src: Path, layer: str | None, out: Path, where: str | None = None) -> None:
    """ogr2ogr-clip a (large) vector dataset to the raster bounds."""
    if out.exists():
        return
    if not shutil.which("ogr2ogr"):
        raise RuntimeError("ogr2ogr not found on PATH (install GDAL)")
    epsg = crs.to_epsg()
    cmd = ["ogr2ogr", "-f", "GPKG", str(out), str(src)] + ([layer] if layer else [])
    cmd += ["-spat", *map(str, (bounds.left, bounds.bottom, bounds.right, bounds.top)),
            "-spat_srs", f"EPSG:{epsg}" if epsg else crs.to_wkt()]
    if where:
        cmd += ["-where", where]
    subprocess.run(cmd, check=True)


def _read(path: Path, layer: str | None, crs, bounds) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    if gdf.empty:
        return gdf
    gdf = gdf.to_crs(crs).cx[bounds.left:bounds.right, bounds.bottom:bounds.top]
    return gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]


def _union(geoms):
    return geoms.union_all() if hasattr(geoms, "union_all") else geoms.unary_union


def _buffered(gdf: gpd.GeoDataFrame, width_m: float) -> list[tuple[dict, float]]:
    """[(shape, width)] for the union of gdf buffered by width/2; [] if nothing."""
    if gdf is None or gdf.empty or width_m <= 0:
        return []
    union = _union(gdf.geometry)
    if union is None or union.is_empty:
        return []
    buf = union.buffer(width_m / 2.0)
    return [] if buf.is_empty else [(buf.__geo_interface__, float(width_m))]


def _by_class(gdf: gpd.GeoDataFrame, field: str, widths: dict[str, float]) -> list[tuple[dict, float]]:
    if gdf.empty or field not in gdf.columns:
        return []
    cls = gdf[field].astype(str)
    return [s for name, w in widths.items() for s in _buffered(gdf[cls == name], w)]


def _process(folder: Path):
    inputs = folder / cfg.INPUTS_SUBDIR_NAME
    template, out_tif = inputs / DEM_NAME, inputs / cfg.BARRIER_FILE_NAME
    if out_tif.exists():
        return skipped(f"{out_tif.name} already exists")
    require(template, hint="run the split_bands step first")
    require(cfg.ROADS_GPKG, cfg.WATER_GPKG, hint="set ROADS_GPKG / WATER_GPKG in pipelineConfig")

    with rasterio.open(template) as src:
        meta, transform, crs, bounds = src.meta.copy(), src.transform, src.crs, src.bounds
        shape = (src.height, src.width)
    if crs is None or crs.is_geographic:
        raise ValueError(f"{template} must have a projected CRS (metres) for barrier buffering; got {crs}")

    roads_src, roads_lyr = Path(cfg.ROADS_GPKG), cfg.ROADS_LAYER
    water_src, water_lyr = Path(cfg.WATER_GPKG), cfg.WATER_LAYER
    backup_src = Path(cfg.BACKUP_WATER_GPKG) if Path(cfg.BACKUP_WATER_GPKG).exists() else None
    tmp = [inputs / cfg.BARRIER_ROADS_CLIP_NAME, inputs / cfg.BARRIER_WATER_CLIP_NAME,
           inputs / cfg.BARRIER_BACKUP_CLIP_NAME]
    if cfg.WRITE_TEMP_CLIPS:
        _clip(bounds, crs, roads_src, roads_lyr, tmp[0], where=f"{cfg.ROAD_CLASS_FIELD} IS NOT NULL")
        _clip(bounds, crs, water_src, water_lyr, tmp[1], where=f"{cfg.WATER_CLASS_FIELD} IS NOT NULL")
        (roads_src, roads_lyr), (water_src, water_lyr) = (tmp[0], None), (tmp[1], None)
        if backup_src:
            _clip(bounds, crs, backup_src, None, tmp[2])
            backup_src = tmp[2]

    roads = _read(roads_src, roads_lyr, crs, bounds)
    water = _read(water_src, water_lyr, crs, bounds)
    backup = _read(backup_src, None, crs, bounds) if backup_src else gpd.GeoDataFrame(geometry=[], crs=crs)

    # Backup rivers only where the primary dataset has nothing within tolerance
    if not backup.empty and not water.empty:
        prim = _union(water.geometry).buffer(cfg.BARRIER_BACKUP_MATCH_TOL_M)
        backup = backup[~backup.geometry.intersects(prim)]

    # Within a layer later classes overwrite earlier ones (rasterize order);
    # roads, waterways and backup rivers are then max-combined.
    arr = np.zeros(shape, dtype=cfg.RASTER_DTYPE)
    for shapes in (_by_class(roads, cfg.ROAD_CLASS_FIELD, cfg.ROAD_WIDTHS_M),
                   _by_class(water, cfg.WATER_CLASS_FIELD, cfg.WATER_WIDTHS_M),
                   _buffered(backup, cfg.BARRIER_BACKUP_WATER_WIDTH_M)):
        if shapes:
            np.maximum(arr, rasterize(shapes, out_shape=shape, transform=transform, fill=0,
                                      dtype=cfg.RASTER_DTYPE, all_touched=cfg.ALL_TOUCHED), out=arr)

    meta.update(count=1, dtype=cfg.RASTER_DTYPE, nodata=cfg.RASTER_NODATA, compress="deflate", tiled=True)
    with atomic_write(out_tif) as tmp, rasterio.open(tmp, "w", **meta) as dst:
        dst.write(arr, 1)
    print(f"  {len(roads)} road, {len(water)} waterway, {len(backup)} backup-river features "
          f"-> {out_tif.name} ({100 * (arr > 0).mean():.2f}% barrier pixels)")

    if cfg.WRITE_TEMP_CLIPS and not cfg.KEEP_TEMP_CLIPS:
        for p in tmp:
            p.unlink(missing_ok=True)


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
