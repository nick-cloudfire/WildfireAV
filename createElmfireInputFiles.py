"""
Create an Elmfire input file (<folder>.data) for each FirePairs case.

For each folder 00001, 00002, ... under FIRE_ROOT:
- Read DEM from inputs/dem.tif
- Infer:
    - A_SRS (EPSG)
    - COMPUTATIONAL_DOMAIN_CELLSIZE
    - COMPUTATIONAL_DOMAIN_XLLCORNER
    - COMPUTATIONAL_DOMAIN_YLLCORNER
- Read start/end time from FIRE_CSV (point_discovery, EventEndTime)
- Compute SIMULATION_TSTOP as (end - start) in seconds
- Read ignition point from ignition_point.gpkg in the case folder,
  reproject to DEM CRS if needed, and use as X_IGN(1), Y_IGN(1)
- Also write LATITUDE, LONGITUDE, CURRENT_YEAR, HOUR_OF_YEAR
  derived from ignition location and time.
- Write <folder>.data in the case folder (e.g. 00001.data)

Requirements:
    pip install rasterio pandas pyproj fiona
"""

from pathlib import Path
import datetime as dt
import pandas as pd
import rasterio
from rasterio.transform import Affine
from pyproj import Transformer
import fiona
import pipelineConfig as cfg
import os

# ==========================
# USER SETTINGS
# ==========================

FIRE_ROOT = cfg.FIRE_ROOT
FIRE_CSV = cfg.FIRE_SUMMARY_WITH_SATELLITE_CSV
FOLDER_COL = cfg.COL_FOLDER
START_COL = cfg.COL_SATELLITE_IGNITION
END_COL = cfg.EVENT_END_COL

# Fuels & topo filenames (without extension) used by Elmfire
FUELS_TOPO_FILENAMES = {
    "ASP_FILENAME":  "asp",
    "CBD_FILENAME":  "cbd",
    "CBH_FILENAME":  "cbh",
    "CC_FILENAME":   "cc",
    "CH_FILENAME":   "ch",
    "DEM_FILENAME":  "dem",
    "FBFM_FILENAME": "fbfm40",
    "SLP_FILENAME":  "slp",
    "ADJ_FILENAME":  "adj",
    "PHI_FILENAME":  "phi",
}

# Meteorology filenames (without extension) in inputs/
MET_FILENAMES = {
    "WS_FILENAME":   "ws",
    "WD_FILENAME":   "wd",
    "M1_FILENAME":   "m1",
    "M10_FILENAME":  "m10",
    "M100_FILENAME": "m100",
}


# Moisture contents (match lh.tif = 60, lw.tif = 90)
LH_MC = 60.0
LW_MC = 90.0

# Elmfire fixed settings
DT_METEOROLOGY = 3600.0   # seconds between wx timesteps
DTDUMP = 3600.0
SIMULATION_DT = 30.0
TARGET_CFL = 0.2

# Directories Elmfire will use (relative to each case folder)
FUELS_AND_TOPO_DIR = "./inputs"         # where dem/asp/etc live
WEATHER_DIR = "./inputs"                # where ws/wd/m1/m10/m100 live
OUTPUTS_DIR = "./outputs"
SCRATCH_DIR = "./scratch"

# Path to GDAL (adjust for your system)
PATH_TO_GDAL = "/home/nick/miniconda3/envs/elmfire/bin/"


# ==========================
# HELPER FUNCTIONS
# ==========================

def compute_domain_from_dem(dem_path: Path):
    """
    From a DEM, compute:
        epsg_str (e.g. 'EPSG: 5070'),
        cellsize (float),
        xllcorner,
        yllcorner
    """
    with rasterio.open(dem_path) as ds:
        transform: Affine = ds.transform
        width = ds.width
        height = ds.height

        cellsize_x = transform.a
        cellsize_y = transform.e
        cellsize = abs(cellsize_x)  # assume square pixels

        x_ul = transform.c
        y_ul = transform.f

        xll = x_ul
        yll = y_ul + (cellsize_y * height)  # cellsize_y is typically negative

        epsg = None
        if ds.crs is not None:
            epsg = ds.crs.to_epsg()

        epsg_str = f"EPSG: {epsg}" if epsg is not None else "EPSG: UNKNOWN"

    return epsg_str, cellsize, xll, yll


def get_ignition_xy_from_shp(dem_path: Path, shp_path: Path):
    """
    Read ignition point from shapefile and return coordinates in DEM CRS.

    - Takes the first feature from ignition_point.gpkg
    - If CRS differs from DEM, reprojects to DEM CRS
    """
    if not shp_path.exists():
        raise FileNotFoundError(f"Ignition shapefile not found: {shp_path}")

    with rasterio.open(dem_path) as ds:
        dem_crs = ds.crs

    if dem_crs is None:
        raise ValueError(f"DEM at {dem_path} has no CRS; cannot transform ignition.")

    with fiona.open(shp_path, "r") as src:
        shp_crs = src.crs  # dict
        try:
            feat = next(iter(src))
        except StopIteration:
            raise ValueError(f"No features in ignition shapefile: {shp_path}")

        geom = feat["geometry"]
        if geom is None or geom["type"] != "Point":
            raise ValueError(f"Ignition geometry must be a Point in {shp_path}")

        x_src, y_src = geom["coordinates"]

    # If CRSs are compatible, transform if needed
    if shp_crs is None or dem_crs is None:
        # assume already in DEM CRS
        return float(x_src), float(y_src)

    transformer = Transformer.from_crs(shp_crs, dem_crs, always_xy=True)
    x_dem, y_dem = transformer.transform(x_src, y_src)
    return float(x_dem), float(y_dem)


def get_ignition_latlon(shp_path: Path):
    """
    Read ignition point from shapefile and return (lat, lon) in EPSG:4326.

    - Takes the first feature from ignition_point.gpkg
    - If CRS is not EPSG:4326, reprojects to EPSG:4326
    """
    if not shp_path.exists():
        raise FileNotFoundError(f"Ignition shapefile not found: {shp_path}")

    with fiona.open(shp_path, "r") as src:
        shp_crs = src.crs  # dict
        try:
            feat = next(iter(src))
        except StopIteration:
            raise ValueError(f"No features in ignition shapefile: {shp_path}")

        geom = feat["geometry"]
        if geom is None or geom["type"] != "Point":
            raise ValueError(f"Ignition geometry must be a Point in {shp_path}")

        x_src, y_src = geom["coordinates"]

    # If shapefile CRS is missing, assume it's already lat/lon WGS84
    if shp_crs is None:
        lon = float(x_src)
        lat = float(y_src)
        return lat, lon

    # Reproject to EPSG:4326 (WGS84 lon/lat)
    transformer = Transformer.from_crs(shp_crs, "EPSG:4326", always_xy=True)
    lon, lat = transformer.transform(x_src, y_src)
    return float(lat), float(lon)


def build_elmfire_input_text(
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
):
    """
    Construct the Elmfire namelist text as a single string.
    """
    lines = []

    # &INPUTS block
    lines.append("&INPUTS")
    lines.append(f"FUELS_AND_TOPOGRAPHY_DIRECTORY = '{FUELS_AND_TOPO_DIR}'")
    for key, val in FUELS_TOPO_FILENAMES.items():
        lines.append(f"{key:<30} = '{val}'")
    lines.append(f"DT_METEOROLOGY                 = {DT_METEOROLOGY:.1f}")
    lines.append(f"WEATHER_DIRECTORY              = '{WEATHER_DIR}'")
    for key, val in MET_FILENAMES.items():
        lines.append(f"{key:<30} = '{val}'")
    lines.append(f"LH_MOISTURE_CONTENT            = {LH_MC:.1f}")
    lines.append(f"LW_MOISTURE_CONTENT            = {LW_MC:.1f}")
    lines.append("USE_BARRIERS = .TRUE.")
    lines.append(f"BARRIER_FILENAME = '{cfg.BARRIER_FILE_NAME.removesuffix('.tif')}'")
    lines.append("/\n")

    # &OUTPUTS block
    lines.append("&OUTPUTS")
    lines.append(f"OUTPUTS_DIRECTORY    = '{OUTPUTS_DIR}'")
    lines.append(f"DTDUMP               = {DTDUMP:.1f}")
    lines.append("DUMP_TIME_OF_ARRIVAL = .TRUE.")
    lines.append("CONVERT_TO_GEOTIFF   = .TRUE.")
    lines.append("/\n")

    # &COMPUTATIONAL_DOMAIN block
    lines.append("&COMPUTATIONAL_DOMAIN")
    lines.append(f"A_SRS                          = '{epsg_str}'")
    lines.append(f"COMPUTATIONAL_DOMAIN_CELLSIZE  = {cellsize:.2f}")
    lines.append(f"COMPUTATIONAL_DOMAIN_XLLCORNER = {xll:.2f}")
    lines.append(f"COMPUTATIONAL_DOMAIN_YLLCORNER = {yll:.2f}")
    lines.append("/\n")

    # &TIME_CONTROL block
    lines.append("&TIME_CONTROL")
    lines.append(f"SIMULATION_DT    = {SIMULATION_DT:.1f}")
    lines.append(f"TARGET_CFL       = {TARGET_CFL:.1f}")
    lines.append(f"SIMULATION_TSTOP = {tstop_sec:.1f}")
    lines.append(f"LATITUDE                       = {latitude:.6f}")
    lines.append(f"LONGITUDE                      = {longitude:.6f}")
    lines.append(f"CURRENT_YEAR                   = {current_year:d}")
    lines.append(f"HOUR_OF_YEAR                   = {hour_of_year:d}")
    lines.append("/\n")

    # &SIMULATOR block
    lines.append("&SIMULATOR")
    lines.append("NUM_IGNITIONS = 1")
    lines.append(f"X_IGN(1)      = {x_ign:.2f}")
    lines.append(f"Y_IGN(1)      = {y_ign:.2f}")
    lines.append("T_IGN(1)      = 0.00")
    lines.append("DEBUG_LEVEL   = 20")
    lines.append("/\n")

    # &MISCELLANEOUS block
    lines.append("&MISCELLANEOUS")
    lines.append(f"PATH_TO_GDAL                   = '{PATH_TO_GDAL}'")
    lines.append(f"SCRATCH                        = '{SCRATCH_DIR}'")
    lines.append("/\n")

    return "\n".join(lines)


# ==========================
# MAIN
# ==========================

def main():
    root = Path(FIRE_ROOT)
    df = pd.read_csv(FIRE_CSV)

    # Check required columns
    for col in (FOLDER_COL, START_COL, END_COL):
        if col not in df.columns:
            raise KeyError(f"Column '{col}' not found in {FIRE_CSV}")

    print(f"Loaded {len(df)} records from {FIRE_CSV}\n")

    for idx, row in df.iterrows():
        folder_id = int(row[FOLDER_COL])
        folder_name = f"{folder_id:05d}"
        case_folder = (root / folder_name).resolve()
        inputs_folder = case_folder / "inputs"
        scratch_folder = case_folder / "scratch"
        output_folder = case_folder / "outputs"
        dem_path = inputs_folder / "dem.tif"
        ign_shp = case_folder / "ignition_point.gpkg"

        print(f"[{idx}] Case folder: {case_folder}")
        
        if not scratch_folder.exists():
            os.mkdir(scratch_folder)
            
        if not output_folder.exists():
            os.mkdir(output_folder)

        if not dem_path.exists():
            print(f"  DEM not found at {dem_path}, skipping.")
            continue

        if not ign_shp.exists():
            print(f"  Ignition shapefile not found at {ign_shp}, skipping.")
            continue

        # Parse start/end times to define simulation length
        start_raw = row[START_COL]
        end_raw = row[END_COL]
        try:
            start_dt = pd.to_datetime(start_raw)
            end_dt = pd.to_datetime(end_raw)

            # If timezone-aware, convert to naive UTC
            if start_dt.tzinfo is not None:
                start_dt = start_dt.tz_convert("UTC").tz_localize(None)
            if end_dt.tzinfo is not None:
                end_dt = end_dt.tz_convert("UTC").tz_localize(None)
        except Exception as e:
            print(f"  Error parsing times '{start_raw}' / '{end_raw}': {e}")
            continue

        if end_dt <= start_dt:
            print(f"  End <= start ({start_dt} / {end_dt}), skipping.")
            continue

        tstop_sec = (end_dt - start_dt).total_seconds()

        # Domain info from DEM
        epsg_str, cellsize, xll, yll = compute_domain_from_dem(dem_path)

        # Ignition point in DEM CRS for X_IGN / Y_IGN
        try:
            x_ign, y_ign = get_ignition_xy_from_shp(dem_path, ign_shp)
            print(f"  Ignition (DEM CRS): X={x_ign:.2f}, Y={y_ign:.2f}")
        except Exception as e:
            print(f"  Error reading ignition point (DEM CRS): {e}")
            continue

        # Ignition point in lat/lon for LATITUDE / LONGITUDE
        try:
            lat, lon = get_ignition_latlon(ign_shp)
            print(f"  Ignition (lat/lon): lat={lat:.6f}, lon={lon:.6f}")
        except Exception as e:
            print(f"  Error reading ignition lat/lon: {e}")
            continue

        # CURRENT_YEAR and HOUR_OF_YEAR from ignition time
        current_year = start_dt.year
        print(current_year)
        start_of_year = pd.Timestamp(year=current_year, month=1, day=1)
        hour_of_year = int((start_dt - start_of_year).total_seconds() // 3600)

        # Build input text
        elmfire_text = build_elmfire_input_text(
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
        )

        # Save as 00001.data, 00002.data, etc.
        out_path = case_folder / f"{folder_name}.data"
        out_path.write_text(elmfire_text, encoding="utf-8")
        print(f"  Wrote {out_path}\n")

    print("Done.")


if __name__ == "__main__":
    main()