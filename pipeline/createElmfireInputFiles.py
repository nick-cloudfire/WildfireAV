"""
Step "elmfire_inputs": write the ELMFIRE namelist <case>/<case>.data.

- Ignition point (ignition_point.gpkg) reprojected to the DEM CRS and snapped
  to the nearest burnable FBFM40 cell.
- Simulation window [SatelliteIgnitionTime, EventEndTime] from case_metadata.json.
- NUM_METEOROLOGY_TIMES = band count of inputs/ws.tif.

All tunable ELMFIRE parameters come from pipelineConfig (section 10).
"""

from __future__ import annotations

from pathlib import Path

import fiona
import pandas as pd
import rasterio
from pyproj import Transformer

import pipelineConfig as cfg
from case_metadata import read_case_metadata
from common import atomic_write, case_window, for_each_case, require, skipped, snap_to_valid_fuel

INPUTS = cfg.INPUTS_SUBDIR_NAME
_B = cfg.LANDFIRE_BAND_FILE_NAMES
_stem = lambda name: name.removesuffix(".tif")

# Namelist key -> filename without extension
FUELS_TOPO_FILENAMES = {
    "ASP_FILENAME": _B[2], "CBD_FILENAME": _B[7], "CBH_FILENAME": _B[6], "CC_FILENAME": _B[4],
    "CH_FILENAME": _B[5], "DEM_FILENAME": _B[0], "FBFM_FILENAME": _B[3], "SLP_FILENAME": _B[1],
    "ADJ_FILENAME": _stem(cfg.ADJ_FILE_NAME), "PHI_FILENAME": _stem(cfg.PHI_FILE_NAME),
}
MET_FILENAMES = {
    "WS_FILENAME": _stem(cfg.WS_TIF_NAME), "WD_FILENAME": _stem(cfg.WD_TIF_NAME),
    "M1_FILENAME": cfg.FMC_FILE_NAMES[0], "M10_FILENAME": cfg.FMC_FILE_NAMES[1],
    "M100_FILENAME": cfg.FMC_FILE_NAMES[2],
}


def _ignition_xy(dem_crs, gpkg: Path) -> tuple[float, float]:
    """First feature of the ignition layer, in the DEM CRS."""
    with fiona.open(gpkg) as src:
        feat = next(iter(src), None)
        if feat is None or feat["geometry"] is None or feat["geometry"]["type"] != "Point":
            raise ValueError(f"{gpkg} must contain a Point feature")
        x, y = feat["geometry"]["coordinates"][:2]
        if src.crs:
            x, y = Transformer.from_crs(src.crs, dem_crs, always_xy=True).transform(x, y)
    return float(x), float(y)


def build_namelist(tstop_sec: float, x_ign: float, y_ign: float, current_year: int,
                   hour_of_year: int, num_meteorology_times: int) -> str:
    kv = lambda k, v: f"{k:<30} = {v}"
    return "\n".join([
        "&INPUTS",
        f"FUELS_AND_TOPOGRAPHY_DIRECTORY = './{INPUTS}'",
        *(kv(k, f"'{v}'") for k, v in FUELS_TOPO_FILENAMES.items()),
        kv("DT_METEOROLOGY", f"{cfg.ELMFIRE_DT_METEOROLOGY:.1f}"),
        kv("WEATHER_DIRECTORY", f"'./{INPUTS}'"),
        *(kv(k, f"'{v}'") for k, v in MET_FILENAMES.items()),
        kv("LH_MOISTURE_CONTENT", f"{cfg.LIVE_HERB_MC:.1f}"),
        kv("LW_MOISTURE_CONTENT", f"{cfg.LIVE_WOODY_MC:.1f}"),
        "USE_BARRIERS = .TRUE.",
        "WS_AT_10M = .FALSE.",
        kv("BARRIER_FILENAME", f"'{_stem(cfg.BARRIER_FILE_NAME)}'"),
        "/\n",
        "&OUTPUTS",
        f"OUTPUTS_DIRECTORY    = './{cfg.ELMFIRE_OUTPUTS_SUBDIR}'",
        f"DTDUMP               = {cfg.ELMFIRE_DTDUMP:.1f}",
        "DUMP_TIME_OF_ARRIVAL = .TRUE.",
        "CONVERT_TO_GEOTIFF   = .TRUE.",
        "/\n",
        "&TIME_CONTROL",
        f"SIMULATION_DT    = {cfg.ELMFIRE_SIMULATION_DT:.1f}",
        f"TARGET_CFL       = {cfg.ELMFIRE_TARGET_CFL:.1f}",
        f"SIMULATION_TSTOP = {tstop_sec:.1f}",
        kv("CURRENT_YEAR", f"{current_year:d}"),
        kv("HOUR_OF_YEAR", f"{hour_of_year:d}"),
        "/\n",
        "&SIMULATOR",
        "NUM_IGNITIONS = 1",
        f"X_IGN(1)      = {x_ign:.2f}",
        f"Y_IGN(1)      = {y_ign:.2f}",
        "T_IGN(1)      = 0.00",
        "DEBUG_LEVEL   = 0",
        "CLEAN_SCRATCH = .TRUE.",
        "/\n",
        "&MONTE_CARLO",
        f"NUM_METEOROLOGY_TIMES = {num_meteorology_times:d}",
        "/\n",
        "&MISCELLANEOUS",
        f"PATH_TO_GDAL = '{cfg.ELMFIRE_PATH_TO_GDAL}'",
        f"SCRATCH      = './{cfg.ELMFIRE_SCRATCH_SUBDIR}'",
        "/\n",
    ])


def _process(case_dir: Path):
    case_dir = case_dir.resolve()
    if any(case_dir.glob("*.data")):
        return skipped(".data namelist already exists")
    inputs = case_dir / INPUTS
    dem, fuels, ws = inputs / f"{_B[0]}.tif", inputs / f"{_B[3]}.tif", inputs / cfg.WS_TIF_NAME
    ign = case_dir / cfg.IGNITION_POINT_SHP_NAME
    require(dem, fuels, ign, ws, hint="earlier steps (split_bands / windninja) did not produce these")

    start, end = case_window(read_case_metadata(case_dir), cfg.COL_SATELLITE_IGNITION, cfg.EVENT_END_COL)
    with rasterio.open(dem) as ds:
        dem_crs = ds.crs
    if dem_crs is None:
        raise ValueError(f"{dem} has no CRS")
    with rasterio.open(ws) as ds:
        n_met = ds.count

    x0, y0 = _ignition_xy(dem_crs, ign)
    x, y, moved = snap_to_valid_fuel(fuels, x0, y0)
    lon, lat = Transformer.from_crs(dem_crs, "EPSG:4326", always_xy=True).transform(x, y)
    print(f"  Ignition {x:.2f}, {y:.2f} ({lat:.5f}, {lon:.5f})"
          + (f" — snapped {((x - x0) ** 2 + (y - y0) ** 2) ** 0.5:.0f} m to burnable fuel" if moved else ""))

    hour_of_year = int((start - pd.Timestamp(year=start.year, month=1, day=1)).total_seconds() // 3600)
    text = build_namelist((end - start).total_seconds(), x, y, start.year, hour_of_year, n_met)

    for sub in (cfg.ELMFIRE_SCRATCH_SUBDIR, cfg.ELMFIRE_OUTPUTS_SUBDIR):
        (case_dir / sub).mkdir(exist_ok=True)
    out = case_dir / f"{case_dir.name}.data"
    with atomic_write(out) as tmp:
        tmp.write_text(text, encoding="utf-8")
    print(f"  Wrote {out.name}: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} "
          f"({(end - start).total_seconds() / 3600:.0f} h), {n_met} met times")


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
