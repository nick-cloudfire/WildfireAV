#!/usr/bin/env python3
"""
Step 5 of runPipelineParallel: run WindNinja using local WXS weather data.

Unlike downloadAndRunWindninja_wxModel.py (which downloads a gridded weather
model), this script uses the hourly RAWS data already present in
``inputs/weather.wxs``.  It runs WindNinja once per hour between the fire
start and end times using ``domainAverageInitialization``, which is much
faster because no data download is required.

For each hourly record in the WXS window:
  - Wind speed and direction are taken directly from the WXS file.
  - Air temperature and cloud cover are passed to WindNinja so diurnal
    and atmospheric-stability corrections can be applied.
  - Each run executes in an isolated ``step_NNN/`` subdirectory; its ASCII
    outputs are staged into the shared ``inputs/windninja/`` folder.
  - ``wn_to_geotiff`` then converts all staged ASC files to ws.tif / wd.tif.

Outputs per case (inside inputs/windninja/)
-------------------------------------------
- ws.tif  – wind speed  (one band per hour, mph)
- wd.tif  – wind direction (one band per hour, degrees)
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pandas as pd
import rasterio

import pipelineConfig as cfg
import wn_to_geotiff
from case_metadata import read_case_metadata

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIRE_ROOT        = cfg.FIRE_ROOT
INPUTS           = cfg.INPUTS_SUBDIR_NAME
WINDNINJA_SUBDIR = cfg.WINDNINJA_SUBDIR
WXS_NAME         = cfg.WXS_FILE_NAME

START_COL = cfg.COL_SATELLITE_IGNITION
END_COL   = cfg.COL_SATELLITE_END

LANDSCAPE_FILENAME = "LANDFIRE.tif"

TIME_ZONE           = cfg.WINDNINJA_TIME_ZONE
MESH_UNITS          = cfg.WINDNINJA_MESH_UNITS
OUTPUT_HEIGHT       = cfg.WINDNINJA_OUTPUT_HEIGHT
OUTPUT_HEIGHT_UNITS = cfg.WINDNINJA_OUTPUT_HEIGHT_UNITS
MESH_RES_FACTOR     = cfg.WINDNINJA_MESH_RESOLUTION_FACTOR
CFG_FILENAME        = cfg.WINDNINJA_CFG_FILENAME
CONDA_ENV           = cfg.WINDNINJA_CONDA_ENV
CONDA_EXE           = __import__("os").environ.get("CONDA_EXE", "conda")
THREADS             = cfg.WINDNINJA_NUM_THREADS

STAGE_BACK_SUFFIXES = {".asc", ".prj", ".json", ".kml", ".kmz", ".csv", ".txt"}


# ---------------------------------------------------------------------------
# WXS parser (hourly RAWS data)
# ---------------------------------------------------------------------------

def _read_wxs(wxs_path: Path) -> pd.DataFrame:
    """
    Parse a RAWS .wxs file into a DataFrame indexed by UTC-naive datetime.

    Expected data header: starts with 'Year' and contains 'WindSpd'.
    Metric units assumed (kph, °C, % cloud).
    """
    lines = wxs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    header_idx = next(
        (i for i, ln in enumerate(lines)
         if ln.strip().startswith("Year") and "WindSpd" in ln),
        None,
    )
    if header_idx is None:
        raise ValueError(f"Could not find data header in {wxs_path}")

    rows = []
    for ln in lines[header_idx + 1:]:
        parts = re.split(r"\s+", ln.strip())
        if len(parts) < 10:
            continue
        year, mth, day = int(parts[0]), int(parts[1]), int(parts[2])
        hhmm = parts[3].zfill(4)
        ts = dt.datetime(year, mth, day, int(hhmm[:2]), int(hhmm[2:]))
        rows.append((ts, float(parts[4]), float(parts[5]), float(parts[6]),
                     float(parts[7]), float(parts[8]), float(parts[9])))

    df = pd.DataFrame(
        rows,
        columns=["dt", "temp_C", "rh", "pcp", "wind_kph", "wind_dir_deg", "cloud_pct"],
    ).set_index("dt").sort_index()

    if df.empty:
        raise ValueError(f"No data rows parsed from {wxs_path}")
    return df


def _wxs_window(wxs_df: pd.DataFrame,
                start: dt.datetime,
                stop: dt.datetime) -> pd.DataFrame:
    """Return WXS rows that fall within [start, stop], snapped to hour boundaries.

    Both boundaries are floored to the hour.  The WXS file is always written
    with records through ceil(end_time) = floor(end_time) + 1 h, so flooring
    here drops that one extra record and makes the window length (= ws/wd band
    count) equal to the number of bands the Nelson model produces from the same
    WXS data (Nelson outputs N-1 bands from N post-conditioning records).
    """
    start_hour = start.replace(minute=0, second=0, microsecond=0)
    stop_hour  = stop.replace(minute=0, second=0, microsecond=0)
    window = wxs_df.loc[(wxs_df.index >= start_hour) & (wxs_df.index <= stop_hour)].copy()
    if window.empty:
        raise ValueError(f"No WXS records overlap {start} … {stop}")
    return window


# ---------------------------------------------------------------------------
# WindNinja config builder (domainAverageInitialization)
# ---------------------------------------------------------------------------

def _build_cfg(
    landscape: Path,
    ts: dt.datetime,
    wind_kph: float,
    wind_dir: float,
    temp_c: float,
    cloud_pct: float,
    output_path: Path,
    cellsize: float,
) -> str:
    mesh_resolution = MESH_RES_FACTOR * cellsize
    return textwrap.dedent(f"""
        num_threads                = {THREADS}
        elevation_file             = {landscape.resolve()}

        initialization_method      = domainAverageInitialization
        time_zone                  = {TIME_ZONE}

        input_speed                = {wind_kph:.2f}
        input_speed_units          = kph
        input_direction            = {wind_dir:.1f}
        input_wind_height          = 10
        units_input_wind_height    = m
        
        uni_air_temp               = {temp_c:.1f}
        air_temp_units             = C
        uni_cloud_cover            = {cloud_pct:.1f}
        cloud_cover_units          = percent

        year                       = {ts.year}
        month                      = {ts.month}
        day                        = {ts.day}
        hour                       = {ts.hour}
        minute                     = {ts.minute}

        output_path                = {output_path.resolve()}

        output_speed_units         = mph
        output_wind_height         = {OUTPUT_HEIGHT}
        units_output_wind_height   = {OUTPUT_HEIGHT_UNITS}

        diurnal_winds              = true
        non_neutral_stability      = true

        mesh_resolution            = {mesh_resolution:.1f}
        units_mesh_resolution      = {MESH_UNITS}

        write_ascii_output         = true
        ascii_out_resolution       = {cellsize:.1f}
        units_ascii_out_resolution = m
        ascii_out_aaigrid          = true
        ascii_out_json             = false
        write_farsite_atm          = true
    """).strip() + "\n"


# ---------------------------------------------------------------------------
# Staging helpers
# ---------------------------------------------------------------------------

def _clean_main_workdir(workdir: Path) -> None:
    """Remove previously staged ASCII outputs; keep config, logs, step dirs."""
    for item in workdir.iterdir():
        if item.is_dir() or item.name == CFG_FILENAME or item.name.endswith(".log"):
            continue
        if item.suffix.lower() in STAGE_BACK_SUFFIXES:
            item.unlink(missing_ok=True)


def _stage_step_outputs(step_dir: Path, main_workdir: Path) -> None:
    """Copy step ASCII outputs to the shared workdir."""
    for src in step_dir.iterdir():
        if not src.is_file():
            continue
        if src.name == CFG_FILENAME or src.name.endswith(".log"):
            continue
        if src.suffix.lower() not in STAGE_BACK_SUFFIXES:
            continue
        dst = main_workdir / src.name
        if dst.exists():
            raise FileExistsError(
                f"Filename collision staging step outputs: {dst}\n"
                "Two steps produced a file with the same name."
            )
        shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Single-step runner
# ---------------------------------------------------------------------------

def _run_step(
    landscape_path: Path,
    ts: dt.datetime,
    wind_kph: float,
    wind_dir: float,
    temp_c: float,
    cloud_pct: float,
    step_idx: int,
    n_steps: int,
    main_workdir: Path,
    cellsize: float,
) -> None:
    step_dir = main_workdir / f"step_{step_idx:03d}"
    step_dir.mkdir(parents=True, exist_ok=True)

    cfg_text = _build_cfg(
        landscape_path, ts, wind_kph, wind_dir, temp_c, cloud_pct, step_dir, cellsize
    )
    cfg_path = step_dir / CFG_FILENAME
    cfg_path.write_text(cfg_text, encoding="utf-8")

    print(
        f"    Step {step_idx + 1:3d}/{n_steps}: {ts.strftime('%Y-%m-%d %H:%M')}  "
        f"ws={wind_kph:.1f} kph  wd={wind_dir:.0f}°  T={temp_c:.1f}°C  cld={cloud_pct:.0f}%"
    )

    cmd = [
        CONDA_EXE, "run", "--no-capture-output", "-n", CONDA_ENV,
        "WindNinja_cli", str(cfg_path),
    ]
    log_path = step_dir / "windninja_cli.log"
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write("COMMAND:\n" + " ".join(cmd) + "\n\n")
        proc = subprocess.Popen(
            cmd, cwd=step_dir, stdout=logf, stderr=subprocess.STDOUT, text=True
        )
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(
            f"WindNinja step {step_idx:03d} failed (exit {proc.returncode}). "
            f"See {log_path}"
        )
    _stage_step_outputs(step_dir, main_workdir)


# ---------------------------------------------------------------------------
# Case-level runner
# ---------------------------------------------------------------------------

def _run_case(case_folder: Path) -> None:
    case_folder = Path(case_folder).resolve()
    meta = read_case_metadata(case_folder)

    landscape_path = case_folder / LANDSCAPE_FILENAME
    if not landscape_path.exists():
        print(f"  DEM not found at {landscape_path}, skipping.")
        return

    ws_tif = case_folder / INPUTS / cfg.WS_TIF_NAME
    if ws_tif.exists():
        print("  Skipped — ws.tif already exists.")
        return

    wxs_path = case_folder / INPUTS / WXS_NAME
    if not wxs_path.exists():
        raise FileNotFoundError(f"Missing WXS file: {wxs_path}")

    with rasterio.open(landscape_path) as src:
        cellsize = src.res[0]

    start_dt = pd.to_datetime(meta[START_COL])
    end_dt   = pd.to_datetime(meta[END_COL])
    for t in (start_dt, end_dt):
        if t.tzinfo is not None:
            t = t.tz_convert("UTC").tz_localize(None)
    if start_dt.tzinfo is not None:
        start_dt = start_dt.tz_convert("UTC").tz_localize(None)
    if end_dt.tzinfo is not None:
        end_dt = end_dt.tz_convert("UTC").tz_localize(None)

    if end_dt <= start_dt:
        print(f"  Invalid times (stop ≤ start): {start_dt} / {end_dt}, skipping.")
        return

    wxs_df  = _read_wxs(wxs_path)
    window  = _wxs_window(wxs_df, start_dt.to_pydatetime(), (end_dt).to_pydatetime())
    n_steps = len(window)

    workdir = case_folder / INPUTS / WINDNINJA_SUBDIR
    workdir.mkdir(parents=True, exist_ok=True)

    print(
        f"  Running WindNinja ({n_steps} hourly steps): "
        f"{window.index[0]} → {window.index[-1]}"
    )
    _clean_main_workdir(workdir)

    for idx, (ts, row) in enumerate(window.iterrows()):
        _run_step(
            landscape_path = landscape_path,
            ts             = ts.to_pydatetime(),
            wind_kph       = float(row["wind_kph"]),
            wind_dir       = float(row["wind_dir_deg"]),
            temp_c         = float(row["temp_C"]),
            cloud_pct      = float(row["cloud_pct"]),
            step_idx       = idx,
            n_steps        = n_steps,
            main_workdir   = workdir,
            cellsize       = cellsize,
        )

    print("  All steps finished; converting ASCII outputs to GeoTIFF …")
    wn_to_geotiff.main(workdir, case_folder / INPUTS, landscape_path, clean=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(case_dir=None) -> None:
    if case_dir is not None:
        _run_case(Path(case_dir))
        return

    fire_root = Path(FIRE_ROOT)
    for case_folder in sorted(fire_root.iterdir()):
        if not case_folder.is_dir() or not case_folder.name.isdigit():
            continue
        print(f"[{case_folder.name}] Running WXS WindNinja")
        try:
            _run_case(case_folder)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
