# FARSITE Linux Binary — Input File Reference

This document describes the input switches recognized by the compiled Linux
FARSITE binary (`TestFARSITE`) built from `/home/nick/farsite/src/`.

> **Important — WindNinja integration is NOT compiled in.**
> `Far_WN.cpp` is excluded from the Makefile `SOURCES` list, so
> `GRIDDED_WINDS_GENERATE: Yes` is parsed but silently ignored.
> Use `FARSITE_ATM_FILE` (see below) to supply pre-generated wind grids.

---

## Command line

```
TestFARSITE <command-file>
```

The command file contains a single line:

```
landscape.lcp  farsite.input  ignition.shp  barrier.shp|0  outputs/farsite  <output-type>
```

| Field | Description |
|---|---|
| `landscape.lcp` | FARSITE landscape file (LCP format) |
| `farsite.input` | FARSITE input parameters file (ICF) |
| `ignition.shp` | Ignition perimeter or point shapefile |
| `barrier.shp` or `0` | Barrier shapefile, or `0` for none |
| `outputs/farsite` | Output base path (no extension) |
| `<output-type>` | `1` = ASCII grids (`.asc`), `2` = GeoTIFF (`.tif`) |

FARSITE must be run with the case `farsite/` directory as the working directory
(all relative paths in `farsite.input` and the ATM file are resolved from CWD).

---

## farsite.input (ICF) switches

All fields use the format `KEY: value` or `KEY: value1 value2 ...`.  
The file must begin with `FARSITE INPUTS FILE VERSION 1.0`.

### Simulation timing

| Switch | Format | Description |
|---|---|---|
| `FARSITE_START_TIME` | `MM DD HHMM` | Simulation start (month, day, 24-hr time) |
| `FARSITE_END_TIME` | `MM DD HHMM` | Simulation end |
| `FARSITE_TIMESTEP` | minutes | Internal fire-spread calculation interval (default 60) |

### Spatial resolution

| Switch | Format | Description |
|---|---|---|
| `FARSITE_DISTANCE_RES` | metres | Maximum fire spread distance per timestep (default 60.0) |
| `FARSITE_PERIMETER_RES` | metres | Minimum vertex spacing on fire perimeter (default 60.0) |
| `FARSITE_MIN_IGNITION_VERTEX_DISTANCE` | metres | Minimum spacing when expanding ignition (default 15.0) |

### Spotting

| Switch | Format | Description |
|---|---|---|
| `FARSITE_SPOT_PROBABILITY` | 0.0–1.0 | Probability a firebrand starts a new spot fire (0 = disabled) |
| `FARSITE_SPOT_GRID_RESOLUTION` | metres | Grid cell size for spot fire accumulation (default 30.0) |
| `FARSITE_SPOT_IGNITION_DELAY` | minutes | Delay before a spot fire ignites (default 0) |
| `FARSITE_MINIMUM_SPOT_DISTANCE` | metres | Minimum distance from parent fire for a spot to ignite |

### Acceleration

| Switch | Value | Description |
|---|---|---|
| `FARSITE_ACCELERATION_ON` | `0` or `1` | Enable fire-spread acceleration model (Rothermel) |

### Burn periods

```
FARSITE_BURN_PERIODS: <N>
MM DD HHMM HHMM
...
```

One line per day.  Each line specifies the active burn period for that date
(`start_HHMM end_HHMM`).  Use `0000 2359` for a full 24-hour burn day.

### Fuel moisture data

```
FUEL_MOISTURES_DATA: <N>
<FuelModel> <1hr%> <10hr%> <100hr%> <LiveHerb%> <LiveWoody%>
...
```

One line per fuel model.  The first line should be for model 0 (fallback default).

### Foliar moisture and crown fire

| Switch | Format | Description |
|---|---|---|
| `FOLIAR_MOISTURE_CONTENT` | integer % | Foliar (canopy foliage) moisture content |
| `CROWN_FIRE_METHOD` | `Finney` or `ScottReinhardt` | Crown fire spread model |

### Weather (RAWS data embedded in the input file)

```
RAWS_ELEVATION: <elevation_m>
RAWS_UNITS: Metric | English
RAWS: <N>
YYYY MM DD HHMM temp rh precip wind_speed wind_dir cloud_cover
...
```

Weather units:  
- **Metric**: temperature °C, precipitation mm, wind speed kph  
- **English**: temperature °F, precipitation inches, wind speed mph

### Gridded wind grids (ATM file)

```
FARSITE_ATM_FILE: winds/winds.atm
```

Path relative to FARSITE's working directory.  The ATM file format is:

```
ENGLISH
<month> <day> <HHMM> <speed.asc> <dir.asc>
...
```

- First line: `ENGLISH` (mph) or `METRIC` (kph at 10 m → converted to 20 ft internally)
- One entry per timestep; timesteps need not be uniform
- `HHMM` is the integer time (e.g., `100` = 01:00, `1400` = 14:00)
- Paths are relative to FARSITE's CWD (not to the ATM file location, on Linux)
- **Coverage**: each grid must cover the full LCP extent
  (`west ≤ LCP_west`, `east ≥ LCP_east`, `south ≤ LCP_south`, `north ≥ LCP_north`)
- **Timing**: the first entry's timestamp must equal or precede `FARSITE_START_TIME`
  (sim-time = 0 requirement in `CWindGrids::CheckTimes`)
- Wind speed values **must be ≥ 0** — the reader aborts on any negative value

#### ASCII grid format (`.asc` files)

ESRI ASCII raster with **exactly 6 header lines** using lowercase keywords:

```
ncols        <N>
nrows        <N>
xllcorner    <float>
yllcorner    <float>
cellsize     <float>
NODATA_value -9999
```

Followed immediately by space-separated float data rows (row 0 = north edge).

### Switches parsed but not functional in this build

| Switch | Status |
|---|---|
| `GRIDDED_WINDS_GENERATE: Yes/No` | Parsed; no WindNinja library → ignored |
| `GRIDDED_WINDS_RESOLUTION: <m>` | Parsed but unused |
| `GRIDDED_WINDS_HEIGHT: <ft>` | Parsed but unused |

---

## Output files

With output type `1` (ASCII), FARSITE writes the following `.asc` files to the
specified output base path:

| File | Description |
|---|---|
| `farsite_ArrivalTime.asc` | Arrival time (minutes from simulation start) |
| `farsite_FirelineIntensity.asc` | Fireline intensity (kW/m) |
| `farsite_FlameLength.asc` | Flame length (m) |
| `farsite_SpreadRate.asc` | Rate of spread (m/min) |
| `farsite_HeatPerUnitArea.asc` | Heat per unit area (kJ/m²) |
| `farsite_ReactiveIntensity.asc` | Reactive intensity (kW/m²) |
| `farsite_CrownFireActivity.asc` | Crown fire flag (0/1/2) |

The pipeline keeps only `farsite_ArrivalTime.asc` and deletes the rest.
