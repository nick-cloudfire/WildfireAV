# pipeline_config.py
"""
Central configuration for the FirePairs/FlamMap/Elmfire pipeline.

All shared paths, filenames, and key column names live here so that:
- You only change things in one place.
- Individual scripts can `from pipeline_config import ...`
- The main driver (run_pipeline.py) can also rely on the same values.
"""

from pathlib import Path

# -----------------------------------------------------------------------------
# USER MODIFIABLE VARIABLES
# -----------------------------------------------------------------------------

MTBS_AREA_THRESHOLD_ACRES   = 2000
MIN_FIRE_YEAR               = 2020
MAX_FIRE_YEAR               = 2025
DAY_TOLERANCE_DAYS          = 2
EXPAND                      = 0.8
LANDFIRE_EMAIL              = "nick@cloudfire.com"
CONDITIONING_DAYS           = 20
MAX_WORKERS                 = 12
MIN_HOURS_DURATION          = 3

# -----------------------------------------------------------------------------
# PATHS
# -----------------------------------------------------------------------------

BASE_VALIDATION         = Path(r"/home/nick/elmfire_validation")
BASE_DATA               = BASE_VALIDATION / "Data"
FIRE_ROOT               = BASE_VALIDATION / "FirePairs"
PERIMETER_DATA_ROOT     = BASE_DATA / "Perimeter Data"
SATELLITES_ROOT         = BASE_DATA / "satellites"
BIN_ROOT                = BASE_DATA / "bin"

# -----------------------------------------------------------------------------
# FILES
# -----------------------------------------------------------------------------

FIRE_SUMMARY_CSV                    = FIRE_ROOT / "fire_pairs_summary.csv"
FIRE_SUMMARY_WITH_SATELLITE_CSV     = FIRE_ROOT / "fire_pairs_summary_with_satellite.csv"
IGNITION_POINT_SHP_NAME             = "ignition_point.gpkg"
LANDFIRE_ZIP_NAME                   = "LANDFIRE.zip"

# -----------------------------------------------------------------------------
# SUMMARY FILE COLUMN NAMES
# -----------------------------------------------------------------------------

COL_FOLDER              = "folder"               # numeric folder id (1, 2, 3, ...)
COL_POINT_DISCOVERY     = "point_discovery"
COL_POINT_FIREOUT       = "point_fireout"
COL_SATELLITE_END       = "SatelliteEndTime"
EVENT_END_COL           = "EventEndTime"     # <-- new final end column

# -----------------------------------------------------------------------------
# MTBS PERIMETERS & USFS IGNITION POINTS
# -----------------------------------------------------------------------------

MTBS_PERIMS_RAW             = PERIMETER_DATA_ROOT / "mtbs_perims_DD.shp"
MTBS_PERIMS_FILTERED        = PERIMETER_DATA_ROOT / "perimeters.gpkg"
USFS_POINTS_RAW             = BASE_DATA / "National_USFS_Fire_Occurrence_Point_(Feature_Layer).geojson"
USFS_POINTS_MATCHED         = FIRE_ROOT / "all_ignitions.gpkg"
MTBS_PERIMS_WITH_IGNITIONS  = FIRE_ROOT / "perimeters_ignitions.gpkg"

MTBS_ACRES_FIELD    = "BurnBndAc"

PERIM_NAME_FIELD    = "Incid_Name"
PERIM_DATE_FIELD    = "Ig_Date"  # polygon ignition date (date in shapefile)

POINT_NAME_FIELD    = "FIRENAME"
POINT_DISC_FIELD    = "DISCOVERYDATETIME"  # datetime
POINT_OUT_FIELD     = "FIREOUTDATETIME"     # datetime

# -----------------------------------------------------------------------------
# LANDFIRE DOWNLOAD & SPLITTING
# -----------------------------------------------------------------------------

INPUTS_SUBDIR_NAME          = "inputs"
LANDFIRE_BAND_FILE_NAMES    = [
                                "dem",     # ELEV2020
                                "slp",     # SLPD2020
                                "asp",     # ASP2020
                                "fbfm40",  # FBFM40
                                "cc",      # canopy cover
                                "ch",      # canopy height
                                "cbh",     # canopy base height
                                "cbd",     # canopy bulk density
                            ]
ADJ_FILE_NAME               = "adj.tif"
PHI_FILE_NAME               = "phi.tif"
BARRIER_FILE_NAME           = "barrier.tif"

# -----------------------------------------------------------------------------
# MOISTURE RASTERS
# -----------------------------------------------------------------------------

FMC_FILE_NAMES      = ["m1", "m10", "m100"]

# -----------------------------------------------------------------------------
# SATELLITE END TIMES
# -----------------------------------------------------------------------------

SATELLITE_GPKG              = SATELLITES_ROOT / "Clipped" / "clipped.gpkg"
CASE_SAT_GPKG_NAME          = "satellite_points.gpkg"
SATELLITE_LAYER_NAME        = "output"
SAT_DATE_COL                = "ACQ_DATE"   # Date type
SAT_TIME_COL                = "ACQ_TIME"   # String / HHMM
BURN_SHAPE_NAME             = "firescar.gpkg"
SAT_CHAIN_MAX_GAP_DAYS      = 7
SAT_HOTSPOT_BUFFER_DIST     = 200 #m
COVERAGE_FRACTION           = 0.9  # 90% of true burn area
COL_IGNITION_TIME           = "point_discovery"   # <-- set to your actual CSV column name
COL_SATELLITE_IGNITION      = "SatelliteIgnitionTime"
COL_SAT_CHAIN_END_TIME      = "SatelliteEnd_chain"
COL_SAT_END_AREA            = "SatelliteEnd_coverage"
SAT_IGNITION_WINDOW_DAYS    = 7

# -----------------------------------------------------------------------------
# WS / WD WIND FILES FROM OPEN-METEO
# -----------------------------------------------------------------------------

WS_WD_FOLDER_COL    = COL_FOLDER
WS_WD_START_COL     = COL_SATELLITE_IGNITION
WS_WD_END_COL       = COL_SATELLITE_END
WS_TIF_NAME         = "ws.tif"
WD_TIF_NAME         = "wd.tif"
OPENMETEO_URL       = "https://archive-api.open-meteo.com/v1/era5"
OPENMETEO_MODEL     = "era5"

# -----------------------------------------------------------------------------
# WEATHER.WXS DOWNLOAD (40-DAY PRE-IGNITION)
#   (downloadWeatherData.py)
# -----------------------------------------------------------------------------

WXS_FILE_NAME       = "weather.wxs"


# -----------------------------------------------------------------------------
# BARRIER FILE PARAMETERS
# -----------------------------------------------------------------------------

ROADS_GPKG          = BASE_DATA / "Barriers" / "us_roads.gpkg"
ROADS_LAYER         = "lines"
WATER_GPKG          = BASE_DATA / "Barriers" / "waterways.gpkg"
WATER_LAYER         = "lines"
BACKUP_WATER_SHP    = BASE_DATA / "Barriers" / "us_rivers.shp"
DTYPE               = "float32"
NODATA              = -9999
ROAD_CLASS_FIELD    = "highway"
WATER_CLASS_FIELD   = "waterway"
OSM_WIDTH_FIELD     = "width"
USE_OSM_WIDTH_TAG   = False
WRITE_TEMP_CLIPS    = True
KEEP_TEMP_CLIPS     = True
ALL_TOUCHED         = True
ROAD_WIDTHS_M       = {
                        "motorway": 30.0,
                        "trunk": 25.0,
                        "primary": 16.0,
                        "secondary": 12.0,
                        "tertiary": 10.0,
                        "residential": 8.0,
                        "service": 6.0,
                        "track": 4.0,
                        "path": 2.0,
                    }
WATER_WIDTHS_M      = {
                        "river": 30.0,
                        "stream": 6.0,
                        "canal": 10.0,
                        "ditch": 3.0,
                        "drain": 2.0,
                    }


