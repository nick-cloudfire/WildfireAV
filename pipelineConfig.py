# pipelineConfig.py
"""
Central configuration for the Elmfire validation pipeline.

All shared paths, filenames, tunable parameters, and column names live here so
that individual scripts only need to ``import pipelineConfig`` (or
``import pipelineConfig as cfg``) and never hard-code these values.

Sections
--------
1.  User-modifiable parameters   – thresholds, year range, parallelism
2.  Paths                        – roots and every derived file path
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

Machine-specific settings
-------------------------
Put only the lines that differ in ``pipelineConfig_local.py`` next to this file
(gitignored), e.g. on HPC::

    from pathlib import Path
    FIRE_ROOT          = Path("/scratch/nick/FirePairs")
    MAX_PARALLEL_CASES = 96

Any scalar setting can also be set per job with an environment variable
``WAV_<NAME>`` (e.g. ``export WAV_FIRE_ROOT=/scratch/$USER/FirePairs``).
Environment beats the local file, which beats the defaults here.  The paths in
section 2 are derived *after* the overrides, so changing a root (FIRE_ROOT,
INPUTS_DATA_ROOT) moves everything under it.  ``python pipeline/preflight.py``
reports the active overrides and flags local keys this file no longer knows.
"""

import os
import shutil
import sys
from pathlib import Path

# =============================================================================
# 1. USER-MODIFIABLE PARAMETERS
# =============================================================================

MTBS_AREA_THRESHOLD_ACRES   = 5000      # minimum burn area to include (acres)
MIN_FIRE_YEAR               = 2024      # earliest fire year to process
MAX_FIRE_YEAR               = 2025      # latest  fire year to process
DAY_TOLERANCE_DAYS          = 2         # ±days when matching perimeters to points
EXPAND                      = 1.5       # fractional bbox expansion for LANDFIRE download
LANDFIRE_EMAIL              = os.environ.get("LFPS_EMAIL", "")  # export LFPS_EMAIL=you@example.com (checked by preflight)
CONDITIONING_DAYS           = 20        # pre-ignition weather window (days)
MAX_PARALLEL_CASES          = 0         # cases at once in runBatch (0 = one per available CPU)
BATCH_HEARTBEAT_MIN         = 10        # minutes between batch progress heartbeats (0 = off)
PREFLIGHT_MIN_FREE_GB       = 50        # warn when FIRE_ROOT has less free disk than this
SETUP_PIPELINE_MAX_WORKERS  = 0         # parallel workers for getSatelliteEndTimes (0 = available CPUs)
MIN_HOURS_DURATION          = 12         # minimum valid fire duration (hours)
WINDNINJA_SOURCE            = "install"             # "install" (run WindNinja) | "farsite" (derive winds from FARSITE run)

# Wall-clock limits for external programs (hours, 0 = none).  A hung program is
# killed and the case fails at that step instead of holding a worker until the
# job's time limit.  Set generously: a real large fire must not hit them.
ELMFIRE_TIMEOUT_H           = 24
FARSITE_TIMEOUT_H           = 24
WINDNINJA_TIMEOUT_H         = 12        # per WindNinja run (one time chunk)
NELSON_TIMEOUT_H            = 4
GDAL_TIMEOUT_H              = 2         # gdal_translate / ogr2ogr / gdalwarp calls

# =============================================================================
# 2. PATHS
# =============================================================================

BASE_DATA               = Path(__file__).resolve().parent   # this repository
BASE_VALIDATION         = BASE_DATA.parent
FARSITE_FB_DIR          = Path.home() / "farsite" / "bin"  # Wine FARSITE binary directory
FARSITE_EXE_NAME        = "TestFARSITE.exe"               # Wine executable name

# Derived paths: evaluated after the local/env overrides (bottom of this file),
# so they follow the roots.  Each one can still be overridden on its own.
_DERIVED_PATHS = {
    # roots
    "FIRE_ROOT":                  lambda: BASE_VALIDATION / "FirePairs",   # case folders + summaries (HPC: scratch)
    "INPUTS_DATA_ROOT":           lambda: BASE_DATA / "inputs",            # national input datasets
    # setup inputs
    "MTBS_PERIMS_RAW":            lambda: INPUTS_DATA_ROOT / "mtbs_perimeters.gpkg",
    "USFS_POINTS_RAW":            lambda: INPUTS_DATA_ROOT / "NIFC_FOD.gpkg",
    "SATELLITE_GPKG":             lambda: INPUTS_DATA_ROOT / "nasa_lance_allSatellites.gpkg",
    # barrier inputs
    "ROADS_GPKG":                 lambda: INPUTS_DATA_ROOT / "osm_conus_roads.gpkg",
    "WATER_GPKG":                 lambda: INPUTS_DATA_ROOT / "grwl.gpkg",
    "BACKUP_WATER_GPKG":          lambda: INPUTS_DATA_ROOT / "osm_conus_rivers.gpkg",
    # setup outputs, next to the case folders
    "FIRE_SUMMARY_CSV_PATH":      lambda: FIRE_ROOT / FIRE_SUMMARY_CSV,
    "FIRE_SUMMARY_SAT_CSV_PATH":  lambda: FIRE_ROOT / "fire_pairs_summary_with_satellite.csv",
    "MTBS_PERIMS_WITH_IGNITIONS": lambda: FIRE_ROOT / "perimeters_ignitions.gpkg",
    "USFS_POINTS_MATCHED":        lambda: FIRE_ROOT / "all_ignitions.gpkg",
    # Nelson dead-fuel model (built with `dotnet publish -c Release`)
    "NELSON_EXE":                 lambda: BASE_DATA / "nelson_csharp" / "bin" / "Release" / "net8.0" / "nelson_csharp",
}

# =============================================================================
# 3. FILE / DIRECTORY NAMES
# =============================================================================

FIRE_SUMMARY_CSV                = "fire_pairs_summary.csv"
IGNITION_POINT_SHP_NAME         = "ignition_point.gpkg"
BURN_SHAPE_NAME                 = "firescar.gpkg"
CASE_SAT_GPKG_NAME              = "satellite_points.gpkg"
LANDFIRE_ZIP_NAME               = "LANDFIRE.zip"
INPUTS_SUBDIR_NAME              = "inputs"

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
WS_WD_START_COL         = COL_SATELLITE_IGNITION
WS_WD_END_COL           = COL_SATELLITE_END

# =============================================================================
# 5. MTBS PERIMETERS & USFS IGNITION POINTS
# =============================================================================

# File paths: see section 2.
MTBS_ACRES_FIELD    = "BurnBndAc"
PERIM_NAME_FIELD    = "Incid_Name"
PERIM_DATE_FIELD    = "Ig_Date"         # polygon ignition date (date field in shapefile)
POINT_NAME_FIELD    = "IncidentName"
POINT_DISC_FIELD    = "FireDiscoveryDateTime"
POINT_OUT_FIELD     = "FireOutDateTime"

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

SATELLITE_LAYER_NAME    = "output"
SAT_DATE_COL            = "ACQ_DATE"        # date column in satellite layer
SAT_TIME_COL            = "ACQ_TIME"        # HHMM string column
SAT_CHAIN_MAX_GAP_DAYS  = 7                 # max gap (days) in a continuous chain
SAT_HOTSPOT_BUFFER_DIST = 200               # hotspot buffer radius (m, in EPSG:5070)
COVERAGE_FRACTION       = 0.9               # fraction of effective burn area required
SAT_IGNITION_WINDOW_DAYS = 7               # search window after point ignition (days)
SAT_UNION_BLOCK_SIZE    = 64               # geometries per block in batched union
SAT_BUFFER_RESOLUTION   = 8               # shapely buffer quad_segs (segments per quadrant)

# =============================================================================
# 8. WEATHER DOWNLOAD (OpenMeteo ERA5)
# =============================================================================

WXS_FILE_NAME   = "weather.wxs"
OPENMETEO_URL   = "https://archive-api.open-meteo.com/v1/era5"
OPENMETEO_MODEL = "era5"

# =============================================================================
# 9. WINDNINJA
# =============================================================================

WINDNINJA_MODE              = "hrrrLocal"           # "hrrrLocal" (HRRR downloaded locally) | "wxsFile" (domain-average from WXS) | "wxModel" (WindNinja downloads pastcast)
WINDNINJA_SUBDIR            = "windninja"           # subfolder under inputs/
WINDNINJA_CFG_FILENAME      = "windninja_config.cfg"
WINDNINJA_CONDA_ENV         = None                  # None: WindNinja_cli from PATH (active env); or a conda env name for `conda run -n`
WINDNINJA_WX_MODEL_TYPE     = "PASTCAST-GCP-HRRR-CONUS-3-KM"
WINDNINJA_TIME_ZONE         = "UTC"
WINDNINJA_MESH_UNITS        = "m"
WINDNINJA_OUTPUT_HEIGHT     = 20.0                  # m above ground
WINDNINJA_OUTPUT_HEIGHT_UNITS = "ft"
WINDNINJA_MAX_WINDOW_DAYS   = 13    # WindNinja hard-fails above 14 days; 13 is safe
WINDNINJA_MESH_RESOLUTION_FACTOR = 4  # mesh_resolution = cellsize * this factor
WINDNINJA_NUM_THREADS       = 1     # CPU threads passed to WindNinja_cli (num_threads)

# HRRR local download (WINDNINJA_MODE = "hrrrLocal")
HRRR_LOCAL_SUBDIR           = "hrrr"    # under inputs/windninja/; deleted with the rest of the workspace
HRRR_BASE_URL               = "https://storage.googleapis.com/high-resolution-rapid-refresh"
HRRR_BUFFER_FRACTION        = 0.20      # DEM bbox padding per side before cropping HRRR
HRRR_MIN_BUFFER_M           = 6000.0    # minimum padding (m); HRRR cells are 3 km
HRRR_DOWNLOAD_THREADS       = 4         # concurrent hours per case (keep low when running many cases)
HRRR_DOWNLOAD_RETRIES       = 4
HRRR_DOWNLOAD_TIMEOUT_S     = 120

# =============================================================================
# 10. ELMFIRE SIMULATION
# =============================================================================

ELMFIRE_EXE             = "elmfire"
# GDAL binaries ELMFIRE calls: the running env's bin/ if it has them, else whatever is on PATH
ELMFIRE_PATH_TO_GDAL    = str(Path(sys.prefix) / "bin" if (Path(sys.prefix) / "bin" / "gdal_translate").exists()
                              else Path(shutil.which("gdal_translate") or "/usr/bin/gdal_translate").parent) + "/"
ELMFIRE_DT_METEOROLOGY  = 3600.0    # seconds between wx timesteps
ELMFIRE_DTDUMP          = 7200.0    # seconds between output dumps
ELMFIRE_SIMULATION_DT   = 30.0     # simulation time step (seconds)
ELMFIRE_TARGET_CFL      = 0.2
LIVE_HERB_MC            = 60.0     # live herbaceous moisture content (%) — used by ELMFIRE and FARSITE
LIVE_WOODY_MC           = 90.0     # live woody moisture content (%)       — used by ELMFIRE and FARSITE
ELMFIRE_OUTPUTS_SUBDIR  = "outputs"
ELMFIRE_SCRATCH_SUBDIR  = "scratch"

# =============================================================================
# 11. BARRIER FILE
# =============================================================================

ROADS_LAYER         = "lines"
WATER_LAYER         = "lines"

ROAD_CLASS_FIELD    = "highway"
WATER_CLASS_FIELD   = "waterway"
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
LFPS_POLL_SLEEP_S           = 10    # seconds between job-status polls
LFPS_POLL_HEARTBEAT_S       = 1800  # log "still waiting" every N seconds (0 = disable)
LFPS_JOB_TIMEOUT_H          = 0     # give up on one queued job after N hours (0 = poll forever)
LFPS_MAX_ATTEMPTS           = 3     # submit → poll → download attempts per case
LFPS_CONCURRENT_JOBS        = 60    # concurrent LFPS jobs in prefetchLandfire.py

# =============================================================================
# 13. NELSON DEAD-FUEL MODEL
# =============================================================================

# NELSON_EXE: see section 2.

# =============================================================================
# OVERRIDES  (pipelineConfig_local.py, then WAV_<NAME> env vars) — see top
# =============================================================================

_KNOWN = {k for k in globals() if k.isupper()} | set(_DERIVED_PATHS)

try:
    import pipelineConfig_local as _local
    LOCAL_OVERRIDES = {k: v for k, v in vars(_local).items() if k.isupper()}
except ImportError:
    LOCAL_OVERRIDES = {}
UNKNOWN_LOCAL_KEYS = sorted(set(LOCAL_OVERRIDES) - _KNOWN)   # stale/typo'd keys (reported by preflight)
globals().update(LOCAL_OVERRIDES)


def _from_env(name: str, raw: str):
    default = globals().get(name)
    if name in _DERIVED_PATHS or isinstance(default, Path):
        return Path(raw)
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, (int, float)):
        return type(default)(raw)
    return raw


ENV_OVERRIDES = {k: _from_env(k, os.environ[f"WAV_{k}"]) for k in sorted(_KNOWN) if f"WAV_{k}" in os.environ}
globals().update(ENV_OVERRIDES)

for _name, _make in _DERIVED_PATHS.items():
    globals()[_name] = Path(globals()[_name] if _name in LOCAL_OVERRIDES or _name in ENV_OVERRIDES else _make())


def _available_cpus() -> int:
    """CPUs this job may use: the SLURM allocation, else the process's CPU affinity."""
    slurm = os.environ.get("SLURM_CPUS_PER_TASK", "")
    if slurm.isdigit():
        return int(slurm)
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:   # macOS / Windows
        return os.cpu_count() or 1


AVAILABLE_CPUS = _available_cpus()
MAX_PARALLEL_CASES = MAX_PARALLEL_CASES or AVAILABLE_CPUS
SETUP_PIPELINE_MAX_WORKERS = SETUP_PIPELINE_MAX_WORKERS or AVAILABLE_CPUS
