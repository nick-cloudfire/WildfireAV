# Pipeline improvement TODO

Goal: smaller, faster, more reliable — and equally usable on HPC (SLURM, scratch,
possibly no internet on compute nodes) and laptops (WSL).
Effort: S = hours, M = a day or two.

## Reliability

- [x] **Config overrides don't propagate (S).** `pipelineConfig_local.py` is imported at the
      end, so derived paths (`FIRE_SUMMARY_CSV_PATH`, `MTBS_*`, `ROADS_GPKG`, `NELSON_EXE`, …)
      keep the defaults when `BASE_VALIDATION`/`FIRE_ROOT` are overridden. Import overrides first,
      derive after; then trim `pipelineConfig_local.py` to only the lines that differ. Allow env-var
      overrides (e.g. `WAV_FIRE_ROOT`) for SLURM.
- [x] **No hard-coded `/home/nick` defaults (S).** Derive the repo root from `__file__`; take
      `ELMFIRE_PATH_TO_GDAL` from `$CONDA_PREFIX/bin` / `which gdalinfo`. Also
      `tools/compareOutputs.py:898`, `tools/getWeatherHerbie.py:16-18`.
- [x] **Collapse `FIRE_ROOT` / `FIRE_ROOT_LOGIN_NODE` (S/M).** Summaries go to one, cases to the
      other, `case_metadata.case_dirs()` defaults to LOGIN_NODE — discovery/clean diverge on HPC.
- [x] **Timeouts on external binaries (S).** ELMFIRE, FARSITE (Wine), WindNinja, Nelson, GDAL calls
      have none; a hung `wine64` holds a worker until wall time. Add `*_TIMEOUT_S` config, kill
      wineserver on timeout, optional per-case wall time in `runBatch`.
- [x] **LFPS polling (S).** `_poll_job` has no deadline and a `ConnectionError`/`ReadTimeout`
      escapes and resubmits the job; `MAX_RETRIES` hard-coded; zip not written atomically.
- [x] **Timezone bug (S).** `case_metadata.py:39-58` uses `astimezone()` (machine local zone) →
      laptop and HPC can write different times. Use UTC; remove the duplicated branch.
- [x] **Atomic writes everywhere (S).** `write_case_metadata`, summary CSV, `_safe_to_gpkg`,
      `farsite.input`/`farsite.txt`, WindNinja cfg — half-written files get reused on `--resume`.
- [x] **Live fuel moisture duplicated (S).** `prepareFarsite.py:55-56` `LH_CONST/LW_CONST` vs
      `ELMFIRE_LH_MC/LW_MC` — models can silently diverge. Move FARSITE physics knobs to config too.

## Physics

- [ ] **Live fuel moisture from NFDRS4 GSI (M).** Replace fixed `LIVE_HERB_MC=60` / `LIVE_WOODY_MC=90` with
      a per-case value from the Growing Season Index (Jolly et al. 2005; NFDRS4 `LiveFuelMoisture`), marched
      over ERA5 from 1 Dec of the prior year to ignition. Pure-Python port (no native NFDRS4 binding).
      Prototype on 9 cases: herb 30–245 %, woody 60–197 % (most western summer fires fully cured: 30/60).
      Decide: annual vs perennial herb, woody minimum (60 constructor vs 50 NFDRS init sample).

## HPC / laptop portability

- [ ] **Download stage for offline compute nodes (M).** `--start download` (login node) runs only
      LANDFIRE / OpenMeteo / HRRR for all cases; compute job runs the rest.
- [x] **CPU budget (S).** Derive worker/thread defaults from `sched_getaffinity` /
      `SLURM_CPUS_PER_TASK`; set `OMP_NUM_THREADS`/`GDAL_NUM_THREADS` per case; PDF workers likewise.
- [ ] **Preflight: runtime checks (S).** Only checks executables exist; could check they actually
      start (`ldd`, Nelson's .NET runtime, `wine64` prefix / FARSITE launch).

## Speed

- [ ] **Validation PDF memory (M).** Keeps every case's float64 TOA raster in memory until the end
      — OOM risk with hundreds of cases. Keep area curves only, render case pages as they finish,
      use float32. Re-enable metrics CSV export (`getValidationPDF.py:1564`).
- [ ] **Setup reads national datasets in full (M).** Satellite gpkg, MTBS, NIFC FOD: use pyogrio
      `where=`/`bbox=`/`columns=`. Drop the forced `set_crs(..., allow_override=True)`.
- [ ] **HRRR shared cache (M).** Overlapping fires re-download the same hours.
- [ ] **`snap_to_valid_fuel` (S).** Re-reads a growing window one ring at a time (`common.py:150`);
      double the radius or use a distance transform.

## Succinctness

- [ ] **One satellite-chain/coverage module (M).** Four copies with different params
      (`getSatelliteEndTimes`, `debugSatelliteEndTimes`, `getValidationPDF`, `compareOutputs`).
- [x] **Delete or merge `tools/compareOutputs.py` (S).** 967 lines, duplicates the PDF, stale paths.
      Review `debugBandCounts.py`, `getWeatherHerbie.py` too.
- [ ] **Split `getValidationPDF.py` (M).** 1571 lines → metrics / case pages / summary pages;
      replace silent `except Exception` blocks with logged warnings.
- [x] **Deduplicate helpers (S).** Two log tees, two retry helpers, two clean routines,
      re-implemented `fmt_duration` in `prefetchLandfire.py`.
- [x] **Prune config (S).** Unused keys (`LANDFIRE_ZIP_NAME`, `OSM_WIDTH_FIELD`, `USE_OSM_WIDTH_TAG`,
      `WS_WD_FOLDER_COL`, `PERIMETER_DATA_ROOT`, `SATELLITES_ROOT`); consider dropping
      `WINDNINJA_MODE="wxModel"`.
- [ ] **Package it (M).** `pip install -e .` instead of `sys.path` hacks in every entry point.

## Docs / hygiene

- [ ] **README is stale (S).** Says FARSITE is native / Wine not required (`README.md:102,493`);
      code uses `wine64 TestFARSITE.exe`. Report filename, section numbers, Nelson clone dir
      (`nelson_csharp` vs repo name `Nelson-Dead-Fuel-Moisture`).
- [ ] **Repo hygiene (S).** Ignore `elmfire/`, `*.zip`; remove stale `__pycache__`.
