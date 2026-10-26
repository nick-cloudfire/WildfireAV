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
│   ├── runPipelineParallel.py         ← per-case driver: step plan, pipeline.log, status.json
│   ├── runBatch.py                    ← parallel batch runner (one process per case)
│   ├── preflight.py                   ← environment checks run before a batch
│   ├── common.py                      ← helpers shared by the steps (skip/require/progress/…)
│   ├── getLandfireProductsForFireSim.py ← step 1:  download LANDFIRE from LFPS
│   ├── splitLandfireTifBands.py       ← step 2:  split multi-band LANDFIRE.tif
│   ├── makePhiAndAdjFiles.py          ← step 3:  create adj/phi rasters
│   ├── downloadWeatherData.py         ← step 4:  fetch ERA5 weather (OpenMeteo)
│   ├── liveFuelMoisture.py            ← step 4b: live fuel moisture from the NFDRS4 GSI
│   ├── runWindninja.py                ← step 5:  WindNinja (WINDNINJA_MODE = hrrrLocal | wxModel | wxsFile)
│   ├── downloadHrrr.py                ← helper: byte-range HRRR download → WindNinja pastcast zip
│   ├── wn_to_geotiff.py               ← helper: convert WindNinja ASCII → GeoTIFF
│   ├── applyNelsonModel.py            ← step 6:  compute dead-fuel moisture
│   ├── getBarrierFile.py              ← step 7:  rasterise road/water barriers
│   ├── createElmfireInputFiles.py     ← step 8:  write ELMFIRE namelist (.data)
│   ├── prepareFarsite.py              ← step 9:  create FARSITE inputs (LCP, etc.)
│   ├── runElmfireCase.py              ← step 10: execute ELMFIRE
│   └── runFarsiteCase.py              ← step 11: execute FARSITE (Wine)

# Validation report
├── validation/
│   └── getValidationPDF.py            ← generate multi-page validation PDF

# Developer / debug utilities
├── tools/
│   ├── monitorBatch.py                ← live status table from every case's status.json
│   ├── prefetchLandfire.py            ← pre-fetch LANDFIRE for all cases
│   ├── debugBandCounts.py             ← debug raster band counts per case
│   ├── debugSatelliteEndTimes.py      ← inspect satellite coverage curves
│   └── visualiseSingleCase.py         ← plot observed vs simulated for one case

# Data directories
# Input data (not version-controlled — see Input data requirements below)
├── inputs/
│   ├── mtbs_perimeters.gpkg           ← MTBS burn perimeters
│   ├── NIFC_FOD.gpkg                  ← NIFC fire occurrence points
│   ├── nasa_lance_allSatellites.gpkg  ← VIIRS/MODIS/Landsat hotspot detections
│   ├── osm_conus_roads.gpkg           ← OSM road network
│   ├── grwl.gpkg                      ← Global River Widths from Landsat waterways
│   └── osm_conus_rivers.gpkg          ← Backup river polygons
└── nelson_csharp/                     ← Nelson dead-fuel model (clone of Nelson-Dead-Fuel-Moisture)
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
| FARSITE Windows binary (`TestFARSITE.exe`) | `FARSITE_FB_DIR` / `FARSITE_EXE_NAME` |
| Wine (`wine64`) | on `$PATH` |
| Nelson C# model | `pipelineConfig.NELSON_EXE` |
| LFPS API access (USGS email) | `LFPS_EMAIL` environment variable |
| OpenMeteo ERA5 (free, no key) | `pipelineConfig.OPENMETEO_URL` |

---

## WSL setup

This pipeline runs on Linux: a laptop under **Windows Subsystem for Linux
(WSL2)** with Ubuntu 24.04, or an HPC cluster (SLURM, see
[Running on an HPC](#running-on-an-hpc)).  FARSITE is the Windows
`TestFARSITE.exe`, run through Wine.

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
The compiled `elmfire` executable must be on PATH inside the conda
environment.  `ELMFIRE_PATH_TO_GDAL` (the GDAL tools ELMFIRE calls) defaults to
the running environment's `bin/`, falling back to wherever `gdal_translate` is
on PATH; override it only if ELMFIRE needs a different GDAL.

### 5. Install WindNinja

Follow the instructions in the [WindNinja Github](https://github.com/firelab/windninja/wiki/Building-WindNinja-on-Linux-22.04). 
Note that the pipeline was setup in Ubuntu-24.04, when asked to run scripts for 22.04, run the provided files for 24.04 instead.
No need to install the GUI version. 

By default (`WINDNINJA_CONDA_ENV = None`) `WindNinja_cli` is taken from PATH,
i.e. the environment the pipeline runs in.  If it lives in a different conda
environment, set `WINDNINJA_CONDA_ENV` to that name and it is called through
`conda run -n <env>`.  `python pipeline/preflight.py` tells you which one it found.

With `WINDNINJA_MODE = "hrrrLocal"` (default) the pipeline downloads only the
HRRR analysis hours it needs (TMP/UGRD/VGRD/TCDC, byte ranges from the public
archive on Google Cloud, no credentials), crops them to the case and hands them
to WindNinja as a `PASTCAST-GCP-HRRR-CONUS-3-KM_*.zip`.  `"wxModel"` lets
WindNinja download the pastcast itself; `"wxsFile"` is the old domain-average
initialisation from `weather.wxs`.

### 6. Install Wine and the FARSITE binary

FARSITE is the Windows `TestFARSITE.exe` (not included in this repository),
run with `WINEDEBUG=-all wine64 TestFARSITE.exe farsite.txt` from each case's
`farsite/` folder.

```bash
sudo apt install -y wine64
mkdir -p ~/farsite/bin && cp TestFARSITE.exe ~/farsite/bin/   # default FARSITE_FB_DIR
```

If it lives elsewhere, set `FARSITE_FB_DIR` / `FARSITE_EXE_NAME` in
`pipelineConfig_local.py`.

### 7. Clone and configure

```bash
cd /home/<user>/elmfire_validation
git clone https://github.com/nick-cloudfire/wildfireAV.git Data
cd Data
```

### 8. Install the Nelson dead-fuel moisture model

Clone the Nelson dead-fuel moisture model made for this pipeline (based on
WUINITY-PREACT, Copyright (C) 2025 Jonathan Wahlqvist) into `nelson_csharp/`
and build it:

```bash
git clone https://github.com/nick-cloudfire/Nelson-Dead-Fuel-Moisture.git nelson_csharp

# Install .NET 8 SDK
sudo apt install -y dotnet-sdk-8.0

# Build the release binary
cd nelson_csharp
dotnet publish -c Release
```
The compiled binary path is set automatically by `pipelineConfig.NELSON_EXE`.

### 9. Machine-specific settings

The repository root and every derived path are detected automatically.  Put
only the settings that differ on this machine in `pipelineConfig_local.py`
(gitignored) next to `pipelineConfig.py`, e.g. on an HPC:

```python
from pathlib import Path
FIRE_ROOT          = Path("/scratch/<user>/FirePairs")
MAX_PARALLEL_CASES = 96
```

Any scalar setting can also be set per job with `WAV_<NAME>` environment
variables (`export WAV_FIRE_ROOT=/scratch/$USER/FirePairs`).
`python pipeline/preflight.py` lists the active overrides and flags local keys
the config no longer knows.

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
`LFPS_EMAIL` environment variable (see WSL setup step 9).  On an HPC whose
compute nodes have no internet, run `tools/prefetchLandfire.py` on the login
node first.

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
└── nelson_csharp/                (built from source — see WSL setup step 8)

# FARSITE binary lives outside this repo (see WSL setup step 6):
~/farsite/bin/TestFARSITE.exe
```

---

## Quick-start

### 1. Configure

Defaults work out of the box (cases go to `../FirePairs`).  Put what you want
to change in `pipelineConfig_local.py`, e.g.:

```python
from pathlib import Path
FIRE_ROOT          = Path("/scratch/yourname/FirePairs")
MIN_FIRE_YEAR      = 2024
MAX_FIRE_YEAR      = 2025
MAX_PARALLEL_CASES = 14          # 0 = one per available CPU (SLURM-aware)
```

Then check the environment: `python pipeline/preflight.py`.

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

# Resume after a timeout / crash: keep outputs, skip finished cases
./runWildfireAV --start run --resume --workers 8

# Process specific cases only (leading zeros optional):
./runWildfireAV --start run --cases 1 5 8

# What would run, and each case's last state / failing step:
./runWildfireAV --start run --dry-run
```

See `./runWildfireAV --help` for all options, and
[Running on an HPC](#running-on-an-hpc) for what the output looks like.

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

Runs the preflight checks, cleans per-case simulation outputs (from
`landfire_bands` onwards — skipped with `--resume`), then runs every case in
its own process, `--workers` at a time.  Every step skips itself when its
outputs already exist (outputs are written atomically, so a killed case never
leaves a half-written file behind), so re-running resumes where a case
stopped.  `python pipeline/runPipelineParallel.py --list-steps` prints the
active step plan:

```
Step 1   getLandfireProductsForFireSim  →  LANDFIRE.tif
Step 2   splitLandfireTifBands          →  inputs/{dem,slp,asp,fbfm40,cc,ch,cbh,cbd}.tif
Step 3   makePhiAndAdjFiles             →  inputs/{adj,phi}.tif
Step 4   downloadWeatherData            →  inputs/weather.wxs
Step 4b  liveFuelMoisture               →  inputs/live_fuel_moisture.json  (GSI live herb/woody %)
Step 5   runWindninja                   →  inputs/{ws,wd}.tif  (WindNinja)
Step 6   applyNelsonModel               →  inputs/{m1,m10,m100}.tif
Step 7   getBarrierFile                 →  inputs/barrier.tif
Step 8   createElmfireInputFiles        →  <case>.data
Step 9   prepareFarsite                 →  farsite/{landscape.lcp, farsite.input, …}
Step 10  runElmfireCase                 →  outputs/time_of_arrival_*.tif
Step 11  runFarsiteCase                 →  farsite/outputs/farsite_Arrival Time.tif
```

#### Live fuel moisture

Each case gets one live herbaceous and one live woody moisture, used by both
ELMFIRE (`LH_/LW_MOISTURE_CONTENT`) and FARSITE (the live columns of every
fuel-moisture line).  With `LIVE_FUEL_MOISTURE_SOURCE = "gsi"` (default) they
come from the Growing Season Index (Jolly, Nemani & Running 2005, *Global
Change Biology* 11:619–632) as implemented in NFDRS4's `LiveFuelMoisture`:
the product of minimum-temperature, VPD and daylength ramps, averaged over 21
days and mapped onto 30–250 % (herb) / 60–200 % (woody) above a greenup
threshold of 0.5, with the annual-herb curing rule.  It is marched over ERA5 at
the DEM centre from 1 December of the previous year to the simulation start.
The Python port matches the NFDRS4 C++ to within 1e-4 % on two years of ERA5.
Parameters are in `pipelineConfig.py` section 14; set the source to
`"constant"` to use `LIVE_HERB_MC` / `LIVE_WOODY_MC` instead.

### Running on an HPC

The batch log (terminal or SLURM `.out`) is designed to be read on its own:

```
PREFLIGHT
  [  ok] exe elmfire            /home/me/bin/elmfire
  [FAIL] net OpenMeteo          https://archive-api.open-meteo.com/v1/era5 (ConnectTimeout; needed by 40 case(s))
  [warn] CPUs                   16 available (SLURM_CPUS_PER_TASK=16), 14 worker(s) x 1 thread(s)

12:00:01  START  00012   [14 running, 86 queued]
12:41:10  OK     00012   41m09s   [12/100 done, 1 failed, ETA ~5h10m]
12:46:00  FAIL   00013   46m02s   [13/100 done, 2 failed, ETA ~5h05m]
            at step windninja: RuntimeError: WindNinja chunk 1/2 failed (exit 1); see …/windninja_cli.log
            log: /scratch/me/FirePairs/00013/pipeline.log
12:50:00  ····  50m elapsed · 13/100 done, 2 failed, ETA ~5h · 14 running
            00014   5/11 windninja        12m  HRRR download 180/312 h (3m40s)
            00015  10/11 run_elmfire      31m
```

- **Preflight** (seconds; up to 2 min on a first Wine run) checks that the
  executables exist and start (`ldd`, Nelson's .NET runtime, FARSITE under
  Wine, WindNinja's PROJ database), `LFPS_EMAIL`, barrier datasets,
  internet access to LFPS / OpenMeteo / HRRR (compute nodes often have none),
  free disk and CPU oversubscription — only for what the cases still need.
  `--no-preflight` skips it.
- **Heartbeat** every `BATCH_HEARTBEAT_MIN` (`--heartbeat`) minutes: every
  running case, its step and live progress.
- **Final report**: counts, median/max time per step, failures grouped by
  step with the exact `--cases …` to re-run them, and a
  `run_summary_<timestamp>.csv` (per-case state, error, per-step seconds) in
  `FIRE_ROOT`.
- **Exit status**: 0 all ok, 1 some case failed (so `--dependency=afterok`
  works), 2 preflight failed, 143 interrupted.
- **Time limits**: on SIGTERM (SLURM time limit, `scancel`) running cases are
  stopped, marked `interrupted` and queued ones are not started; add
  `#SBATCH --signal=TERM@300` and resubmit with `--resume`.  A case killed
  outright (out of memory) is reported as `killed by SIGKILL — most likely out
  of memory` without affecting the others.
- Each case writes a timestamped `pipeline.log` (including subprocess output)
  and a `status.json`.  `python tools/monitorBatch.py` shows a live table from
  them (`--once` for a single print, `--failed` for failures only); it also
  flags `dead` cases whose process vanished.

### Phase: pdf (`--start pdf`)

Generates `validation_report.pdf` in `FIRE_ROOT`.  The report contains a cover
page, four summary pages and one page per case:

| Page | Content |
|------|---------|
| Cover | Case counts (successful / non-ignited / failed) |
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
python cleanPipelineOutputs.py --dry-run                     # show what would be deleted
python cleanPipelineOutputs.py                               # everything after LANDFIRE.tif
python cleanPipelineOutputs.py --from-step elmfire_outputs   # only ELMFIRE outputs
python cleanPipelineOutputs.py --from-step windninja --include-wind-tifs --cases 00013
```

---

## FARSITE execution (Wine)

`prepareFarsite.py` writes `farsite.txt`, a one-line command file with paths
relative to the case's `farsite/` folder, and `farsite.input` with all run
parameters and inline weather/moisture.  `runFarsiteCase.py` runs
`WINEDEBUG=-all wine64 TestFARSITE.exe farsite.txt` there, with the
`FARSITE_TIMEOUT_H` limit (on timeout the whole process group, including
`wineserver` children, is killed).  The completion sentinel is
`farsite/outputs/farsite_Arrival Time.tif`.

FARSITE gets the same WindNinja winds as ELMFIRE (`winds.atm` + ASCII grids
built from `ws.tif`/`wd.tif`).  After a successful run only
`farsite_Arrival Time.tif` is kept.

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
├── pipeline.log                         timestamped output of every step of the last run
├── status.json                          live/last state: step, progress, per-step times, error
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
│   ├── farsite.txt                      one-line FARSITE command file
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
| 1. User-modifiable | `MIN/MAX_FIRE_YEAR`, `MTBS_AREA_THRESHOLD_ACRES`, `MAX_PARALLEL_CASES`, `BATCH_HEARTBEAT_MIN`, `PREFLIGHT_MIN_FREE_GB`, `*_TIMEOUT_H` |
| 2. Paths | `FIRE_ROOT`, `INPUTS_DATA_ROOT`, `FARSITE_FB_DIR` / `FARSITE_EXE_NAME`, derived input/output paths |
| 3. File/dir names | CSV names, shapefile names, subfolder names |
| 4. Column names | master CSV column keys shared across scripts |
| 5. MTBS / USFS | field names in raw input datasets |
| 6. LANDFIRE | band order, product names, raster naming |
| 7. Satellite | coverage fraction, gap tolerance, buffer size |
| 8. Weather | OpenMeteo URL and model |
| 9. WindNinja | `WINDNINJA_MODE`, conda env, chunk size, output height, mesh resolution factor, `HRRR_*` download settings |
| 10. ELMFIRE | exe, GDAL path, simulation parameters |
| 11. Barrier | road/water widths, OSM field names |
| 12. LFPS API | base URL, products, polling, `LFPS_JOB_TIMEOUT_H`, `LFPS_MAX_ATTEMPTS` |
| 13. Nelson | path to C# executable (derived, section 2) |
| 14. Live fuel moisture | `LIVE_FUEL_MOISTURE_SOURCE`, constant values, `GSI_*` model parameters |

---

## Troubleshooting

### LANDFIRE job is stuck in queue
- The LFPS queue can be very long when many users are submitting jobs simultaneously.
  By default (`LFPS_JOB_TIMEOUT_H = 0`) the pipeline waits however long the
  queue takes; network blips keep polling the same job.
- A "still waiting…" heartbeat is logged every `LFPS_POLL_HEARTBEAT_S` seconds
  (default 1800 s / 30 min) so you can confirm the job is still live.
- Do **not** kill and resubmit the job — that adds another entry to the queue and
  makes the wait longer for everyone.
- New submissions (up to `LFPS_MAX_ATTEMPTS = 3`) are reserved for genuine
  failures (job status `Failed`, a timed-out job, a bad download), not queue waits.
- Check that `LFPS_EMAIL` is set in your shell (`echo $LFPS_EMAIL`) and that the
  address is registered at https://lfps.usgs.gov.

### OpenMeteo weather download fails with 502 / 503 / 504
- These are transient gateway errors from the OpenMeteo server.  The pipeline
  automatically retries up to 6 times with exponential back-off (5 s → 10 s → 20 s
  → 40 s → 60 s → 60 s).  No action needed unless all 6 attempts fail.
- A permanent failure (any other 4xx status) is not retried and will appear in
  `pipeline.log` with the URL and response body.

### WindNinja fails
- The last lines of `windninja_cli.log` are copied into `pipeline.log`; the
  full log is in `inputs/windninja/chunk_NNN/` (or `step_NNN/` in wxsFile mode).
- Exit 127 / "command not found": `WindNinja_cli` is not on PATH in the job's
  environment — activate its env in the job script, or set `WINDNINJA_CONDA_ENV`.
- `PROJ: proj_create_from_database: Open of …/share/proj failed` (followed by
  "Unable to calculate angle departure from north"): WindNinja can't open its
  PROJ database, so wind directions aren't corrected for grid north.  Run
  WindNinja from the pipeline's own env (`WINDNINJA_CONDA_ENV = None`, with
  `WindNinja_cli` installed in that env) and check
  `PROJ_DATA=$CONDA_PREFIX/share/proj` (older PROJ: `PROJ_LIB`).  On an HPC
  with `proj.db` on a network filesystem, copying `share/proj` to node-local
  disk at job start and pointing `PROJ_DATA` there also helps.
- "Failed to connect to ninjastorm.firelab.org" is WindNinja's version check
  and harmless on nodes without internet.
- Ensure the DEM covers the full fire domain.

### FARSITE fails
- `python pipeline/preflight.py` checks that `wine64` is on PATH and that
  `FARSITE_FB_DIR/FARSITE_EXE_NAME` exists.
- Check the case's `pipeline.log` for FARSITE's output.
- Re-run it by hand: `cd <case>/farsite && wine64 ~/farsite/bin/TestFARSITE.exe farsite.txt`
  (drop `WINEDEBUG=-all` to see Wine's messages).

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
- WindNinja creates `chunk_NNN/` (or `step_NNN/`) subdirectories and an
  `hrrr/` download cache.  These are deleted automatically after `ws.tif` and
  `wd.tif` are built; an interrupted run keeps the HRRR cache and resumes with
  `./runWildfireAV --start run --resume`.
- All GeoTIFF outputs use LZW compression + tiling.

---

## Development notes

- **`pipelineConfig.py` is the single source of truth.**  Never hard-code
  paths, filenames, or tunable numbers inside individual scripts.

- **Step contract** (see `pipeline/common.py`): every step module has
  `main(case_dir=None)`.  With a case it processes that case and *raises* on
  any problem (`require(...)` for missing inputs), returns `skipped(...)` when
  its outputs already exist, writes its outputs with `atomic_write(...)`, and
  reports progress of long operations with `progress(...)` (shown in the
  heartbeat and monitor).  Without a case it loops over every case under
  `FIRE_ROOT`.  The step order lives in `runPipelineParallel.plan()`.

- **Run one case by hand**: `python pipeline/runPipelineParallel.py /path/to/00012`
  (prints to the terminal and `pipeline.log`), or a single step with
  `python -c "import sys; sys.path[:0]=['.','pipeline']; import runWindninja; runWindninja.main('/path/to/00012')"`.

- **`parallel_api.py`** provides `Tee`, `run_subprocess`, `retry_call` and
  `get_thread_session`.  Import from here instead of reimplementing locally.

- **`case_metadata.py`** handles all JSON serialisation of per-case metadata,
  including datetime normalisation.

- Scripts in `tools/` are standalone dev/debug utilities and do not participate
  in the main pipeline.
