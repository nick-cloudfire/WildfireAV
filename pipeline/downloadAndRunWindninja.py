#!/usr/bin/env python3
"""
Step 5 of runPipelineParallel: run WindNinja for each case.

WindNinja requires a weather-model initialisation and cannot span more than
``WINDNINJA_MAX_WINDOW_DAYS`` days in a single run.  Long fires are
automatically split into consecutive chunks, each run in an isolated
subdirectory; outputs are then staged into the shared ``inputs/windninja/``
folder before wn_to_geotiff converts them to multi-band GeoTIFFs.

Outputs per case (inside inputs/windninja/)
-------------------------------------------
- ws.tif   – wind speed  (one band per hour, m/s)
- wd.tif   – wind direction (one band per hour, degrees)
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
# Config (all values come from pipelineConfig)
# ---------------------------------------------------------------------------

FIRE_ROOT        = cfg.FIRE_ROOT
FIRE_CSV         = cfg.FIRE_SUMMARY_SAT_CSV_PATH   # full path under FIRE_ROOT_LOGIN_NODE
INPUTS           = cfg.INPUTS_SUBDIR_NAME
WINDNINJA_SUBDIR = cfg.WINDNINJA_SUBDIR
WXS_NAME         = cfg.WXS_FILE_NAME

FOLDER_COL = cfg.COL_FOLDER
START_COL  = cfg.COL_SATELLITE_IGNITION
END_COL    = cfg.COL_SATELLITE_END

LANDSCAPE_FILENAME = "LANDFIRE.tif"

WX_MODEL_TYPE        = cfg.WINDNINJA_WX_MODEL_TYPE
TIME_ZONE            = cfg.WINDNINJA_TIME_ZONE
MESH_UNITS           = cfg.WINDNINJA_MESH_UNITS
OUTPUT_HEIGHT        = cfg.WINDNINJA_OUTPUT_HEIGHT
OUTPUT_HEIGHT_UNITS  = cfg.WINDNINJA_OUTPUT_HEIGHT_UNITS
MAX_WINDOW_DAYS      = cfg.WINDNINJA_MAX_WINDOW_DAYS
MESH_RES_FACTOR      = cfg.WINDNINJA_MESH_RESOLUTION_FACTOR
CFG_FILENAME         = cfg.WINDNINJA_CFG_FILENAME
CONDA_ENV            = cfg.WINDNINJA_CONDA_ENV
CONDA_EXE            = __import__("os").environ.get("CONDA_EXE", "conda")
THREADS              = cfg.WINDNINJA_NUM_THREADS

# File extensions to copy from each chunk back to the main windninja folder
STAGE_BACK_SUFFIXES = {".asc", ".prj", ".json", ".kml", ".kmz", ".csv", ".txt"}


# ---------------------------------------------------------------------------
# wxs file parser
# ---------------------------------------------------------------------------

def _read_wxs(wxs_path: pathlib.Path) -> pd.DataFrame:
    """
    Parse a RAWS .wxs file into a DataFrame indexed by UTC-naive datetime.

    Expected data header line starts with 'Year' and contains 'WindSpd'.
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
    window = wxs_df.loc[(wxs_df.index >= start) & (wxs_df.index <= stop)].copy()
    if window.empty:
        raise ValueError(f"No wxs records overlap {start} .. {stop}")
    return window


def _split_windows(
    start: dt.datetime,
    stop: dt.datetime,
    max_days: int = MAX_WINDOW_DAYS,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Split [start, stop] into consecutive windows of at most max_days."""
    if stop <= start:
        return []
    out = []
    cur = start
    step = pd.Timedelta(days=max_days)
    while cur < stop:
        nxt = min(cur + step, stop)
        cur_py = cur.to_pydatetime() if hasattr(cur, "to_pydatetime") else cur
        nxt_py = nxt.to_pydatetime() if hasattr(nxt, "to_pydatetime") else nxt
        out.append((cur_py, nxt_py))
        cur = nxt
    return out


# ---------------------------------------------------------------------------
# WindNinja config builder
# ---------------------------------------------------------------------------

def _build_cfg(
    landscape: Path,
    wxs_window: pd.DataFrame,
    output_path: Path,
    cellsize: float,
) -> str:
    start = wxs_window.index[0]
    stop  = wxs_window.index[-1]
    steps = len(wxs_window)
    mesh_resolution = MESH_RES_FACTOR * cellsize

    text = f"""
    num_threads = {THREADS}
    elevation_file = {landscape.resolve()}

    initialization_method = wxModelInitialization
    time_zone = {TIME_ZONE}
    wx_model_type = {WX_MODEL_TYPE}

    number_time_steps = {steps}

    start_year   = {start.year}
    start_month  = {start.month}
    start_day    = {start.day}
    start_hour   = {start.hour}
    start_minute = {start.minute}

    stop_year    = {stop.year}
    stop_month   = {stop.month}
    stop_day     = {stop.day}
    stop_hour    = {stop.hour}
    stop_minute  = {stop.minute}

    output_path = {output_path.resolve()}

    output_wind_height       = {OUTPUT_HEIGHT}
    units_output_wind_height = {OUTPUT_HEIGHT_UNITS}

    diurnal_winds         = true
    non_neutral_stability = true

    mesh_resolution       = {mesh_resolution:.1f}
    units_mesh_resolution = {MESH_UNITS}

    write_ascii_output        = true
    ascii_out_resolution      = {cellsize:.1f}
    units_ascii_out_resolution = m
    ascii_out_aaigrid         = true
    ascii_out_json            = false
    """
    return textwrap.dedent(text).strip() + "\n"


# ---------------------------------------------------------------------------
# Chunk staging helpers
# ---------------------------------------------------------------------------

def _clean_main_workdir(workdir: pathlib.Path) -> None:
    """Remove previously staged ASCII outputs; keep config, logs, chunk dirs."""
    for item in workdir.iterdir():
        if item.is_dir() or item.name == CFG_FILENAME or item.name.endswith(".log"):
            continue
        if item.suffix.lower() in STAGE_BACK_SUFFIXES:
            item.unlink(missing_ok=True)


def _stage_chunk_outputs(chunk_dir: pathlib.Path, main_workdir: pathlib.Path) -> None:
    """Copy chunk outputs to the shared workdir (filenames should be timestamp-unique)."""
    for src in chunk_dir.iterdir():
        if not src.is_file():
            continue
        if src.name == CFG_FILENAME or src.name.endswith(".log"):
            continue
        if src.suffix.lower() not in STAGE_BACK_SUFFIXES:
            continue
        dst = main_workdir / src.name
        if dst.exists():
            raise FileExistsError(
                f"Filename collision while staging chunk outputs: {dst}\n"
                "Chunks may have overlapping time windows."
            )
        shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Single-chunk runner
# ---------------------------------------------------------------------------

def _run_chunk(
    landscape_path: pathlib.Path,
    wxs_df: pd.DataFrame,
    chunk_start: dt.datetime,
    chunk_stop: dt.datetime,
    chunk_idx: int,
    main_workdir: pathlib.Path,
    cellsize: float,
) -> None:
    chunk_dir = main_workdir / f"chunk_{chunk_idx:03d}"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    wxs_window = _wxs_window(wxs_df, chunk_start, chunk_stop)
    cfg_text   = _build_cfg(landscape_path, wxs_window, chunk_dir, cellsize)
    cfg_path   = chunk_dir / CFG_FILENAME
    cfg_path.write_text(cfg_text, encoding="utf-8")

    cmd = [
        CONDA_EXE, "run", "--no-capture-output", "-n", CONDA_ENV,
        "WindNinja_cli", str(cfg_path),
    ]
    print(
        f"    Chunk {chunk_idx:03d}: {chunk_start} -> {chunk_stop} "
        f"({len(wxs_window)} steps)"
    )

    log_path = chunk_dir / "windninja_cli.log"
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write("COMMAND:\n" + " ".join(cmd) + "\n\n")
        proc = subprocess.Popen(
            cmd, cwd=chunk_dir, stdout=logf, stderr=subprocess.STDOUT, text=True
        )
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(
            f"WindNinja chunk {chunk_idx:03d} failed (exit {proc.returncode}). "
            f"See {log_path}"
        )
    _stage_chunk_outputs(chunk_dir, main_workdir)


# ---------------------------------------------------------------------------
# Case-level runner
# ---------------------------------------------------------------------------

def _run_case(case_folder: pathlib.Path) -> None:
    case_folder = pathlib.Path(case_folder).resolve()
    meta = read_case_metadata(case_folder)

    landscape_path = case_folder / LANDSCAPE_FILENAME
    if not landscape_path.exists():
        print(f"  DEM not found at {landscape_path}, skipping.")
        return

    ws_tif = case_folder / INPUTS / WINDNINJA_SUBDIR / cfg.WS_TIF_NAME
    if ws_tif.exists():
        print(f"  Skipped — ws.tif already exists.")
        return

    # Read actual cellsize from the DEM (do NOT use a global)
    with rasterio.open(landscape_path) as src:
        cellsize = src.res[0]

    start_dt = pd.to_datetime(meta[START_COL]) - pd.Timedelta(hours=1)
    end_dt   = pd.to_datetime(meta[END_COL])
    if start_dt.tzinfo is not None:
        start_dt = start_dt.tz_convert("UTC").tz_localize(None)
    if end_dt.tzinfo is not None:
        end_dt = end_dt.tz_convert("UTC").tz_localize(None)

    if end_dt <= start_dt:
        print(f"  Invalid times (stop <= start): {start_dt} / {end_dt}, skipping.")
        return

    workdir = case_folder / INPUTS / WINDNINJA_SUBDIR
    workdir.mkdir(parents=True, exist_ok=True)

    wxs_path = case_folder / INPUTS / WXS_NAME
    if not wxs_path.exists():
        raise FileNotFoundError(f"Missing wxs file: {wxs_path}")
    wxs_df = _read_wxs(wxs_path)

    # Restrict to effective window from wxs, then split into chunks
    case_wxs       = _wxs_window(wxs_df, start_dt.to_pydatetime(), end_dt.to_pydatetime())
    eff_start      = case_wxs.index[0].to_pydatetime()
    eff_stop       = case_wxs.index[-1].to_pydatetime()
    windows        = _split_windows(eff_start, eff_stop, MAX_WINDOW_DAYS)

    if not windows:
        print("  No valid chunk windows; skipping.")
        return

    print(
        f"  Running WindNinja in {len(windows)} chunk(s): "
        f"{eff_start} -> {eff_stop}"
    )
    _clean_main_workdir(workdir)

    for idx, (chunk_start, chunk_stop) in enumerate(windows):
        _run_chunk(
            landscape_path=landscape_path,
            wxs_df=wxs_df,
            chunk_start=chunk_start,
            chunk_stop=chunk_stop,
            chunk_idx=idx,
            main_workdir=workdir,
            cellsize=cellsize,
        )

    print("  All WindNinja chunks finished; converting ASCII outputs to GeoTIFF …")
    wn_to_geotiff.main(workdir, case_folder / INPUTS, landscape_path, clean=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(case_dir=None) -> None:
    if case_dir is not None:
        _run_case(pathlib.Path(case_dir))
        return

    fire_root = pathlib.Path(FIRE_ROOT)
    df = pd.read_csv(FIRE_CSV)
    for col in (FOLDER_COL, START_COL, END_COL):
        if col not in df.columns:
            raise KeyError(f"Column '{col}' not found in {FIRE_CSV}")
    print(f"Loaded {len(df)} records from {FIRE_CSV}\n")

    for idx, row in df.iterrows():
        folder_name = f"{int(row[FOLDER_COL]):05d}"
        case_folder = (fire_root / folder_name).resolve()
        print(f"[{idx}] Running case {folder_name}")
        try:
            _run_case(case_folder)
        except Exception as e:
            print(f"  Case {folder_name} failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
