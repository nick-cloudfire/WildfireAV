"""
Which fires the HRRR CONUS winds can cover.

With WINDNINJA_MODE = "hrrrLocal" or "wxModel" the winds come from HRRR's
CONUS 3 km grid (lower 48 states plus a margin into Canada and Mexico).  Fires
in Alaska, Hawaii or Puerto Rico lie outside it and are excluded: by setup for
new case sets, and by the run and the report for existing case folders.  In
"wxsFile" mode (ERA5 point winds) every fire is usable.

A fire is covered when its whole simulation domain - the burn perimeter's
bounding box widened by EXPAND, as for LANDFIRE, plus the margin the HRRR crop
adds - lies inside the grid.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from pyproj import Transformer

import pipelineConfig as cfg

# HRRR CONUS grid (checked against an HRRR GRIB2 file): Lambert conformal on a sphere,
# 1799 x 1059 cells of 3 km; west and north edges of the grid in metres.
HRRR_CRS = "+proj=lcc +lat_0=38.5 +lon_0=-97.5 +lat_1=38.5 +lat_2=38.5 +R=6371229 +units=m +no_defs"
WEST, NORTH, CELL, NCOLS, NROWS = -2699020.14, 1588193.85, 3000.0, 1799, 1059
EAST, SOUTH = WEST + NCOLS * CELL, NORTH - NROWS * CELL

_TO_GRID = Transformer.from_crs("EPSG:4326", HRRR_CRS, always_xy=True)


def uses_hrrr() -> bool:
    return cfg.WINDNINJA_MODE in ("hrrrLocal", "wxModel")


def covers(lonlat_bounds) -> bool:
    """True if HRRR CONUS covers a fire whose perimeter has these (W, S, E, N) degree bounds."""
    w, s, e, n = lonlat_bounds
    dx, dy = cfg.EXPAND * (e - w) / 2, cfg.EXPAND * (n - s) / 2          # LANDFIRE domain
    xmin, ymin, xmax, ymax = _TO_GRID.transform_bounds(w - dx, s - dy, e + dx, n + dy, densify_pts=21)
    pad = max(cfg.HRRR_BUFFER_FRACTION * max(xmax - xmin, ymax - ymin), cfg.HRRR_MIN_BUFFER_M) + 2 * CELL
    return WEST <= xmin - pad and xmax + pad <= EAST and SOUTH <= ymin - pad and ymax + pad <= NORTH


def case_covered(case_dir: Path) -> bool:
    perimeter = case_dir / cfg.BURN_SHAPE_NAME
    if not uses_hrrr() or not perimeter.exists():
        return True
    gdf = gpd.read_file(perimeter)
    return covers((gdf if gdf.crs else gdf.set_crs(4326)).to_crs(4326).total_bounds)


def drop_uncovered(folders: list[Path]) -> list[Path]:
    """Case folders HRRR covers; prints which ones are left out."""
    outside = [f for f in folders if not case_covered(f)]
    if outside:
        print(f"Excluded {len(outside)} case(s) outside the HRRR CONUS domain (e.g. Alaska, Hawaii): "
              + " ".join(f.name for f in outside))
    return [f for f in folders if f not in outside]
