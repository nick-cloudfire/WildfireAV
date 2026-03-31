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

# Master entry point
├── run_validation.py                  ← clean / setup / run / pdf phases

# Setup scripts (run once, locally)
├── setupPipeline.py                   ← orchestrates setup steps 1-5
├── processScarsAndPoints.py           ← step 1: match perimeters → ignition points
├── separateScarsAndPointsToCases.py   ← step 2: create numbered case folders
├── getSatelliteEndTimes.py            ← step 3: compute satellite start/end times
├── eraseInvalidCases.py               ← step 4: remove short/invalid cases

# Per-case simulation pipeline (run in parallel)
├── runPipelineParallel.py             ← orchestrates per-case steps (see below)
├── getLandfireProductsForFireSim.py   ← step 1:  download LANDFIRE from LFPS
├── splitLandfireTifBands.py           ← step 2:  split multi-band LANDFIRE.tif
├── makePhiAndAdjFiles.py              ← step 3:  create adj/phi rasters
├── downloadWeatherData.py             ← step 4:  fetch ERA5 weather (OpenMeteo)
├── downloadAndRunWindninja_WXS.py     ← step 5a: WindNinja (WINDNINJA_SOURCE=install)
├── downloadAndRunWindninja_wxModel.py ← step 5b: WindNinja wx-model variant
├── wn_to_geotiff.py                   ← helper: convert WindNinja ASCII → GeoTIFF
├── applyNelsonModel.py                ← step 6:  compute dead-fuel moisture
├── getBarrierFile.py                  ← step 7:  rasterise road/water barriers
├── createElmfireInputFiles.py         ← step 8:  write ELMFIRE namelist (.data)
├── prepareFarsite.py                  ← step 9:  create FARSITE inputs (LCP, etc.)
├── runElmfireCase.py                  ← step 10: execute ELMFIRE
├── runFarsiteCase.py                  ← step 11: execute FARSITE via Wine
├── farsiteWindToGeotiff.py            ← step 12: extract ws/wd from FARSITE winds
                                          (only when WINDNINJA_SOURCE=farsite)

# Validation report
├── getValidationPDF.py                ← generate multi-page validation PDF

# Data directories
├── Barriers/                          ← OSM roads/waterways GeoPackages
├── Perimeters/                        ← MTBS burn perimeters shapefile
├── satellites/                        ← VIIRS/MODIS hotspot GeoPackage
├── FB/                                ← FARSITE SDK (bin/TestFARSITE.exe, etc.)
├── nelson_csharp/                     ← Nelson dead-fuel model (C# executable)
└── bin/                               ← GDAL and other binaries
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
| FARSITE SDK (`TestFARSITE.exe`) | `pipelineConfig.FARSITE_FB_DIR` |
| Wine (for running FARSITE on Linux/WSL) | on `$PATH` |
| Nelson C# model | `pipelineConfig.NELSON_EXE` |
| LFPS API access (USGS email) | `pipelineConfig.LANDFIRE_EMAIL` |
| OpenMeteo ERA5 (free, no key) | `pipelineConfig.OPENMETEO_URL` |

---

## WSL setup

This pipeline is designed to run inside **Windows Subsystem for Linux (WSL2)**
with Ubuntu 24.04.  FARSITE runs as a Windows `.exe` via Wine so both Linux
and Windows tooling are available in the same environment.

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

```bash
# WindNinja must be available in a conda environment (default: "base")
conda activate base
conda install -c conda-forge windninja -y
conda activate elmfire
```

Set `WINDNINJA_CONDA_ENV` in `pipelineConfig.py` to the environment name that
has `WindNinja_cli` on its PATH (default `"base"`).

### 6. Install Wine (for FARSITE)

```bash
sudo dpkg --add-architecture i386
sudo apt update
sudo apt install -y wine wine32 wine64 libwine libwine:i386 fonts-wine
```

Verify:
```bash
wine --version   # should print wine-x.x.x
```

On first use Wine will initialise its prefix (`.wine/`) automatically.
No further Wine configuration is needed — the pipeline sets all required
environment variables (`GDAL_DATA`, `PROJ_LIB`, `WINEDEBUG`) at runtime.

### 7. Install the Nelson dead-fuel moisture model

The Nelson model is a .NET 8 C# project included in `nelson_csharp/`.

```bash
# Install .NET 8 SDK
sudo apt install -y dotnet-sdk-8.0

# Build the release binary
cd /home/<user>/elmfire_validation/Data/nelson_csharp
dotnet publish -c Release
```

The compiled binary path is set automatically by `pipelineConfig.NELSON_EXE`.

### 8. Install FARSITE SDK

The FARSITE SDK (`FB/`) is not included in this repository.  Obtain
`TestFARSITE.exe` and its supporting files from Missoula Fire Sciences Lab and
place them at the path configured in `pipelineConfig.FARSITE_FB_DIR`.
The expected layout is:

```
FB/
├── bin/
│   ├── TestFARSITE.exe
│   ├── gdal-data/
│   └── proj9/share/
└── setenv.bat
```

### 9. Clone and configure

```bash
cd /home/<user>/elmfire_validation
git clone https://github.com/nick-cloudfire/autoValidate.git Data
cd Data
```

Edit `pipelineConfig.py` — at minimum update:

```python
BASE_VALIDATION  = Path("/home/<user>/elmfire_validation/")
LANDFIRE_EMAIL   = "you@example.com"   # must be registered with LFPS
WINDNINJA_SOURCE = "install"           # or "farsite"
```

---

## Input data requirements

The pipeline requires the following external datasets.  Download them once and
place them at the paths configured in `pipelineConfig.py`.

### MTBS burn perimeters

- **Source**: [MTBS Data Access](https://www.mtbs.gov/direct-download)
- **File**: `mtbs_perims_DD.shp` (national perimeter shapefile)
- **Place at**: `pipelineConfig.MTBS_PERIMS_RAW`
  (default `Data/Perimeters/mtbs_perims_DD.shp`)

### USFS fire occurrence points

- **Source**: USFS ArcGIS Feature Service — download as GeoJSON:
  `National_USFS_Fire_Occurrence_Point_(Feature_Layer).geojson`
- **Place at**: `pipelineConfig.USFS_POINTS_RAW`
  (default `Data/National_USFS_Fire_Occurrence_Point_(Feature_Layer).geojson`)

### VIIRS / MODIS satellite hotspot detections

- **Source**: NASA FIRMS archive — download the VIIRS/MODIS active fire
  detections for the US and convert to a GeoPackage named `clipped.gpkg`
  with layer `output`.  Required columns: `ACQ_DATE` (date), `ACQ_TIME` (HHMM
  string), plus geometry.
- **Place at**: `pipelineConfig.SATELLITE_GPKG`
  (default `Data/satellites/clipped.gpkg`)

### OSM road and waterway barriers

Pre-processed OpenStreetMap extracts in GeoPackage format:

| File | Contents | Config key |
|------|----------|------------|
| `Barriers/us_roads.gpkg` | US road network (layer `lines`, field `highway`) | `ROADS_GPKG` |
| `Barriers/waterways.gpkg` | OSM waterways (layer `lines`, field `waterway`) | `WATER_GPKG` |
| `Barriers/us_rivers.shp` | Backup river shapefile for areas with poor OSM coverage | `BACKUP_WATER_SHP` |

These can be extracted from a US OSM `.pbf` file using `osmium` + `ogr2ogr`,
or downloaded from [GeoFabrik](https://download.geofabrik.de/).

### LANDFIRE (downloaded automatically)

LANDFIRE terrain and fuel rasters are downloaded automatically per case via the
USGS LFPS API during step 1 of the simulation pipeline.  No manual download is
required — only a valid `LANDFIRE_EMAIL` registered at
[USGS LFPS](https://lfps.usgs.gov) is needed.

### Summary of required files before first run

```
Data/
├── Perimeters/
│   └── mtbs_perims_DD.shp   (+ .dbf, .prj, .shx)
├── satellites/
│   └── clipped.gpkg
├── Barriers/
│   ├── us_roads.gpkg
│   ├── waterways.gpkg
│   └── us_rivers.shp        (+ .dbf, .prj, .shx)
├── FB/                       (FARSITE SDK)
│   └── bin/TestFARSITE.exe
├── nelson_csharp/            (built from source — see WSL setup above)
└── National_USFS_Fire_Occurrence_Point_(Feature_Layer).geojson
```

---

## Quick-start

### 1. Configure `pipelineConfig.py`

Edit the **User-modifiable parameters** and **Paths** sections:

```python
# Paths
BASE_VALIDATION      = Path("/your/network/share/autoValidate/")
FIRE_ROOT            = Path("/scratch/yourname/FirePairs")
FARSITE_FB_DIR       = Path("/path/to/FB")        # FARSITE SDK root

# User parameters
MIN_FIRE_YEAR        = 2023
MAX_FIRE_YEAR        = 2025
LANDFIRE_EMAIL       = "you@example.com"
MAX_PARALLEL_CASES   = 12

# Wind source: "install" = WindNinja, "farsite" = derive winds from FARSITE output
WINDNINJA_SOURCE     = "install"

# ELMFIRE
ELMFIRE_PATH_TO_GDAL = "/path/to/your/conda/envs/elmfire/bin/"
```

### 2. Run the full pipeline

```bash
# Full pipeline from scratch:
python run_validation.py

# Skip clean + setup (case folders already prepared):
python run_validation.py --phases run pdf

# Regenerate PDF only:
python run_validation.py --phases pdf

# Clean outputs then re-run (keep existing case folders):
python run_validation.py --phases clean run pdf

# Parallel run, skip cases that already have ELMFIRE outputs:
python run_validation.py --phases run pdf --workers 8 --skip-done

# Process specific cases only:
python run_validation.py --phases run pdf --cases 00001 00005 00008
```

See `python run_validation.py --help` for all options.

---

## Pipeline phases

### Phase: setup

Runs the one-time case-preparation pipeline.  Only needed when starting fresh
or changing case-selection parameters (year range, area threshold, etc.).

| Step | Script | Output |
|------|--------|--------|
| 1 | `processScarsAndPoints.py` | `perimeters_ignitions.gpkg`, `all_ignitions.gpkg` |
| 2 | `separateScarsAndPointsToCases.py` | `00001/`, `00002/`, …, `fire_pairs_summary.csv` |
| 3 | `getSatelliteEndTimes.py` | `fire_pairs_summary_with_satellite.csv` |
| 4 | `eraseInvalidCases.py` | removes cases shorter than `MIN_HOURS_DURATION` h |
| 5 | `write_metadata_from_summary` | `case_metadata.json` in each folder |

Use `--setup-clean` to delete all generated files first (full reset).

### Phase: run

Executes the per-case simulation pipeline in parallel using
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
Step 11  runFarsiteCase                 →  farsite/outputs/farsite_Arrival Time.tif
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
Step 10  runFarsiteCase                 →  farsite/outputs/farsite_Arrival Time.tif
                                            farsite/outputs/farsite_WindGrids.tif
Step 11  farsiteWindToGeotiff           →  inputs/{ws,wd}.tif  (from FARSITE wind grids)
                                            deletes farsite_WindGrids.tif after extraction
Step 12  runElmfireCase                 →  outputs/time_of_arrival_*.tif
```

### Phase: pdf

Generates `validation.pdf` in `FIRE_ROOT`.  The report contains one page per
case plus four summary pages:

| Page | Content |
|------|---------|
| Per-case | Burn-mask map, area-evolution curves, metrics table |
| Summary 1 | Similarity scores vs. average wind speed |
| Summary 2 | Similarity scores vs. burn area and fire duration (log x) |
| Summary 3 | Area estimation bias per case (asymmetric log₂ bar chart) |
| Summary 4 | Similarity score distribution histograms |

### Phase: clean

Deletes per-case simulation outputs so cases can be re-run.  Preserves
`LANDFIRE.tif`, `case_metadata.json`, and fire geometry files.

```bash
# Clean from landfire_bands step onwards (default):
python run_validation.py --phases clean

# Clean only ELMFIRE outputs, keep everything else:
python run_validation.py --phases clean --clean-from elmfire_outputs

# Also delete ws.tif / wd.tif:
python run_validation.py --phases clean --include-wind-tifs

# Preview what would be deleted without doing it:
python run_validation.py --phases clean --dry-run
```

Available `--clean-from` values:
`landfire_bands`, `phi_adj`, `weather`, `windninja`, `nelson`, `barrier`,
`elmfire_inputs`, `elmfire_outputs`

---

## FARSITE execution (Wine / WSL)

FARSITE runs as a Windows executable (`TestFARSITE.exe`) via Wine on Linux/WSL.
`runFarsiteCase.py` handles path translation (`/home/…` → `Z:\home\…`),
sets the required GDAL/PROJ environment variables, and verifies the sentinel
output (`farsite_Arrival Time.tif`) after the run.

After a successful FARSITE run, outputs are cleaned automatically:
- **`WINDNINJA_SOURCE = "install"`**: keeps only `farsite_Arrival Time.tif`;
  deletes all other FARSITE outputs including `farsite_WindGrids.tif`.
- **`WINDNINJA_SOURCE = "farsite"`**: keeps `farsite_Arrival Time.tif` +
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
│   ├── farsite_wine.txt                 Wine command-line (auto-generated)
│   ├── ignition.shp                     reprojected ignition point
│   ├── barrier.shp                      merged barrier polygons (optional)
│   └── outputs/
│       └── farsite_Arrival Time.tif     FARSITE time-of-arrival output (sentinel)
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
| 11. FARSITE | `FARSITE_FB_DIR`, `FARSITE_EXE_NAME` |
| 12. Barrier | road/water widths, OSM field names |
| 13. LFPS API | base URL, product list, poll parameters |
| 14. Nelson | path to C# executable |

---

## Troubleshooting

### LANDFIRE job fails or times out
- Check that `LANDFIRE_EMAIL` is registered with LFPS.
- Increase `LFPS_POLL_MAX_TRIES` (default 300 × 10 s ≈ 50 min).
- The LFPS service can be slow during peak hours; try again later.

### WindNinja fails
- Confirm `WINDNINJA_CONDA_ENV` matches the conda environment with `WindNinja_cli` on PATH.
- Check `inputs/windninja/chunk_000/windninja_cli.log` for the error message.
- Ensure the DEM covers the full fire domain.

### FARSITE fails (Wine)
- Confirm Wine is installed: `wine --version`.
- Confirm `FARSITE_FB_DIR` points to the SDK root containing `bin/TestFARSITE.exe`.
- Check `farsite/pipeline.log` for Wine error output.
- Run `wine TestFARSITE.exe` interactively to test the Wine prefix.

### ELMFIRE fails
- Confirm `ELMFIRE_EXE` is on PATH: `which elmfire`.
- Check `ELMFIRE_PATH_TO_GDAL` points to a directory containing `gdal_translate`.
- The `outputs/` and `scratch/` directories are created automatically; existing
  contents are overwritten on re-run.

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
  `windninja` step: `python run_validation.py --phases clean run --clean-from windninja`.
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

- Scripts in `serial/` are slower sequential versions kept for debugging.
  Scripts in `old_code/` are obsolete.
