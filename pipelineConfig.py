# pipelineConfig.py
"""
Central configuration for the Elmfire validation pipeline.

All shared paths, filenames, tunable parameters, and column names live here so
that individual scripts only need to ``import pipelineConfig`` (or
``import pipelineConfig as cfg``) and never hard-code these values.

Sections
--------
1.  User-modifiable parameters   – thresholds, year range, parallelism
2.  Paths                        – network and scratch roots
3.  File / directory names       – CSV names, layer names, sub-folder names
4.  Summary CSV column names     – shared keys for the master DataFrames
5.  MTBS perimeters & USFS points – field names for raw input data
6.  LANDFIRE download & splitting – product lists, band order, raster names
7.  Satellite end-time detection  – tuning knobs for coverage algorithm
8.  Weather download (OpenMeteo)  – URL, model, column routing
9.  WindNinja                     – run-time settings for CLI invocation
10. Elmfire simulation            – namelist parameters (written to .data file)
11. Barrier file                  – road/water widths and rasterisation options
12. LFPS API                      – USGS LANDFIRE download service settings
13. Nelson dead-fuel model        – executable path
"""

import os
from pathlib import Path

# =============================================================================
# 1. USER-MODIFIABLE PARAMETERS
# =============================================================================

MTBS_AREA_THRESHOLD_ACRES   = 3000      # minimum burn area to include (acres)
MIN_FIRE_YEAR               = 2023      # earliest fire year to process
MAX_FIRE_YEAR               = 2025      # latest  fire year to process
DAY_TOLERANCE_DAYS          = 2         # ±days when matching perimeters to points
EXPAND                      = 1.5       # fractional bbox expansion for LANDFIRE download
LANDFIRE_EMAIL              = os.environ["LFPS_EMAIL"]  # set in shell: export LFPS_EMAIL=you@example.com
CONDITIONING_DAYS           = 20        # pre-ignition weather window (days)
MAX_PARALLEL_CASES          = 14        # cases to run simultaneously in runBatch.py
SETUP_PIPELINE_MAX_WORKERS  = 8         # parallel workers for getSatelliteEndTimes
MIN_HOURS_DURATION          = 12         # minimum valid fire duration (hours)
WINDNINJA_SOURCE            = "farsite"             # "install" (run WindNinja) | "farsite" (derive winds from FARSITE run)

# =============================================================================
# 2. PATHS
# =============================================================================

BASE_VALIDATION         = Path(r"/home/nick/elmfire_validation/")
BASE_DATA               = BASE_VALIDATION / "Data"
FIRE_ROOT_LOGIN_NODE    = BASE_VALIDATION / "FirePairs"   # setup output (login node)
FIRE_ROOT               = FIRE_ROOT_LOGIN_NODE # Path(r"/scratch/nick") / "FirePairs"  # HPC scratch location
INPUTS_DATA_ROOT        = BASE_DATA / "inputs"
PERIMETER_DATA_ROOT     = INPUTS_DATA_ROOT / "perimeters"
SATELLITES_ROOT         = INPUTS_DATA_ROOT / "satellites"
FARSITE_FB_DIR          = INPUTS_DATA_ROOT / "FB"   # root of the FireBehavior SDK folder
FARSITE_EXE_NAME        = "TestFARSITE.exe"         # executable name inside FB/bin/

# =============================================================================
# 3. FILE / DIRECTORY NAMES
# =============================================================================

FIRE_SUMMARY_CSV                = "fire_pairs_summary.csv"
FIRE_SUMMARY_WITH_SATELLITE_CSV = "fire_pairs_summary_with_satellite.csv"
IGNITION_POINT_SHP_NAME         = "ignition_point.gpkg"
BURN_SHAPE_NAME                 = "firescar.gpkg"
CASE_SAT_GPKG_NAME              = "satellite_points.gpkg"
LANDFIRE_ZIP_NAME               = "LANDFIRE.zip"
INPUTS_SUBDIR_NAME              = "inputs"

# Full paths for the master summary CSVs (live under FIRE_ROOT_LOGIN_NODE)
FIRE_SUMMARY_CSV_PATH     = FIRE_ROOT_LOGIN_NODE / FIRE_SUMMARY_CSV
FIRE_SUMMARY_SAT_CSV_PATH = FIRE_ROOT_LOGIN_NODE / FIRE_SUMMARY_WITH_SATELLITE_CSV

# =============================================================================
# 4. SUMMARY CSV COLUMN NAMES
# =============================================================================

COL_FOLDER              = "folder"              # zero-padded numeric case id
COL_POINT_DISCOVERY     = "point_discovery"
COL_POINT_FIREOUT       = "point_fireout"
COL_SATELLITE_IGNITION  = "SatelliteIgnitionTime"
COL_SATELLITE_END       = "SatelliteEndTime"
COL_SAT_CHAIN_END_TIME  = "SatelliteEnd_chain"
COL_SAT_END_AREA        = "SatelliteEnd_coverage"  # timestamp when area threshold reached
EVENT_END_COL           = "EventEndTime"            # min(SatelliteEndTime, point_fireout)

# Aliases used for wind/weather column routing
COL_IGNITION_TIME       = COL_POINT_DISCOVERY
WS_WD_FOLDER_COL        = COL_FOLDER
WS_WD_START_COL         = COL_SATELLITE_IGNITION
WS_WD_END_COL           = COL_SATELLITE_END

# =============================================================================
# 5. MTBS PERIMETERS & USFS IGNITION POINTS
# =============================================================================

MTBS_PERIMS_RAW             = PERIMETER_DATA_ROOT / "mtbs_perimeters.shp"
USFS_POINTS_RAW             = INPUTS_DATA_ROOT / "usfs_fire_points.geojson"

# These intermediate files are written to / read from FIRE_ROOT_LOGIN_NODE
MTBS_PERIMS_WITH_IGNITIONS  = FIRE_ROOT_LOGIN_NODE / "perimeters_ignitions.gpkg"
USFS_POINTS_MATCHED         = FIRE_ROOT_LOGIN_NODE / "all_ignitions.gpkg"

MTBS_ACRES_FIELD    = "BurnBndAc"
PERIM_NAME_FIELD    = "Incid_Name"
PERIM_DATE_FIELD    = "Ig_Date"         # polygon ignition date (date field in shapefile)
POINT_NAME_FIELD    = "FIRENAME"
POINT_DISC_FIELD    = "DISCOVERYDATETIME"
POINT_OUT_FIELD     = "FIREOUTDATETIME"

# =============================================================================
# 6. LANDFIRE DOWNLOAD & SPLITTING
# =============================================================================

# Band order MUST match the LFPS Layer_List used in getLandfireProductsForFireSim.py
LANDFIRE_BAND_FILE_NAMES = [
    "dem",      # ELEV2020  – elevation
    "slp",      # SLPD2020  – slope degrees
    "asp",      # ASP2020   – aspect degrees
    "fbfm40",   # FBFM40    – fuel model
    "cc",       # CC        – canopy cover
    "ch",       # CH        – canopy height
    "cbh",      # CBH       – canopy base height
    "cbd",      # CBD       – canopy bulk density
]

ADJ_FILE_NAME   = "adj.tif"
PHI_FILE_NAME   = "phi.tif"
BARRIER_FILE_NAME = "barrier.tif"
WS_TIF_NAME     = "ws.tif"
WD_TIF_NAME     = "wd.tif"

FMC_FILE_NAMES  = ["m1", "m10", "m100"]    # 1-hr, 10-hr, 100-hr fuel moisture

# Raster dtype / nodata used by adj, phi, and barrier outputs
RASTER_DTYPE    = "float32"
RASTER_NODATA   = -9999.0

# =============================================================================
# 7. SATELLITE END-TIME DETECTION
# =============================================================================

SATELLITE_GPKG          = SATELLITES_ROOT / "nasa_lance_allSatellites.gpkg"
SATELLITE_LAYER_NAME    = "output"
SAT_DATE_COL            = "ACQ_DATE"        # date column in satellite layer
SAT_TIME_COL            = "ACQ_TIME"        # HHMM string column
SAT_CHAIN_MAX_GAP_DAYS  = 7                 # max gap (days) in a continuous chain
SAT_HOTSPOT_BUFFER_DIST = 200               # hotspot buffer radius (m, in EPSG:5070)
COVERAGE_FRACTION       = 0.9               # fraction of effective burn area required
SAT_IGNITION_WINDOW_DAYS = 7               # search window after point ignition (days)
SAT_UNION_BLOCK_SIZE    = 64               # geometries per block in batched union
SAT_BUFFER_RESOLUTION   = 8               # shapely buffer resolution (segments per quadrant)

# =============================================================================
# 8. WEATHER DOWNLOAD (OpenMeteo ERA5)
# =============================================================================

WXS_FILE_NAME   = "weather.wxs"
OPENMETEO_URL   = "https://archive-api.open-meteo.com/v1/era5"
OPENMETEO_MODEL = "era5"

# =============================================================================
# 9. WINDNINJA
# =============================================================================

WINDNINJA_MODE              = "wxsFile"             # "wxsFile" (local WXS data) | "wxModel" (downloads forecast)
WINDNINJA_SUBDIR            = "windninja"           # subfolder under inputs/
WINDNINJA_CFG_FILENAME      = "windninja_config.cfg"
WINDNINJA_CONDA_ENV         = "base"                # conda environment with WindNinja_cli
WINDNINJA_WX_MODEL_TYPE     = "PASTCAST-GCP-HRRR-CONUS-3-KM"
WINDNINJA_TIME_ZONE         = "UTC"
WINDNINJA_MESH_UNITS        = "m"
WINDNINJA_OUTPUT_HEIGHT     = 10.0                  # m above ground
WINDNINJA_OUTPUT_HEIGHT_UNITS = "m"
WINDNINJA_MAX_WINDOW_DAYS   = 13    # WindNinja hard-fails above 14 days; 13 is safe
WINDNINJA_MESH_RESOLUTION_FACTOR = 4  # mesh_resolution = cellsize * this factor
WINDNINJA_NUM_THREADS       = 1     # CPU threads passed to WindNinja_cli (num_threads)

# =============================================================================
# 10. ELMFIRE SIMULATION
# =============================================================================

ELMFIRE_EXE             = "elmfire"
ELMFIRE_PATH_TO_GDAL    = "/home/nick/miniconda3/envs/elmfire/bin/"  # adjust per system
ELMFIRE_DT_METEOROLOGY  = 3600.0    # seconds between wx timesteps
ELMFIRE_DTDUMP          = 7200.0    # seconds between output dumps
ELMFIRE_SIMULATION_DT   = 30.0     # simulation time step (seconds)
ELMFIRE_TARGET_CFL      = 0.2
ELMFIRE_LH_MC           = 60.0     # live herbaceous moisture content (%)
ELMFIRE_LW_MC           = 90.0     # live woody moisture content (%)
ELMFIRE_OUTPUTS_SUBDIR  = "outputs"
ELMFIRE_SCRATCH_SUBDIR  = "scratch"

# =============================================================================
# 11. BARRIER FILE
# =============================================================================

ROADS_GPKG          = INPUTS_DATA_ROOT / "barriers" / "osm_conus_roads.gpkg"
ROADS_LAYER         = "lines"
WATER_GPKG          = INPUTS_DATA_ROOT / "barriers" / "grwl.gpkg"
WATER_LAYER         = "lines"
BACKUP_WATER_GPKG   = INPUTS_DATA_ROOT / "barriers" / "osm_conus_rivers.gpkg"

ROAD_CLASS_FIELD    = "highway"
WATER_CLASS_FIELD   = "waterway"
OSM_WIDTH_FIELD     = "width"
USE_OSM_WIDTH_TAG   = False
WRITE_TEMP_CLIPS    = True
KEEP_TEMP_CLIPS     = True
ALL_TOUCHED         = True

BARRIER_BACKUP_WATER_WIDTH_M = 5.0   # default width for backup river features (m)
BARRIER_BACKUP_MATCH_TOL_M   = 1.0   # tolerance to consider primary/backup as overlapping (m)

BARRIER_ROADS_CLIP_NAME   = "tmp_roads_clip.gpkg"
BARRIER_WATER_CLIP_NAME   = "tmp_waterways_clip.gpkg"
BARRIER_BACKUP_CLIP_NAME  = "tmp_backup_rivers_clip.gpkg"

ROAD_WIDTHS_M = {
    "motorway":    30.0,
    "trunk":       25.0,
    "primary":     16.0,
    "secondary":   12.0,
    "tertiary":    10.0,
    "residential":  8.0,
    "service":      6.0,
    "track":        4.0,
    "path":         2.0,
}
WATER_WIDTHS_M = {
    "river":  30.0,
    "stream":  6.0,
    "canal":  10.0,
    "ditch":   3.0,
    "drain":   2.0,
}

# =============================================================================
# 12. LFPS API (USGS LANDFIRE download service)
# =============================================================================

LFPS_BASE_API           = "https://lfps.usgs.gov"
LFPS_TERRAIN_PRODUCTS   = ["LF2020_Elev", "LF2020_SlpD", "LF2020_Asp"]  # always downloaded
LFPS_POLL_SLEEP_S       = 10    # seconds between job-status polls
LFPS_POLL_MAX_TRIES     = 300   # max polls before timeout (~50 min)
LFPS_CONCURRENT_JOBS    = 60     # concurrent LFPS jobs in prefetchLandfire.py

# =============================================================================
# 13. NELSON DEAD-FUEL MODEL
# =============================================================================

NELSON_EXE = BASE_DATA / "nelson_csharp" / "bin" / "Release" / "net8.0" / "nelson_csharp"
