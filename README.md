# Elmfire Validation Pipeline

Automated end-to-end pipeline that matches historical MTBS burn perimeters to
USFS ignition points, downloads all required geospatial inputs, runs the
[WindNinja](https://github.com/firelab/windninja) wind model and the Nelson
dead-fuel-moisture model, and executes [Elmfire](https://github.com/lautenberger/elmfire)
to produce time-of-arrival rasters for validation.

---

## Repository layout

```
Data/
├── pipelineConfig.py               ← single source of truth for all settings
├── parallel_api.py                 ← shared utilities (Tee, logging, retries, parallelism)
├── case_metadata.py                ← read/write per-case JSON metadata

# Setup scripts (run once locally)
├── setupPipeline.py                ← orchestrates steps 1-5 below
├── processScarsAndPoints.py        ← step 1: match perimeters to ignition points
├── separateScarsAndPointsToCases.py ← step 2: create numbered case folders
├── getSatelliteEndTimes.py         ← step 3: compute satellite start/end times
├── eraseInvalidCases.py            ← step 4: remove short/invalid cases

# Per-case HPC scripts (run in parallel per case)
├── runPipelineParallel.py          ← orchestrates steps 1-9 below
├── getLandfireProductsForFireSim.py ← step 1: download LANDFIRE from LFPS
├── splitLandfireTifBands.py        ← step 2: split multi-band LANDFIRE.tif
├── makePhiAndAdjFiles.py           ← step 3: create adj/phi rasters
├── downloadWeatherData.py          ← step 4: fetch ERA5 weather (OpenMeteo)
├── downloadAndRunWindninja.py      ← step 5: run WindNinja
├── wn_to_geotiff.py                ← helper: convert WindNinja ASCII → GeoTIFF
├── applyNelsonModel.py             ← step 6: compute dead-fuel moisture
├── getBarrierFile.py               ← step 7: rasterise road/water barriers
├── createElmfireInputFiles.py      ← step 8: write Elmfire namelist (.data)
├── runElmfireCase.py               ← step 9: execute Elmfire

# Data directories
├── Barriers/                       ← OSM roads/waterways GeoPackages + backup rivers SHP
├── Perimeters/                     ← MTBS burn perimeters shapefile
├── satellites/                     ← VIIRS/MODIS hotspot GeoPackage
├── nelson_csharp/                  ← Nelson dead-fuel model (C# executable)
└── bin/                            ← GDAL and other binaries
```

---

## Prerequisites

| Tool / Library | Where configured |
|---|---|
| Python ≥ 3.11 | – |
| geopandas, rasterio, fiona, pyproj, shapely | conda/pip |
| pandas, numpy, requests | conda/pip |
| GDAL CLI (`gdal_translate`, `gdalbuildvrt`, `ogr2ogr`) | on `$PATH` |
| WindNinja CLI (`WindNinja_cli`) | conda env `base` (configurable) |
| Elmfire executable (`elmfire`) | on `$PATH` |
| Nelson C# model | `pipelineConfig.NELSON_EXE` |
| LFPS API access (USGS email) | `pipelineConfig.LANDFIRE_EMAIL` |
| OpenMeteo ERA5 (free, no key) | `pipelineConfig.OPENMETEO_URL` |

---

## Quick-start

### 1. Configure `pipelineConfig.py`

Edit the **User-modifiable parameters** and **Paths** sections at the top of
`pipelineConfig.py`.  The most important settings are:

```python
# Paths (section 2)
BASE_VALIDATION      = Path("/your/network/share/autoValidate/")
FIRE_ROOT            = Path("/scratch/yourname/FirePairs")

# User parameters (section 1)
MIN_FIRE_YEAR        = 2023
MAX_FIRE_YEAR        = 2025
LANDFIRE_EMAIL       = "you@example.com"
MAX_WORKERS          = 12

# Elmfire (section 10)
ELMFIRE_PATH_TO_GDAL = "/path/to/your/conda/envs/elmfire/bin/"
```

Everything else (column names, file names, API URLs, simulation parameters)
is already set to sensible defaults and normally does not need to change.

### 2. Run the local setup (once per batch)

```bash
python setupPipeline.py
```

This runs five sequential steps and writes numbered case folders under
`FIRE_ROOT_LOGIN_NODE`:

| Step | Script | Output |
|------|--------|--------|
| 1 | `processScarsAndPoints.py` | `perimeters_ignitions.gpkg`, `all_ignitions.gpkg` |
| 2 | `separateScarsAndPointsToCases.py` | `00001/`, `00002/`, …, `fire_pairs_summary.csv` |
| 3 | `getSatelliteEndTimes.py` | `fire_pairs_summary_with_satellite.csv` |
| 4 | `eraseInvalidCases.py` | removes cases shorter than `MIN_HOURS_DURATION` h |
| 5 | `write_metadata_from_summary` | `case_metadata.json` in each folder |

To start fresh (delete all generated output before re-running):

```bash
python setupPipeline.py --clean
```

### 3. Copy case folders to HPC scratch

```bash
rsync -av $FIRE_ROOT_LOGIN_NODE/ $FIRE_ROOT/
```

### 4. Run the HPC pipeline

**Single case (useful for testing):**
```bash
python runPipelineParallel.py /scratch/nick/FirePairs/00001
```

**All cases under FIRE_ROOT:**
```bash
python runPipelineParallel.py
# or explicitly:
python runPipelineParallel.py --case-root /scratch/nick/FirePairs
```

Each case writes a `pipeline.log` file inside the case directory.

**SLURM array (recommended for large batches):**
The `run_full_pipeline.sh` bash script generates and submits a SLURM array
job that runs each case as an independent task.

---

## Per-case pipeline steps

Each case directory is processed sequentially through nine steps.

```
case_dir/
 │
 ├─ 1. getLandfireProductsForFireSim  →  LANDFIRE.tif
 ├─ 2. splitLandfireTifBands          →  inputs/{dem,slp,asp,fbfm40,cc,ch,cbh,cbd}.tif
 ├─ 3. makePhiAndAdjFiles             →  inputs/{adj,phi}.tif
 ├─ 4. downloadWeatherData            →  inputs/weather.wxs
 ├─ 5. downloadAndRunWindninja        →  inputs/{ws,wd}.tif
 ├─ 6. applyNelsonModel               →  inputs/{m1,m10,m100}.tif
 ├─ 7. getBarrierFile                 →  inputs/barrier.tif
 ├─ 8. createElmfireInputFiles        →  <folder_name>.data
 └─ 9. runElmfireCase                 →  outputs/time_of_arrival_*.tif
```

### Step 1 – LANDFIRE download

Submits a job to the USGS LANDFIRE Product Service (LFPS) API and polls for
completion.  The job downloads terrain (DEM, slope, aspect) and fuel/canopy
layers for the fire year.  The correct LANDFIRE dataset version (2019–2025)
is chosen automatically based on `perim_ignition` year.

Key config: `LANDFIRE_EMAIL`, `EXPAND`, `LFPS_POLL_MAX_TRIES`, `LFPS_POLL_SLEEP_S`.

### Step 2 – Band splitting

Opens the multi-band `LANDFIRE.tif` and writes one single-band GeoTIFF per
layer.  Band order is fixed by the LFPS `Layer_List` and mirrors
`LANDFIRE_BAND_FILE_NAMES` in `pipelineConfig`.

### Step 3 – Adjacency / phi rasters

Creates two all-ones float32 rasters matching the DEM footprint.  These are
required static inputs for Elmfire.

### Step 4 – ERA5 weather

Fetches hourly ERA5 data from the OpenMeteo archive API for the period
`[SatelliteIgnitionTime − CONDITIONING_DAYS, SatelliteEndTime]` and writes
a RAWS-format `.wxs` file.

Key config: `CONDITIONING_DAYS`, `OPENMETEO_URL`, `OPENMETEO_MODEL`.

### Step 5 – WindNinja

Runs WindNinja with weather-model initialisation (`PASTCAST-GCP-HRRR-CONUS-3-KM`
by default).  Fires longer than `WINDNINJA_MAX_WINDOW_DAYS` (13) are
automatically split into consecutive chunks.  Outputs are converted to
multi-band GeoTIFFs (one band per hour) by `wn_to_geotiff.py`.

Key config: `WINDNINJA_CONDA_ENV`, `WINDNINJA_WX_MODEL_TYPE`,
`WINDNINJA_MAX_WINDOW_DAYS`, `WINDNINJA_OUTPUT_HEIGHT`.

### Step 6 – Nelson dead-fuel moisture

Converts input rasters to BSQ format, calls the Nelson C# executable, then
converts output BSQ files back to compressed GeoTIFFs.  Produces 1-hr, 10-hr,
and 100-hr fuel moisture rasters.

Key config: `NELSON_EXE`, `CONDITIONING_DAYS`.

### Step 7 – Barrier raster

Rasterises OSM roads and waterways as barrier widths (metres).  Road and water
class widths are defined in `ROAD_WIDTHS_M` and `WATER_WIDTHS_M`.  A backup
river shapefile supplements areas with missing OSM waterway coverage.

Key config: `ROADS_GPKG`, `WATER_GPKG`, `BACKUP_WATER_SHP`,
`BARRIER_BACKUP_WATER_WIDTH_M`.

### Step 8 – Elmfire namelist

Reads DEM, ignition point, and timing metadata to build the Fortran namelist
(`.data`) file consumed by Elmfire.  The ignition point is snapped to the
nearest cell with a valid FBFM40 fuel code (≥ 101).

Key config: `ELMFIRE_DT_METEOROLOGY`, `ELMFIRE_DTDUMP`, `ELMFIRE_SIMULATION_DT`,
`ELMFIRE_LH_MC`, `ELMFIRE_LW_MC`, `ELMFIRE_PATH_TO_GDAL`.

### Step 9 – Elmfire execution

Invokes the `elmfire` executable in the case directory.  Elmfire reads the
`.data` file and writes time-of-arrival rasters to `outputs/`.

Key config: `ELMFIRE_EXE`.

---

## Case folder structure

```
<case_dir>/                        e.g. /scratch/nick/FirePairs/00001/
├── case_metadata.json             all case attributes (times, name, area, …)
├── firescar.gpkg                  MTBS burn polygon
├── ignition_point.gpkg            USFS ignition point
├── satellite_points.gpkg          hotspot points inside the burn polygon
├── LANDFIRE.tif                   raw multi-band download (deleted after step 2)
├── 00001.data                     Elmfire namelist
├── pipeline.log                   stdout/stderr from the full pipeline run
├── inputs/
│   ├── dem.tif                    elevation (m)
│   ├── slp.tif                    slope (degrees)
│   ├── asp.tif                    aspect (degrees)
│   ├── fbfm40.tif                 40-class fuel model
│   ├── cc.tif                     canopy cover (%)
│   ├── ch.tif                     canopy height (m)
│   ├── cbh.tif                    canopy base height (m)
│   ├── cbd.tif                    canopy bulk density (kg/m³)
│   ├── adj.tif                    adjacency (all 1s)
│   ├── phi.tif                    phi (all 1s)
│   ├── barrier.tif                road/water width raster (m)
│   ├── weather.wxs                RAWS-format weather file
│   ├── ws.tif                     wind speed (multi-band, one band/hour)
│   ├── wd.tif                     wind direction (multi-band, one band/hour)
│   ├── m1.tif                     1-hr fuel moisture
│   ├── m10.tif                    10-hr fuel moisture
│   ├── m100.tif                   100-hr fuel moisture
│   └── windninja/
│       ├── chunk_000/, chunk_001/ WindNinja output subdirectories
│       └── *.asc, *.prj, …        staged ASCII wind outputs
├── outputs/
│   └── time_of_arrival_<HH>.tif   Elmfire TOA rasters
└── scratch/                        Elmfire working directory
```

---

## Configuration reference

All settings are documented inline in `pipelineConfig.py`.  The sections are:

| Section | Contents |
|---------|----------|
| 1. User-modifiable | area threshold, year range, parallelism, email |
| 2. Paths | network root, HPC scratch, data subdirectories |
| 3. File/dir names | CSV names, shapefile names, subfolder names |
| 4. Column names | master CSV column keys shared across all scripts |
| 5. MTBS / USFS | field names in raw input datasets |
| 6. LANDFIRE | band order, product names, raster naming |
| 7. Satellite | coverage fraction, gap tolerance, buffer size |
| 8. Weather | OpenMeteo URL and model |
| 9. WindNinja | executable, model type, chunk size, output height |
| 10. Elmfire | exe, GDAL path, simulation parameters, moisture content |
| 11. Barrier | road/water widths, OSM field names |
| 12. LFPS API | base URL, product list, poll parameters |
| 13. Nelson | path to C# executable |

---

## Troubleshooting

### LANDFIRE job fails or times out
- Check that `LANDFIRE_EMAIL` is registered with LFPS.
- Increase `LFPS_POLL_MAX_TRIES` (default 300 × 10 s = ~50 min).
- The LFPS service can be slow during peak hours; try again later.

### WindNinja fails
- Confirm `WINDNINJA_CONDA_ENV` matches the name of the conda environment
  that has `WindNinja_cli` on its PATH.
- Check `inputs/windninja/chunk_000/windninja_cli.log` for the error message.
- Ensure the DEM covers the full fire domain.

### Elmfire fails
- Confirm `ELMFIRE_EXE` is on PATH (`which elmfire`).
- Check `ELMFIRE_PATH_TO_GDAL` points to a directory containing `gdal_translate`.
- The `outputs/` and `scratch/` directories are created automatically before
  Elmfire runs; if they exist from a prior run, their contents are overwritten.

### Satellite end time not found
- Some fires have sparse satellite coverage.  Cases where coverage never
  reaches 5% of the effective burn area are skipped gracefully.
- Adjust `COVERAGE_FRACTION` or `SAT_CHAIN_MAX_GAP_DAYS` in `pipelineConfig`
  to be more lenient.

### Ignition snapping fails
- Raised as a `RuntimeError` if no valid FBFM40 pixel (≥ 101) exists within
  2000 cells of the ignition point.  This usually means the burn perimeter is
  outside the LANDFIRE domain or the fuel download failed.

---

## Development notes

- **`pipelineConfig.py` is the single source of truth.**  Never hard-code
  paths, filenames, or tunable numbers inside individual scripts.

- **Each step script is independently runnable.**  Pass a `case_dir` argument
  to `main()` for single-case testing, or omit it to process all cases under
  `FIRE_ROOT`.

- **`parallel_api.py`** provides the `Tee` class, `make_logger`, `retry_call`,
  and `run_parallel`.  Import from here instead of reimplementing locally.

- **`case_metadata.py`** handles all JSON serialisation / deserialisation of
  per-case metadata, including datetime normalisation.

- Scripts in `serial/` are slower sequential versions kept for debugging.
  Scripts in `old_code/` are obsolete.
