"""
Step 8 of runPipelineParallel: write the Elmfire namelist (.data) file.

For each case:
- Reads the DEM to derive domain parameters (EPSG, cellsize, xll, yll).
- Reads the ignition point and snaps it to the nearest valid FBFM40 fuel cell.
- Reads simulation start/end times from case_metadata.json.
- Writes <case_folder>/<folder_name>.data (Fortran namelist format).

All tuneable Elmfire parameters come from pipelineConfig; nothing is
hard-coded in this script.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import fiona
import numpy as np
import pandas as pd
import pipelineConfig as cfg
import rasterio
from case_metadata import read_case_metadata
from pyproj import Transformer
from rasterio.transform import Affine
from rasterio.transform import xy as transform_xy
from rasterio.windows import Window

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIRE_ROOT  = cfg.FIRE_ROOT
FIRE_CSV   = cfg.FIRE_SUMMARY_SAT_CSV_PATH   # full path under FIRE_ROOT_LOGIN_NODE
FOLDER_COL = cfg.COL_FOLDER
START_COL  = cfg.COL_SATELLITE_IGNITION
END_COL    = cfg.EVENT_END_COL
INPUTS     = cfg.INPUTS_SUBDIR_NAME

# Elmfire simulation parameters (from pipelineConfig)
DT_METEOROLOGY = cfg.ELMFIRE_DT_METEOROLOGY
DTDUMP         = cfg.ELMFIRE_DTDUMP
SIMULATION_DT  = cfg.ELMFIRE_SIMULATION_DT
TARGET_CFL     = cfg.ELMFIRE_TARGET_CFL
LH_MC          = cfg.ELMFIRE_LH_MC
LW_MC          = cfg.ELMFIRE_LW_MC
PATH_TO_GDAL   = cfg.ELMFIRE_PATH_TO_GDAL

# Relative directory names used inside the namelist (relative to case folder)
FUELS_DIR   = f"./{INPUTS}"
WEATHER_DIR = f"./{INPUTS}"
OUTPUTS_DIR = f"./{cfg.ELMFIRE_OUTPUTS_SUBDIR}"
SCRATCH_DIR = f"./{cfg.ELMFIRE_SCRATCH_SUBDIR}"

# Build input filename mappings from pipelineConfig to avoid duplication.
# Keys are Elmfire namelist keys; values are filenames WITHOUT extension.
FUELS_TOPO_FILENAMES: dict[str, str] = {
    "ASP_FILENAME":  cfg.LANDFIRE_BAND_FILE_NAMES[2],   # asp
    "CBD_FILENAME":  cfg.LANDFIRE_BAND_FILE_NAMES[7],   # cbd
    "CBH_FILENAME":  cfg.LANDFIRE_BAND_FILE_NAMES[6],   # cbh
    "CC_FILENAME":   cfg.LANDFIRE_BAND_FILE_NAMES[4],   # cc
    "CH_FILENAME":   cfg.LANDFIRE_BAND_FILE_NAMES[5],   # ch
    "DEM_FILENAME":  cfg.LANDFIRE_BAND_FILE_NAMES[0],   # dem
    "FBFM_FILENAME": cfg.LANDFIRE_BAND_FILE_NAMES[3],   # fbfm40
    "SLP_FILENAME":  cfg.LANDFIRE_BAND_FILE_NAMES[1],   # slp
    "ADJ_FILENAME":  cfg.ADJ_FILE_NAME.removesuffix(".tif"),
    "PHI_FILENAME":  cfg.PHI_FILE_NAME.removesuffix(".tif"),
}

MET_FILENAMES: dict[str, str] = {
    "WS_FILENAME":   cfg.WS_TIF_NAME.removesuffix(".tif"),
    "WD_FILENAME":   cfg.WD_TIF_NAME.removesuffix(".tif"),
    "M1_FILENAME":   cfg.FMC_FILE_NAMES[0],
    "M10_FILENAME":  cfg.FMC_FILE_NAMES[1],
    "M100_FILENAME": cfg.FMC_FILE_NAMES[2],
}

BARRIER_STEM = cfg.BARRIER_FILE_NAME.removesuffix(".tif")


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _compute_domain(dem_path: Path) -> tuple[str, float, float, float]:
    """Return (epsg_str, cellsize, xllcorner, yllcorner) from a DEM."""
    with rasterio.open(dem_path) as ds:
        transform: Affine = ds.transform
        cellsize = abs(transform.a)          # assume square pixels
        xll = transform.c
        yll = transform.f + transform.e * ds.height   # e is negative
        epsg = ds.crs.to_epsg() if ds.crs is not None else None
    epsg_str = f"EPSG: {epsg}" if epsg is not None else "EPSG: UNKNOWN"
    return epsg_str, cellsize, xll, yll


def _ignition_xy_in_dem_crs(dem_path: Path, shp_path: Path) -> tuple[float, float]:
    """Read ignition point and return coordinates in the DEM's CRS."""
    with rasterio.open(dem_path) as ds:
        dem_crs = ds.crs
    if dem_crs is None:
        raise ValueError(f"DEM at {dem_path} has no CRS")

    with fiona.open(shp_path, "r") as src:
        feat = next(iter(src), None)
        if feat is None:
            raise ValueError(f"No features in {shp_path}")
        geom = feat["geometry"]
        if geom is None or geom["type"] != "Point":
            raise ValueError(f"Ignition geometry must be a Point in {shp_path}")
        x_src, y_src = geom["coordinates"]
        shp_crs = src.crs

    if shp_crs is None:
        return float(x_src), float(y_src)
    transformer = Transformer.from_crs(shp_crs, dem_crs, always_xy=True)
    x, y = transformer.transform(x_src, y_src)
    return float(x), float(y)


def _latlon_from_dem_xy(dem_path: Path, x: float, y: float) -> tuple[float, float]:
    """Convert DEM-CRS (x, y) to (lat, lon) in EPSG:4326."""
    with rasterio.open(dem_path) as ds:
        dem_crs = ds.crs
    if dem_crs is None:
        raise ValueError(f"DEM at {dem_path} has no CRS")
    tr = Transformer.from_crs(dem_crs, "EPSG:4326", always_xy=True)
    lon, lat = tr.transform(x, y)
    return float(lat), float(lon)


# ---------------------------------------------------------------------------
# Ignition snapping
# ---------------------------------------------------------------------------

def _is_valid_fuel(val, nodata, valid_min: float) -> bool:
    if val is None:
        return False
    if nodata is not None and val == nodata:
        return False
    try:
        if np.isnan(val):
            return False
    except Exception:
        pass
    return val >= valid_min


def _snap_to_valid_fuel(
    fuels_tif: Path,
    x: float,
    y: float,
    valid_min: float = 101.0,
    max_radius_cells: int = 2000,
) -> tuple[float, float, bool]:
    """
    Snap (x, y) to the centre of the nearest pixel with fuel code >= valid_min.

    Returns (x_snapped, y_snapped, was_moved).
    """
    with rasterio.open(fuels_tif) as ds:
        row0, col0 = ds.index(x, y)
        row0 = min(max(row0, 0), ds.height - 1)
        col0 = min(max(col0, 0), ds.width - 1)

        v0 = ds.read(1, window=Window(col0, row0, 1, 1), masked=False)[0, 0]
        if _is_valid_fuel(v0, ds.nodata, valid_min):
            xc, yc = transform_xy(ds.transform, row0, col0, offset="center")
            return float(xc), float(yc), False

        for r in range(1, max_radius_cells + 1):
            r0 = max(row0 - r, 0)
            r1 = min(row0 + r, ds.height - 1)
            c0 = max(col0 - r, 0)
            c1 = min(col0 + r, ds.width - 1)

            arr = ds.read(1, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1), masked=False)
            valid = arr >= valid_min
            if ds.nodata is not None:
                valid &= arr != ds.nodata
            if np.issubdtype(arr.dtype, np.floating):
                valid &= ~np.isnan(arr)
            if not np.any(valid):
                continue

            vrows, vcols = np.where(valid)
            rows = vrows + r0
            cols = vcols + c0
            d2 = (rows - row0) ** 2 + (cols - col0) ** 2
            k = int(np.argmin(d2))
            xc, yc = transform_xy(ds.transform, int(rows[k]), int(cols[k]), offset="center")
            return float(xc), float(yc), True

    raise RuntimeError(
        f"No valid fuel (>= {valid_min}) within {max_radius_cells} cells of ignition"
    )


# ---------------------------------------------------------------------------
# Namelist builder
# ---------------------------------------------------------------------------

def _build_namelist(
    epsg_str: str,
    cellsize: float,
    xll: float,
    yll: float,
    tstop_sec: float,
    x_ign: float,
    y_ign: float,
    latitude: float,
    longitude: float,
    current_year: int,
    hour_of_year: int,
    num_meteorology_times: int,
) -> str:
    lines = []

    lines.append("&INPUTS")
    lines.append(f"FUELS_AND_TOPOGRAPHY_DIRECTORY = '{FUELS_DIR}'")
    for key, val in FUELS_TOPO_FILENAMES.items():
        lines.append(f"{key:<30} = '{val}'")
    lines.append(f"DT_METEOROLOGY                 = {DT_METEOROLOGY:.1f}")
    lines.append(f"WEATHER_DIRECTORY              = '{WEATHER_DIR}'")
    for key, val in MET_FILENAMES.items():
        lines.append(f"{key:<30} = '{val}'")
    lines.append(f"LH_MOISTURE_CONTENT            = {LH_MC:.1f}")
    lines.append(f"LW_MOISTURE_CONTENT            = {LW_MC:.1f}")
    lines.append("USE_BARRIERS = .TRUE.")
    lines.append("WS_AT_10M = .FALSE.")
    lines.append(f"BARRIER_FILENAME               = '{BARRIER_STEM}'")
    lines.append("/\n")

    lines.append("&OUTPUTS")
    lines.append(f"OUTPUTS_DIRECTORY    = '{OUTPUTS_DIR}'")
    lines.append(f"DTDUMP               = {DTDUMP:.1f}")
    lines.append("DUMP_TIME_OF_ARRIVAL = .TRUE.")
    lines.append("CONVERT_TO_GEOTIFF   = .TRUE.")
    lines.append("/\n")

    lines.append("&TIME_CONTROL")
    lines.append(f"SIMULATION_DT    = {SIMULATION_DT:.1f}")
    lines.append(f"TARGET_CFL       = {TARGET_CFL:.1f}")
    lines.append(f"SIMULATION_TSTOP = {tstop_sec:.1f}")
    lines.append(f"CURRENT_YEAR                   = {current_year:d}")
    lines.append(f"HOUR_OF_YEAR                   = {hour_of_year:d}")
    lines.append("/\n")

    lines.append("&SIMULATOR")
    lines.append("NUM_IGNITIONS = 1")
    lines.append(f"X_IGN(1)      = {x_ign:.2f}")
    lines.append(f"Y_IGN(1)      = {y_ign:.2f}")
    lines.append("T_IGN(1)      = 0.00")
    lines.append("DEBUG_LEVEL   = 0")
    lines.append("CLEAN_SCRATCH = .TRUE.")
    lines.append("/\n")

    lines.append("&MONTE_CARLO")
    lines.append(f"NUM_METEOROLOGY_TIMES = {num_meteorology_times:d}")

    lines.append("&MISCELLANEOUS")
    lines.append(f"PATH_TO_GDAL = '{PATH_TO_GDAL}'")
    lines.append(f"SCRATCH      = '{SCRATCH_DIR}'")
    lines.append("/\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-case processor
# ---------------------------------------------------------------------------

def _process_case(case_folder: Path, meta: dict) -> None:
    case_folder  = Path(case_folder).resolve()
    folder_name  = case_folder.name
    inputs_dir   = case_folder / INPUTS
    dem_path     = inputs_dir / "dem.tif"
    fuels_path   = inputs_dir / f"{cfg.LANDFIRE_BAND_FILE_NAMES[3]}.tif"   # fbfm40
    ign_shp      = case_folder / cfg.IGNITION_POINT_SHP_NAME

    if not case_folder.exists():
        print(f"  {case_folder} does not exist, skipping.")
        return

    (case_folder / cfg.ELMFIRE_SCRATCH_SUBDIR).mkdir(exist_ok=True)
    (case_folder / cfg.ELMFIRE_OUTPUTS_SUBDIR).mkdir(exist_ok=True)

    for path, label in ((dem_path, "DEM"), (ign_shp, "ignition shapefile"),
                        (fuels_path, "FBFM40")):
        if not path.exists():
            print(f"  {label} not found at {path}, skipping.")
            return

    # Parse times
    try:
        start_dt = pd.to_datetime(meta[START_COL])
        end_dt   = pd.to_datetime(meta[END_COL])
        if start_dt.tzinfo is not None:
            start_dt = start_dt.tz_convert("UTC").tz_localize(None)
        if end_dt.tzinfo is not None:
            end_dt = end_dt.tz_convert("UTC").tz_localize(None)
    except Exception as e:
        print(f"  Error parsing times: {e}")
        return

    if end_dt <= start_dt:
        print(f"  End <= start ({start_dt} / {end_dt}), skipping.")
        return

    tstop_sec = (end_dt - start_dt).total_seconds()
    epsg_str, cellsize, xll, yll = _compute_domain(dem_path)

    try:
        x_ign, y_ign = _ignition_xy_in_dem_crs(dem_path, ign_shp)
        print(f"  Ignition (raw, DEM CRS): X={x_ign:.2f}, Y={y_ign:.2f}")
    except Exception as e:
        print(f"  Error reading ignition point: {e}")
        return

    try:
        x_snap, y_snap, moved = _snap_to_valid_fuel(
            fuels_tif=fuels_path, x=x_ign, y=y_ign
        )
        if moved:
            print(f"  Snapped ignition to valid fuel: X={x_snap:.2f}, Y={y_snap:.2f}")
        else:
            print(f"  Ignition already on valid fuel: X={x_snap:.2f}, Y={y_snap:.2f}")
        x_ign, y_ign = x_snap, y_snap
    except Exception as e:
        print(f"  Error snapping ignition: {e}")
        return

    try:
        lat, lon = _latlon_from_dem_xy(dem_path, x_ign, y_ign)
        print(f"  Ignition (lat/lon): {lat:.6f}, {lon:.6f}")
    except Exception as e:
        print(f"  Error computing lat/lon: {e}")
        return

    current_year  = start_dt.year
    start_of_year = pd.Timestamp(year=current_year, month=1, day=1)
    hour_of_year  = int((start_dt - start_of_year).total_seconds() // 3600)

    ws_path = inputs_dir / cfg.WS_TIF_NAME
    with rasterio.open(ws_path) as ds:
        num_meteorology_times = ds.count

    text = _build_namelist(
        epsg_str=epsg_str,
        cellsize=cellsize,
        xll=xll,
        yll=yll,
        tstop_sec=tstop_sec,
        x_ign=x_ign,
        y_ign=y_ign,
        latitude=lat,
        longitude=lon,
        current_year=current_year,
        hour_of_year=hour_of_year,
        num_meteorology_times=num_meteorology_times,
    )

    out_path = case_folder / f"{folder_name}.data"
    out_path.write_text(text, encoding="utf-8")
    print(f"  Wrote {out_path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(case_dir=None) -> None:
    if case_dir is not None:
        case_dir = Path(case_dir)
        if any(case_dir.glob("*.data")):
            print(f"  Skipped — .data namelist already exists.")
            return
        meta = read_case_metadata(case_dir)
        _process_case(case_dir, meta)
        return

    df = pd.read_csv(FIRE_CSV)
    for col in (FOLDER_COL, START_COL, END_COL):
        if col not in df.columns:
            raise KeyError(f"Column '{col}' not found in {FIRE_CSV}")
    print(f"Loaded {len(df)} records from {FIRE_CSV}")

    root = Path(FIRE_ROOT)
    for _, row in df.iterrows():
        folder_name = f"{int(row[FOLDER_COL]):05d}"
        case_folder = (root / folder_name).resolve()
        print(f"\nCase: {folder_name}")
        _process_case(case_folder, row.to_dict())

    print("\nDone.")


if __name__ == "__main__":
    main()
