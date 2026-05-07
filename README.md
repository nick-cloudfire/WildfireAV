# ELMFIRE / FARSITE Validation Pipeline

Automated end-to-end pipeline that matches historical MTBS burn perimeters to
USFS ignition points, downloads all required geospatial inputs, runs wind and
fuel-moisture models, executes [ELMFIRE](https://github.com/lautenberger/elmfire)
and [FARSITE](https://www.fs.usda.gov/rmrs/tools/farsite), and produces a
multi-page validation PDF comparing simulated vs. observed burn areas.

---

## Repository layout

```
Data/
├── pipelineConfig.py                  ← single source of truth for all settings
├── parallel_api.py                    ← shared utilities (Tee, logging, retries)
├── case_metadata.py                   ← read/write per-case JSON metadata
├── cleanPipelineOutputs.py            ← clean per-case simulation outputs

# Master entry point (replaces run_validation.py)
├── runWildfireAV                      ← single CLI: setup / run / pdf

# Setup scripts (run once, locally)
├── setup/
│   ├── setupPipeline.py               ← orchestrates setup steps 1-5
│   ├── processScarsAndPoints.py       ← step 1: match perimeters → ignition points
│   ├── separateScarsAndPointsToCases.py ← step 2: create numbered case folders
│   ├── getSatelliteEndTimes.py        ← step 3: compute satellite start/end times
│   └── eraseInvalidCases.py          ← step 4: remove short/invalid cases

# Per-case simulation pipeline (run in parallel)
├── pipeline/
│   ├── runPipelineParallel.py         ← orchestrates per-case steps (see below)
│   ├── runBatch.py                    ← parallel batch runner
│   ├── getLandfireProductsForFireSim.py ← step 1:  download LANDFIRE from LFPS
│   ├── splitLandfireTifBands.py       ← step 2:  split multi-band LANDFIRE.tif
│   ├── makePhiAndAdjFiles.py          ← step 3:  create adj/phi rasters
│   ├── downloadWeatherData.py         ← step 4:  fetch ERA5 weather (OpenMeteo)
│   ├── downloadAndRunWindninja_WXS.py ← step 5a: WindNinja (WINDNINJA_SOURCE=install)
│   ├── downloadAndRunWindninja_wxModel.py ← step 5b: WindNinja wx-model variant
│   ├── downloadAndRunWindninja.py     ← step 5c: WindNinja (legacy)
│   ├── wn_to_geotiff.py               ← helper: convert WindNinja ASCII → GeoTIFF
│   ├── applyNelsonModel.py            ← step 6:  compute dead-fuel moisture
│   ├── getBarrierFile.py              ← step 7:  rasterise road/water barriers
│   ├── createElmfireInputFiles.py     ← step 8:  write ELMFIRE namelist (.data)
│   ├── prepareFarsite.py              ← step 9:  create FARSITE inputs (LCP, etc.)
│   ├── runElmfireCase.py              ← step 10: execute ELMFIRE
│   ├── runFarsiteCase.py              ← step 11: execute FARSITE (Linux native)
│   └── farsiteWindToGeotiff.py       ← step 12: extract ws/wd from FARSITE winds
│                                         (only when WINDNINJA_SOURCE=farsite)

# Validation report
├── validation/
│   └── getValidationPDF.py            ← generate multi-page validation PDF

# Developer / debug utilities
├── tools/
│   ├── monitorBatch.py                ← real-time batch progress monitor
│   ├── prefetchLandfire.py            ← pre-fetch LANDFIRE for all cases
│   ├── debugBandCounts.py             ← debug raster band counts per case
│   ├── debugSatelliteEndTimes.py      ← inspect satellite coverage curves
│   ├── compareOutputs.py              ← multi-model PDF comparison (fast)
│   ├── visualiseSingleCase.py         ← plot observed vs simulated for one case
│   └── getWeatherHerbie.py            ← fetch weather via Herbie (dev use)

# Data directories
# Input data (not version-controlled — see Input data requirements below)
├── inputs/
│   ├── mtbs_perimeters.gpkg           ← MTBS burn perimeters
│   ├── NIFC_FOD.gpkg                  ← NIFC fire occurrence points
│   ├── nasa_lance_allSatellites.gpkg  ← VIIRS/MODIS/Landsat hotspot detections
│   ├── osm_conus_roads.gpkg           ← OSM road network
│   ├── grwl.gpkg                      ← Global River Widths from Landsat waterways
│   └── osm_conus_rivers.gpkg          ← Backup river polygons
└── nelson_csharp/                     ← Nelson dead-fuel model (C# source + binary)
```

---

## Prerequisites

| Tool / Library | Where configured |
|---|---|
| Python ≥ 3.11 | – |
| geopandas, rasterio, fiona, pyproj, shapely | conda/pip |
| pandas, numpy, requests, matplotlib | conda/pip |
| GDAL CLI (`gdal_translate`, `gdalbuildvrt`, `ogr2ogr`) | on `$PATH` |
| WindNinja CLI (`WindNinja_cli`) | conda env (see `WINDNINJA_CONDA_ENV`) |
| ELMFIRE executable (`elmfire`) | on `$PATH` |
| FARSITE Linux binary (`TestFARSITE`) | `pipelineConfig.FARSITE_FB_DIR` |
| Nelson C# model | `pipelineConfig.NELSON_EXE` |
| LFPS API access (USGS email) | `LFPS_EMAIL` environment variable |
| OpenMeteo ERA5 (free, no key) | `pipelineConfig.OPENMETEO_URL` |

---

## WSL setup

This pipeline is designed to run inside **Windows Subsystem for Linux (WSL2)**
with Ubuntu 24.04.  FARSITE runs as a natively compiled Linux executable so
Wine is not required.

### 1. Enable WSL2

In an elevated PowerShell:

```powershell
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2
```

Restart when prompted, then open the Ubuntu 24.04 terminal and create your
Linux user account.

### 2. Install Miniconda

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh
# Follow the prompts; let the installer run conda init
source ~/.bashrc
```

### 3. Create the Python environment

```bash
conda create -n elmfire python=3.11 -y
conda activate elmfire

# Geospatial stack
conda install -c conda-forge \
    geopandas rasterio fiona pyproj shapely \
    pandas numpy requests matplotlib \
    gdal -y
```

All GDAL CLI tools (`gdal_translate`, `gdalbuildvrt`, `ogr2ogr`) come with the
`gdal` conda package and are available on PATH inside the environment.

### 4. Install ELMFIRE

Follow the [ELMFIRE build instructions](https://github.com/lautenberger/elmfire).
The compiled `elmfire` executable must be on PATH inside the `elmfire` conda
environment.  Set `ELMFIRE_PATH_TO_GDAL` in `pipelineConfig.py` to the `bin/`
directory of the same conda environment, e.g.:

```python
ELMFIRE_PATH_TO_GDAL = "/home/<user>/miniconda3/envs/elmfire/bin/"
```

### 5. Install WindNinja (optional — only needed for `WINDNINJA_SOURCE="install"`)

Follow the instructions in the [WindNinja Github](https://github.com/firelab/windninja/wiki/Building-WindNinja-on-Linux-22.04). 
Note that the pipeline was setup in Ubuntu-24.04, when asked to run scripts for 22.04, run the provided files for 24.04 instead.
No need to install the GUI version. 

Set `WINDNINJA_CONDA_ENV` in `pipelineConfig.py` to the environment name that
has `WindNinja_cli` on its PATH (default `"base"`).

### 6. Build or install the native Linux FARSITE binary

The pipeline now runs FARSITE as a natively compiled Linux executable — Wine
is no longer required.  Obtain or build the `TestFARSITE` Linux binary and
place it in any directory on the machine (e.g. `/home/<user>/farsite/src/`).

Set `FARSITE_FB_DIR` in `pipelineConfig.py` to that directory and
`FARSITE_EXE_NAME` to the executable filename:

```python
FARSITE_FB_DIR   = Path("/home/<user>/farsite/src")
FARSITE_EXE_NAME = "TestFARSITE"
```

Verify:
```bash
/home/<user>/farsite/src/TestFARSITE --help
```

### 7. Clone and configure

```bash
cd /home/<user>/elmfire_validation
git clone https://github.com/nick-cloudfire/autoValidate.git Data
cd Data
```

### 8. Install the Nelson dead-fuel moisture model

run the following command to clone the Nelson Dead Fuel Moisture model made for this pipeline,
based on WUINITY-PREACT Copyright (C) 2025 Jonathan Wahlqvist.

git clone https://github.com/nick-cloudfire/Nelson-Dead-Fuel-Moisture.git <nelson_csharp>

```bash
# Install .NET 8 SDK
sudo apt install -y dotnet-sdk-8.0

# Build the release binary
cd /home/<user>/elmfire_validation/Data/nelson_csharp
dotnet publish -c Release
```
The compiled binary path is set automatically by `pipelineConfig.NELSON_EXE`.

### 9. Obtain the native Linux FARSITE binary

The FARSITE Linux binary is not included in this repository.  Place the
compiled `TestFARSITE` executable in any directory (e.g.
`/home/<user>/farsite/src/`).  No Wine SDK layout is required — only the
single binary needs to be present at the path pointed to by `FARSITE_FB_DIR`.

Edit `pipelineConfig.py` — at minimum update:

```python
BASE_VALIDATION  = Path("/home/<user>/elmfire/elmfire_validation/")
FARSITE_FB_DIR   = Path("/home/<user>/farsite/src")   # dir containing TestFARSITE
WINDNINJA_SOURCE = "install"                           # or "farsite"
```

`LANDFIRE_EMAIL` is read from the `LFPS_EMAIL` environment variable (not stored
in the config file).  Add the following to your `~/.bashrc` (or SLURM job script):

```bash
export LFPS_EMAIL=you@example.com   # must be registered at https://lfps.usgs.gov
```

Then reload your shell: `source ~/.bashrc`

---

## Input data requirements

All input data lives under `inputs/` and is not version-controlled (listed in
`.gitignore`).  Download these datasets once and place them at the paths below.

### MTBS burn perimeters

- **Source**: [MTBS Data Access](https://www.mtbs.gov/direct-download)
- **Place at**: `inputs/mtbs_perimeters.gpkg`

### NIFC fire occurrence points

- **Source**: [NIFC Fire Occurrence Database (FOD)](https://data-nifc.opendata.arcgis.com/) — download as GeoPackage
- **Place at**: `inputs/NIFC_FOD.gpkg`
- Required fields: `IncidentName`, `FireDiscoveryDateTime`, `FireOutDateTime`

### VIIRS / MODIS satellite hotspot detections

- **Source**: [NASA FIRMS archive](https://firms.modaps.eosdis.nasa.gov/download/) (VIIRS/MODIS active fire detections for the US)
  Login with your email to download the CONUS dataset.
- Merge all downloaded shapefiles into a single GeoPackage with layer `output`.
  Required columns: `ACQ_DATE` (date), `ACQ_TIME` (HHMM string), plus geometry.
- **Place at**: `inputs/nasa_lance_allSatellites.gpkg`

### OSM road and waterway barriers

| File | Contents | Config key |
|------|----------|------------|
| `inputs/osm_conus_roads.gpkg` | US road network (layer `lines`, field `highway`) | `ROADS_GPKG` |
| `inputs/grwl.gpkg` | Global River Widths from Landsat (layer `lines`, field `waterway`) | `WATER_GPKG` |
| `inputs/osm_conus_rivers.gpkg` | Backup river polygons for areas with poor GRWL coverage | `BACKUP_WATER_GPKG` |

Road GeoPackages can be extracted from a US OSM `.pbf` file using
`osmium` + `ogr2ogr`, or downloaded from [GeoFabrik](https://download.geofabrik.de/).

### LANDFIRE (downloaded automatically)

LANDFIRE terrain and fuel rasters are fetched automatically per case via the
USGS LFPS API (step 1 of the simulation pipeline).  No manual download needed —
only a valid email registered at [USGS LFPS](https://lfps.usgs.gov), set via the
`LFPS_EMAIL` environment variable (see WSL setup step 9).

### Required layout before first run

```
Data/
├── inputs/
│   ├── mtbs_perimeters.gpkg
│   ├── NIFC_FOD.gpkg
│   ├── nasa_lance_allSatellites.gpkg
│   ├── osm_conus_roads.gpkg
│   ├── grwl.gpkg
│   └── osm_conus_rivers.gpkg
└── nelson_csharp/                (built from source — see WSL setup above)

# FARSITE binary lives outside this repo (see WSL setup step 6 / 9):
/home/<user>/farsite/src/TestFARSITE
```

---

## Quick-start

### 1. Configure `pipelineConfig.py`

Edit the **User-modifiable parameters** and **Paths** sections:

```python
# Paths
BASE_VALIDATION      = Path("/home/<user>/elmfire/elmfire_validation/")
FIRE_ROOT            = Path("/scratch/yourname/FirePairs")
FARSITE_FB_DIR       = Path("/home/<user>/farsite/src")   # dir with TestFARSITE binary

# User parameters
MIN_FIRE_YEAR        = 2024
MAX_FIRE_YEAR        = 2025
MAX_PARALLEL_CASES   = 14

# Wind source: "install" = WindNinja, "farsite" = derive winds from FARSITE output
WINDNINJA_SOURCE     = "install"

# ELMFIRE
ELMFIRE_PATH_TO_GDAL = "/path/to/your/conda/envs/elmfire/bin/"
```

### 2. Run the full pipeline

`runWildfireAV` is the single entry point.  The `--start` flag selects the
phase to begin from; all later phases also run automatically.

```bash
# Full pipeline from scratch (clean cases → setup → run → pdf):
./runWildfireAV --start setup

# Clean simulation outputs, re-run all cases, regenerate pdf:
./runWildfireAV --start run

# Regenerate PDF only (no simulation):
./runWildfireAV --start pdf

# Parallel run with 8 workers, skip completed cases:
./runWildfireAV --start run --workers 8 --skip-done

# Process specific cases only:
./runWildfireAV --start run --cases 00001 00005 00008
```

See `./runWildfireAV --help` for all options.

---

## Pipeline phases

### Phase: setup (`--start setup`)

Runs the one-time case-preparation pipeline.  Deletes all existing numbered
case folders first (full reset), then rebuilds them from the raw MTBS and USFS
input data.

| Step | Script | Output |
|------|--------|--------|
| 1 | `setup/processScarsAndPoints.py` | `perimeters_ignitions.gpkg`, `all_ignitions.gpkg` |
| 2 | `setup/separateScarsAndPointsToCases.py` | `00001/`, `00002/`, …, `fire_pairs_summary.csv` |
| 3 | `setup/getSatelliteEndTimes.py` | `fire_pairs_summary_with_satellite.csv` |
| 4 | `setup/eraseInvalidCases.py` | removes cases shorter than `MIN_HOURS_DURATION` h |
| 5 | `write_metadata_from_summary` | `case_metadata.json` in each folder |

### Phase: run (`--start run`)

Cleans per-case simulation outputs (from `landfire_bands` onwards), then
executes the per-case simulation pipeline in parallel using
`ProcessPoolExecutor`.  Step order depends on `WINDNINJA_SOURCE`:

#### `WINDNINJA_SOURCE = "install"` (default)

```
Step 1   getLandfireProductsForFireSim  →  LANDFIRE.tif
Step 2   splitLandfireTifBands          →  inputs/{dem,slp,asp,fbfm40,cc,ch,cbh,cbd}.tif
Step 3   makePhiAndAdjFiles             →  inputs/{adj,phi}.tif
Step 4   downloadWeatherData            →  inputs/weather.wxs
Step 5   downloadAndRunWindninja        →  inputs/{ws,wd}.tif  (WindNinja)
Step 6   applyNelsonModel               →  inputs/{m1,m10,m100}.tif
Step 7   getBarrierFile                 →  inputs/barrier.tif
Step 8   createElmfireInputFiles        →  <case>.data
Step 9   prepareFarsite                 →  farsite/{landscape.lcp, farsite.input, …}
Step 10  runElmfireCase                 →  outputs/time_of_arrival_*.tif
Step 11  runFarsiteCase                 →  farsite/outputs/farsite_ArrivalTime.asc
```

#### `WINDNINJA_SOURCE = "farsite"`

```
Step 1   getLandfireProductsForFireSim  →  LANDFIRE.tif
Step 2   splitLandfireTifBands          →  inputs/{dem,slp,asp,fbfm40,cc,ch,cbh,cbd}.tif
Step 3   makePhiAndAdjFiles             →  inputs/{adj,phi}.tif
Step 4   downloadWeatherData            →  inputs/weather.wxs
Step 5   (WindNinja skipped)
Step 6   applyNelsonModel               →  inputs/{m1,m10,m100}.tif
Step 7   getBarrierFile                 →  inputs/barrier.tif
Step 8   createElmfireInputFiles        →  <case>.data
Step 9   prepareFarsite                 →  farsite/{landscape.lcp, farsite.input, …}
Step 10  runFarsiteCase                 →  farsite/outputs/farsite_ArrivalTime.asc
                                            farsite/outputs/farsite_WindGrids.tif
Step 11  farsiteWindToGeotiff           →  inputs/{ws,wd}.tif  (from FARSITE wind grids)
                                            deletes farsite_WindGrids.tif after extraction
Step 12  runElmfireCase                 →  outputs/time_of_arrival_*.tif
```

### Phase: pdf (`--start pdf`)

Generates `validation.pdf` in `FIRE_ROOT`.  The report contains one page per
case plus four summary pages:

| Page | Content |
|------|---------|
| Per-case | Burn-mask map, area-evolution curves, metrics table |
| Summary 1 | Similarity scores vs. average wind speed |
| Summary 2 | Similarity scores vs. burn area and fire duration (log x) |
| Summary 3 | Area estimation bias per case (asymmetric log₂ bar chart) |
| Summary 4 | Similarity score distribution histograms |

### Cleaning outputs

Cleaning is integrated into the entry point:

- `--start setup` deletes all numbered case folders before re-running setup.
- `--start run` deletes per-case simulation outputs (from `landfire_bands`
  onwards, including `farsite/` directories) before re-running simulations.
  `LANDFIRE.tif`, `case_metadata.json`, and fire geometry files are preserved.

For fine-grained control use `cleanPipelineOutputs.py` directly:

```bash
python cleanPipelineOutputs.py                          # dry-run by default
python cleanPipelineOutputs.py --execute                # actually delete
python cleanPipelineOutputs.py --from elmfire_outputs   # only ELMFIRE outputs
python cleanPipelineOutputs.py --include-wind-tifs      # also delete ws/wd.tif
```

---

## FARSITE execution (native Linux)

FARSITE runs as a natively compiled Linux executable (`TestFARSITE`).
Wine is not required.  `runFarsiteCase.py` writes a plain-text command file
(`farsite_linux.txt`) with absolute Linux paths and invokes the binary directly.
The completion sentinel is `farsite_ArrivalTime.asc`; the script verifies this
file exists after the run.

After a successful FARSITE run, outputs are cleaned automatically:
- **`WINDNINJA_SOURCE = "install"`**: keeps only `farsite_ArrivalTime.asc`;
  deletes all other FARSITE outputs including `farsite_WindGrids.tif`.
- **`WINDNINJA_SOURCE = "farsite"`**: keeps `farsite_ArrivalTime.asc` +
  `farsite_WindGrids.tif` until `farsiteWindToGeotiff.py` has extracted
  `ws.tif`/`wd.tif`, then deletes `farsite_WindGrids.tif` too.

---

## Case folder structure

```
<case_dir>/                              e.g. FirePairs/00001/
├── case_metadata.json                   all case attributes (times, name, area, …)
├── firescar.gpkg                        MTBS burn polygon
├── ignition_point.gpkg                  USFS ignition point
├── satellite_points.gpkg                hotspot points inside the burn polygon
├── LANDFIRE.tif                         raw multi-band download
├── 00001.data                           ELMFIRE namelist
├── pipeline.log                         stdout/stderr from the pipeline run
├── inputs/
│   ├── dem.tif                          elevation (m)
│   ├── slp.tif                          slope (degrees)
│   ├── asp.tif                          aspect (degrees)
│   ├── fbfm40.tif                       40-class fuel model
│   ├── cc.tif                           canopy cover (%)
│   ├── ch.tif                           canopy height (m)
│   ├── cbh.tif                          canopy base height (m)
│   ├── cbd.tif                          canopy bulk density (kg/m³)
│   ├── adj.tif                          adjacency (all 1s)
│   ├── phi.tif                          phi (all 1s)
│   ├── barrier.tif                      road/water width raster (m)
│   ├── weather.wxs                      RAWS-format weather file
│   ├── ws.tif                           wind speed  (N bands, one per hour)
│   ├── wd.tif                           wind direction (N bands, one per hour)
│   ├── m1.tif                           1-hr fuel moisture
│   ├── m10.tif                          10-hr fuel moisture
│   ├── m100.tif                         100-hr fuel moisture
│   └── windninja/                       WindNinja workspace (deleted after ws/wd built)
├── farsite/
│   ├── landscape.lcp                    FARSITE landscape file (converted from LANDFIRE)
│   ├── farsite.input                    FARSITE control file
│   ├── farsite_linux.txt                command-line args file (auto-generated)
│   ├── ignition.shp                     reprojected ignition point
│   ├── barrier.shp                      merged barrier polygons (optional)
│   └── outputs/
│       └── farsite_ArrivalTime.asc      FARSITE time-of-arrival output (sentinel)
├── outputs/
│   └── time_of_arrival_<HH>.tif         ELMFIRE TOA raster
└── scratch/                             ELMFIRE working directory
```

---

## Configuration reference

All settings are documented inline in `pipelineConfig.py`.

| Section | Key settings |
|---------|-------------|
| 1. User-modifiable | `MIN/MAX_FIRE_YEAR`, `MTBS_AREA_THRESHOLD_ACRES`, `MAX_PARALLEL_CASES`, `WINDNINJA_SOURCE` |
| 2. Paths | `FIRE_ROOT`, `BASE_VALIDATION`, `FARSITE_FB_DIR` |
| 3. File/dir names | CSV names, shapefile names, subfolder names |
| 4. Column names | master CSV column keys shared across scripts |
| 5. MTBS / USFS | field names in raw input datasets |
| 6. LANDFIRE | band order, product names, raster naming |
| 7. Satellite | coverage fraction, gap tolerance, buffer size |
| 8. Weather | OpenMeteo URL and model |
| 9. WindNinja | exe, model type, chunk size, output height, mesh resolution factor |
| 10. ELMFIRE | exe, GDAL path, simulation parameters, moisture content |
| 11. FARSITE | `FARSITE_FB_DIR` (dir with Linux binary), `FARSITE_EXE_NAME` |
| 12. Barrier | road/water widths, OSM field names |
| 13. LFPS API | base URL, product list, poll parameters |
| 14. Nelson | path to C# executable |

---

## Troubleshooting

### LANDFIRE job fails or times out
- The pipeline retries each LANDFIRE download up to 3 times automatically
  (`MAX_RETRIES` in `getLandfireProductsForFireSim.py`).
- Check that `LFPS_EMAIL` is set in your shell (`echo $LFPS_EMAIL`) and that the address is registered at https://lfps.usgs.gov.
- Increase `LFPS_POLL_MAX_TRIES` (default 300 × 10 s ≈ 50 min).
- The LFPS service can be slow during peak hours; try again later.

### WindNinja fails
- Confirm `WINDNINJA_CONDA_ENV` matches the conda environment with `WindNinja_cli` on PATH.
- Check `inputs/windninja/chunk_000/windninja_cli.log` for the error message.
- Ensure the DEM covers the full fire domain.

### FARSITE fails
- Confirm `FARSITE_FB_DIR` points to the directory containing `TestFARSITE` and that the binary is executable: `ls -l $FARSITE_FB_DIR/TestFARSITE`.
- Check `farsite/pipeline.log` for error output.
- Run the binary interactively with the generated `farsite_linux.txt` to test: `<FARSITE_FB_DIR>/TestFARSITE farsite/farsite_linux.txt`.

### ELMFIRE fails
- Confirm `ELMFIRE_EXE` is on PATH: `which elmfire`.
- Check `ELMFIRE_PATH_TO_GDAL` points to a directory containing `gdal_translate`.
- The `outputs/` and `scratch/` directories are created automatically; existing
  contents are overwritten on re-run.

### Setup step 1 reports "no valid cases found"
- The four filter stages (area, year, name+date match, spatial) each raise a
  `RuntimeError` with a specific message and the relevant `pipelineConfig.py`
  knobs to relax:
  - **No perimeters**: lower `MTBS_AREA_THRESHOLD_ACRES` or widen `MIN/MAX_FIRE_YEAR`.
  - **No points**: widen `MIN/MAX_FIRE_YEAR`.
  - **No name+date matches**: increase `DAY_TOLERANCE_DAYS`.
  - **No spatial matches**: increase `DAY_TOLERANCE_DAYS` or lower `MTBS_AREA_THRESHOLD_ACRES`.

### Satellite end time not found
- Some fires have sparse satellite coverage.  Cases where coverage never
  reaches the threshold are skipped gracefully.
- Adjust `COVERAGE_FRACTION` or `SAT_CHAIN_MAX_GAP_DAYS` in `pipelineConfig`
  to be more lenient.

### Ignition snapping fails
- Raised as a `RuntimeError` if no valid FBFM40 pixel (≥ 101) exists within
  2000 cells of the ignition point.  Usually means the burn perimeter is
  outside the LANDFIRE domain or the fuel download failed.

### Disk usage is excessive
- WindNinja creates many `step_NNN/` subdirectories.  These are deleted
  automatically by `wn_to_geotiff.clean_windninja_outputs()` after `ws.tif`
  and `wd.tif` are built.  If a run was interrupted mid-step, re-run from the
  `windninja` step: `python cleanPipelineOutputs.py --from windninja --execute`
  then `./runWildfireAV --start run`.
- All GeoTIFF outputs use LZW compression + tiling.

---

## Development notes

- **`pipelineConfig.py` is the single source of truth.**  Never hard-code
  paths, filenames, or tunable numbers inside individual scripts.

- **Each step script is independently runnable.**  Pass a `case_dir` argument
  to `main()` for single-case testing, or omit it to process all cases under
  `FIRE_ROOT`.

- **`parallel_api.py`** provides `Tee`, `make_logger`, `retry_call`, and
  `run_subprocess`.  Import from here instead of reimplementing locally.

- **`case_metadata.py`** handles all JSON serialisation of per-case metadata,
  including datetime normalisation.

- Scripts in `tools/` are standalone dev/debug utilities and do not participate
  in the main pipeline.
