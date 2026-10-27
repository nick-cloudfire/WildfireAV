# WildfireAV — automated validation of fire spread models

WildfireAV runs [ELMFIRE](https://github.com/lautenberger/elmfire) and
[FARSITE](https://www.firelab.org/project/farsite) on real U.S. wildfires
and scores how well each model reproduced the burned area.

It does the whole chain without hand editing: it picks fires from the
national records, builds every model input from public data, runs both
models with the same inputs, and writes a report comparing each simulation
with the mapped burn perimeter. Because nothing is tuned per fire, you get
a model-vs-model and model-vs-reality comparison over many fires, not a
few hand-calibrated case studies.

```
 national records          per-fire inputs                     models          comparison
 ────────────────         ─────────────────                   ────────        ────────────
 MTBS perimeters  ─┐      LANDFIRE fuels & terrain       ─┐
 NIFC ignitions   ─┼─►    ERA5 weather (20-day spin-up)   ├─►  ELMFIRE ─┐
 VIIRS/MODIS hot- ─┘      HRRR → WindNinja winds          │            ├─► burn-area scores,
 spots (timing)           Nelson dead fuel moisture       ├─►  FARSITE ─┘   validation report
                          GSI live fuel moisture          │
                          road / river barriers          ─┘
```

---

## Contents

- [How a fire becomes a validation case](#how-a-fire-becomes-a-validation-case)
- [Model inputs](#model-inputs)
- [How the comparison is scored](#how-the-comparison-is-scored)
- [Assumptions and limitations](#assumptions-and-limitations)
- [Installation](#installation)
- [Input data](#input-data)
- [Running](#running)
- [Running on an HPC cluster](#running-on-an-hpc-cluster)
- [Outputs](#outputs)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)
- [Acknowledgements and license](#acknowledgements-and-license)

---

## How a fire becomes a validation case

`runWildfireAV --start setup` turns the national datasets into numbered case
folders (`FirePairs/00001/`, …). The settings named below are in
`pipelineConfig.py`.

1. **Burn perimeter.** [MTBS](https://www.mtbs.gov) perimeters larger than
   `MTBS_AREA_THRESHOLD_ACRES` (5,000 ac) with an ignition date between
   `MIN_FIRE_YEAR` and `MAX_FIRE_YEAR`. This is the observed final burned area.
2. **Ignition point.** The perimeter is matched to a record in the NIFC fire
   occurrence database with the same name, a discovery date within
   `DAY_TOLERANCE_DAYS` (±2 days), and a location inside the perimeter. The
   reported discovery location is the simulated ignition point.
3. **Start and end times from satellites.** VIIRS and MODIS active-fire
   detections inside the perimeter are buffered by `SAT_HOTSPOT_BUFFER_DIST`
   (200 m), and their covered area is accumulated overpass by overpass.
   Overpasses count as one burning episode while no gap is longer than
   `SAT_CHAIN_MAX_GAP_DAYS` (7 days).
   - **Start (satellite ignition):** coverage reaches 5 % of the fire's area.
   - **End:** coverage reaches `COVERAGE_FRACTION` (90 %).
   - The simulation runs from the start to the end, or to the reported
     fire-out time if that is earlier.
   - The fire's area here is the smaller of the perimeter area and the final
     covered area, so fires with sparse detections can still reach 90 %.
4. **Filter.** Fires burning less than `MIN_HOURS_DURATION` (12 h) by
   satellite are dropped.

Satellite timing is used instead of the reported discovery time. The
reported time is often hours or days away from the main spread, which would
penalise the models for an error in the record. The coverage curve is
implemented once, in `satellite.py`, and used by both setup and the report.

## Model inputs

Every case gets the same inputs for both models, on the LANDFIRE 30 m grid in
the local UTM zone.

| Input | Source | Notes |
|---|---|---|
| Terrain, fuel model (FBFM40), canopy cover / height / base height / bulk density | [LANDFIRE](https://landfire.gov) via the LFPS web service | Latest LANDFIRE release not newer than the fire year (2016, 2022, 2023, 2024). Domain = perimeter bounding box widened by `EXPAND` (1.5×) on each axis. |
| Surface weather | ERA5 reanalysis via [Open-Meteo](https://open-meteo.com), hourly, at the domain centre | Starts `CONDITIONING_DAYS` (20) before ignition, so fuel moisture models can spin up. |
| Winds | [HRRR](https://rapidrefresh.noaa.gov/hrrr/) 3 km hourly analyses, downscaled with [WindNinja](https://www.firelab.org/project/windninja) | Mass-conserving solver with diurnal slope flows and non-neutral stability. Output at 20 ft, 30 m cells (mesh 120 m). One wind field per hour. |
| Dead fuel moisture (1, 10, 100 h) | Nelson model (`nelson_csharp`) | Per cell and per hour. Solar radiation depends on slope, aspect and canopy cover. Spun up over the 20 conditioning days. |
| Live herbaceous / woody moisture | NFDRS4 Growing Season Index | One value per fire, from minimum temperature, vapour pressure deficit and daylength (Jolly et al. 2005). See [Live fuel moisture](#live-fuel-moisture). |
| Barriers | OpenStreetMap roads and waterways | Each road/river class is buffered to a nominal width (`ROAD_WIDTHS_M`, `WATER_WIDTHS_M`). A secondary rivers dataset fills gaps. |

FARSITE receives the same per-hour WindNinja grids (through an ATM file), the
same weather and live moisture, and initial dead fuel moisture averaged per
fuel model from the Nelson grids. FARSITE has no per-cell dead moisture input.

**One clock for all hourly inputs.**
- Band 1 of the wind (`ws/wd.tif`) and dead fuel moisture (`m1/m10/m100.tif`)
  rasters is the hour containing the satellite ignition. Band *i* is *i* − 1
  hours later.
- Each band's UTC time is stored in the GeoTIFF; see it in QGIS or with
  `gdalinfo`.
- Both models simulate the same window, from satellite ignition to end.
  - ELMFIRE reads band *i* during simulation hour *i*.
  - FARSITE switches wind grids at their timestamps.
- The ELMFIRE and FARSITE input steps refuse rasters that start at a
  different hour. `python tools/checkHourlyInputs.py` checks every case.

### Live fuel moisture

The Growing Season Index (Jolly, Nemani & Running 2005, *Global Change
Biology* 11:619–632) is the product of three 0–1 ramps:

- minimum temperature: −2 → 5 °C
- vapour pressure deficit at the daily maximum temperature: 900 → 4100 Pa
- daylength: 10 → 11 h

It is averaged over 21 days. Above a greenup threshold of 0.5, the live
moisture rises linearly from the minimum to the maximum:

| Fuel | Minimum | Maximum |
|---|---|---|
| Herbaceous | 30 % | 250 % |
| Woody | 60 % | 200 % |

Annual herbaceous fuel cures for the rest of the year once it falls below
120 % after greening up.

`pipeline/liveFuelMoisture.py` is a Python port of NFDRS4's
`LiveFuelMoisture`. It matches the C++ to within 1e-4 % on two years of ERA5.
It runs from 1 December of the year before the fire to the ignition date.
Set `LIVE_FUEL_MOISTURE_SOURCE = "constant"` to use fixed values
(`LIVE_HERB_MC`, `LIVE_WOODY_MC`) instead.

## How the comparison is scored

Each model's time-of-arrival raster is turned into a burned / unburned mask
and compared, cell by cell, with the rasterised MTBS perimeter. Here TP is a
cell burned in both, FP is burned only in the simulation, and FN is burned
only in the observation.

| Score | Formula | Meaning |
|---|---|---|
| Jaccard | TP / (TP + FP + FN) | Overlap divided by union; 1 = identical, 0 = no overlap |
| Sørensen (Dice) | 2 TP / (2 TP + FP + FN) | Like Jaccard but weights the overlap twice |
| Cohen's κ | (agreement − chance) / (1 − chance) | Agreement beyond what random placement would give |
| Area ratio | simulated / observed area | > 1 over-prediction, < 1 under-prediction |

A case whose best Jaccard is below 0.01 in every model is reported as *did not
ignite* and left out of the summary statistics.

## Assumptions and limitations

Read the scores with these in mind:

- **One ignition point, no suppression.** Each fire is simulated from the
  reported discovery location as a free-burning fire. Containment lines,
  backfires and multiple ignitions are not represented, beyond the road and
  river barriers.
- **The observed perimeter is the final perimeter.** MTBS maps the burn scar
  after the fire, and it includes unburned islands within its outer boundary.
  The scores therefore compare final areas, not the progression. The report
  shows the timing separately as area-growth curves against the satellite
  detections.
- **Weather is a single point.** Temperature, humidity and precipitation come
  from ERA5 at the domain centre. Wind is spatial (WindNinja) but hourly.
- **Spotting is off** in both models. FARSITE uses Finney's crown fire method
  with 100 % foliar moisture.
- **Live fuel moisture is one value per fire.** The GSI has no terrain or fuel
  type input.
- **Fires are U.S. only,** because the inputs depend on LANDFIRE, HRRR and
  MTBS.

---

## Installation

Tested on Ubuntu 24.04 (native, WSL2 on Windows, and a SLURM cluster).

**1. Python environment** (includes GDAL with its GRIB driver and WindNinja):

```bash
git clone https://github.com/nick-cloudfire/WildfireAV.git
cd WildfireAV
conda env create -f environment.yml
conda activate wildfireav
```

**2. ELMFIRE.** Build it following the
[ELMFIRE instructions](https://github.com/lautenberger/elmfire) and put
`elmfire` on your `PATH`.

**3. FARSITE.** The pipeline runs the Windows command-line FARSITE
(`TestFARSITE.exe`) under Wine. The executable is not included here.

```bash
sudo apt install -y wine64
mkdir -p ~/farsite/bin && cp TestFARSITE.exe ~/farsite/bin/   # default location
```

**4. Nelson dead fuel moisture model.** This is a C# port written for this
pipeline. It needs the .NET 8 SDK.

```bash
git clone https://github.com/nick-cloudfire/Nelson-Dead-Fuel-Moisture.git nelson_csharp
sudo apt install -y dotnet-sdk-8.0
(cd nelson_csharp && dotnet publish -c Release)
```

**5. LANDFIRE access.** Register an email at the
[LANDFIRE Product Service](https://lfps.usgs.gov) and export it:

```bash
export LFPS_EMAIL=you@example.com     # e.g. in ~/.bashrc or the job script
```

**6. Check.** Run `python pipeline/preflight.py`. It reports anything
missing:
- programs that are absent or won't start
- an unset `LFPS_EMAIL`
- missing datasets
- no internet access
- too little disk space or too few CPUs

## Input data

Download these national datasets once and put them in `inputs/` (not
version-controlled):

| File | Contents | Source |
|---|---|---|
| `mtbs_perimeters.gpkg` | Burn perimeters (`Incid_Name`, `Ig_Date`, `BurnBndAc`) | [MTBS direct download](https://www.mtbs.gov/direct-download) |
| `NIFC_FOD.gpkg` | Fire occurrence points (`IncidentName`, `FireDiscoveryDateTime`, `FireOutDateTime`) | [NIFC open data](https://data-nifc.opendata.arcgis.com/) |
| `nasa_lance_allSatellites.gpkg` | VIIRS/MODIS detections, layer `output`, columns `ACQ_DATE`, `ACQ_TIME` (HHMM UTC) | [NASA FIRMS archive](https://firms.modaps.eosdis.nasa.gov/download/), merged into one layer |
| `osm_conus_roads.gpkg` | Road lines, layer `lines`, field `highway` | OpenStreetMap extract, e.g. [Geofabrik](https://download.geofabrik.de/) |
| `grwl.gpkg` | Waterway lines, layer `lines`, field `waterway` | |
| `osm_conus_rivers.gpkg` | Secondary river lines (optional, fills gaps) | |

The pipeline downloads LANDFIRE, ERA5 and HRRR itself for each fire.

## Running

`runWildfireAV` runs three phases in order: **setup → run → pdf**.
`--start` picks the first one.

```bash
./runWildfireAV --start setup            # select fires, build case folders, then run and report
./runWildfireAV --start run              # re-run every case from scratch, then report
./runWildfireAV --start run --resume     # continue: keep outputs, skip finished cases
./runWildfireAV --start run --cases 1 5  # only these cases
./runWildfireAV --start run --test       # quick test: first 10 cases, own report (validation_report_test.pdf)
./runWildfireAV --start run --dry-run    # list cases and their last state
./runWildfireAV --start pdf              # rebuild the report only
```

Each case runs as its own process, `--workers` at a time (default: one per
CPU). Every step skips itself when its output already exists, so a re-run
picks up where a case stopped. You can also run single steps and cases:

```bash
python pipeline/runPipelineParallel.py --list-steps          # the step plan
python pipeline/runPipelineParallel.py /path/to/FirePairs/00012
python validation/getValidationPDF.py --cases 12             # one-case report
python cleanPipelineOutputs.py --from-step run_elmfire --dry-run
```

The per-case steps are:

| # | Step | Writes (in the case folder) |
|---|---|---|
| 1 | `landfire` | `LANDFIRE.tif` |
| 2 | `split_bands` | `inputs/{dem,slp,asp,fbfm40,cc,ch,cbh,cbd}.tif` |
| 3 | `adj_phi` | `inputs/{adj,phi}.tif` (ELMFIRE adjustment factors, all 1) |
| 4 | `weather` | `inputs/weather.wxs` |
| 5 | `live_moisture` | `inputs/live_fuel_moisture.json` |
| 6 | `hrrr` | `inputs/windninja/hrrr/` (HRRR hours, cropped to the domain) |
| 7 | `windninja` | `inputs/{ws,wd}.tif` (one band per hour) |
| 8 | `nelson` | `inputs/{m1,m10,m100}.tif` (one band per hour) |
| 9 | `barrier` | `inputs/barrier.tif` (barrier width, m) |
| 10 | `elmfire_inputs` | `<case>.data` (ELMFIRE namelist) |
| 11 | `prepare_farsite` | `farsite/` (landscape, weather, wind grids, fuel moisture) |
| 12 | `run_elmfire` | `outputs/time_of_arrival_*.tif` |
| 13 | `run_farsite` | `farsite/outputs/farsite_Arrival Time.tif` |

## Running on an HPC cluster

The pipeline is designed for SLURM.

- The batch log (terminal or `slurm-*.out`) reports each case as `START`,
  `OK` or `FAIL`.
- A heartbeat line every `BATCH_HEARTBEAT_MIN` minutes shows what each running
  case is doing.
- At the end it prints step timings, groups failures by step, and gives the
  exact `--cases …` command to re-run the failed ones.
- `python tools/monitorBatch.py` shows a live table, built from each case's
  `status.json`.

```bash
#SBATCH --cpus-per-task=16
#SBATCH --time=24:00:00
#SBATCH --signal=TERM@300        # running cases record 'interrupted' before the time limit
conda activate wildfireav
export LFPS_EMAIL=you@example.com
python -u runWildfireAV --start run --resume
```

- **No internet on compute nodes?** Run the download steps on the login node
  first, then submit the rest:
  ```bash
  ./runWildfireAV --start run --download-only   # login node: LANDFIRE, ERA5, HRRR
  sbatch job.sh                                  # job runs: runWildfireAV --start run --resume
  ```
- **Machine-specific settings** go in `pipelineConfig_local.py` next to
  `pipelineConfig.py` (not version-controlled). List only what differs, e.g.
  `FIRE_ROOT = Path("/scratch/me/FirePairs")`. Any setting can also be set
  per job as `export WAV_<NAME>=…`.
- **Time limits:** external programs have their own limits (`*_TIMEOUT_H`), so
  a hung program fails its case instead of holding a worker. On SIGTERM,
  running cases stop cleanly; resubmit with `--resume`.
- **Exit codes:** 0 all ok, 1 some case failed, 2 preflight failed,
  143 interrupted.

## Outputs

In `FIRE_ROOT` (default `../FirePairs`):

| File | Contents |
|---|---|
| `validation_report.pdf` | Cover (best/worst cases), scores vs. wind speed, fire size and duration, area bias per case, score histograms, then one page per case |
| `validation_metrics.csv` | One row per case: observed and simulated areas, Jaccard, Sørensen, κ, area ratio, mean wind |
| `fire_pairs_summary_with_satellite.csv` | Every case with its ignition, satellite start/end and fire-out times |
| `run_summary_<time>.csv` | Per-case state, error and step timings of each batch run |
| `hrrr_cache/` | HRRR hours shared between cases (safe to delete) |

Each case page shows:
- a map of the simulated burned areas and arrival-time isochrones of both
  models over the observed perimeter, with the ignition point and barriers
- the scores
- area-growth curves of both models against simulation time
- the satellite-detected area against date

Each case folder holds `case_metadata.json` (name, times, areas) and all
inputs and outputs from the step table above. It also has `pipeline.log`, a
timestamped log of every step including the external programs' output, and
`status.json`.

## Configuration

All settings live in `pipelineConfig.py`, grouped and commented. The ones
you're most likely to change:

| Setting | Default | Effect |
|---|---|---|
| `MIN_FIRE_YEAR`, `MAX_FIRE_YEAR` | 2024, 2025 | Which fires are selected |
| `MTBS_AREA_THRESHOLD_ACRES` | 5000 | Minimum fire size |
| `COVERAGE_FRACTION` | 0.9 | Satellite coverage that ends the simulation |
| `CONDITIONING_DAYS` | 20 | Weather spin-up before ignition |
| `WINDNINJA_MODE` | `hrrrLocal` | `hrrrLocal` (HRRR, downloaded by the pipeline), `wxModel` (HRRR, downloaded by WindNinja), `wxsFile` (ERA5 point wind, uniform per hour) |
| `LIVE_FUEL_MOISTURE_SOURCE` | `gsi` | `gsi` or `constant` |
| `ROAD_WIDTHS_M`, `WATER_WIDTHS_M` | | Barrier width per road / waterway class |
| `ELMFIRE_*`, `FARSITE_*` | | Model settings and executable locations |
| `MAX_PARALLEL_CASES` | 0 = one per CPU | Cases run at once |
| `TEST_RUN_CASES` | 10 | Cases run with `--test` |

`python pipeline/preflight.py` lists the settings you've overridden.

## Troubleshooting

Start with the case's `pipeline.log`. The batch report names the failing step
and quotes the error.

- **LANDFIRE job waits for a long time.** The LFPS service queues jobs from
  all users. The pipeline keeps polling the same job, logging its queue
  position every `LFPS_POLL_HEARTBEAT_S`, and resubmits only if the job fails.
  Don't cancel and resubmit by hand; that sends you to the back of the queue.
- **Open-Meteo errors 429/5xx** are retried with back-off. Missing values near
  the present mean ERA5 has not caught up yet (it lags about 5 days).
- **"HRRR archive has no analysis for N hours":** the public archive has a
  gap. Use `WINDNINJA_MODE = "wxsFile"` for that case.
- **"GDAL GRIB driver missing":** run `conda install -c conda-forge libgdal-grib`.
- **WindNinja: `PROJ: proj_create_from_database: Open of …/share/proj
  failed`**, followed by "Unable to calculate angle departure from north".
  WindNinja can't read its projection database, so wind directions are not
  corrected to grid north. Run `WindNinja_cli` from the pipeline's own
  environment (`WINDNINJA_CONDA_ENV = None`) and check that `PROJ_DATA`
  points at that environment's `share/proj`. Preflight checks this.
- **WindNinja "Failed to connect to ninjastorm.firelab.org"** is its version
  check and is harmless.
- **FARSITE fails:** re-run it by hand to see Wine's messages:
  `cd <case>/farsite && wine64 ~/farsite/bin/TestFARSITE.exe farsite.txt`.
  Input format: [docs/farsite_input_reference.md](docs/farsite_input_reference.md).
- **Setup finds no cases:** the error names the filter that removed
  everything (size, year, name and date match, or location) and the setting
  to relax.
- **"No burnable fuel within 2000 cells of the ignition point":** the
  reported ignition is far outside burnable LANDFIRE fuel. This is usually a
  bad ignition record.

## Repository layout

```
runWildfireAV               entry point: setup / run / pdf
pipelineConfig.py           every setting, with comments
satellite.py                satellite coverage curve (fire start and end times)
case_metadata.py            per-case case_metadata.json
cleanPipelineOutputs.py     delete outputs from a step onwards
parallel_api.py             subprocess, retry and HTTP helpers
setup/                      fire selection and case folders (setup phase)
pipeline/                   one module per per-case step, plus the batch runner and preflight
validation/                 scoring (caseMetrics.py) and report pages
tools/                      monitorBatch.py (live status), checkHourlyInputs.py (time alignment)
docs/                       FARSITE input file reference
environment.yml             conda environment
```

Each step module has `main(case_dir=None)`. Given a case it processes that
case and raises on any problem; given none it loops over all cases. It skips
itself when its output already exists, and writes outputs atomically, so an
interrupted run never leaves a half-written file. The step order is
`plan()` in `pipeline/runPipelineParallel.py`.

## Acknowledgements and license

- The Nelson dead fuel moisture model port is based on WUINITY-PREACT,
  © 2025 Jonathan Wahlqvist.
- The live fuel moisture model follows
  [firelab/NFDRS4](https://github.com/firelab/NFDRS4).
- Data: MTBS, NIFC, NASA FIRMS (LANCE), LANDFIRE, ERA5 (Copernicus) via
  Open-Meteo, NOAA HRRR, OpenStreetMap.

Licensed under the GNU GPL v3; see [LICENSE](LICENSE).
