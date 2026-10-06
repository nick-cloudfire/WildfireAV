#!/usr/bin/env python3
"""
Step "windninja": hourly wind fields for one case -> inputs/ws.tif, inputs/wd.tif.

``WINDNINJA_MODE`` selects how WindNinja is initialised:

hrrrLocal  HRRR analysis hours are downloaded locally by ``downloadHrrr`` (only
           the 4 needed bands, cropped to the DEM), packed into a
           PASTCAST-GCP-HRRR-CONUS-3-KM zip per chunk and passed to WindNinja
           via ``forecast_filename``.  Default; scales to many cases.
wxModel    WindNinja downloads ``WINDNINJA_WX_MODEL_TYPE`` (PASTCAST-GCP-HRRR)
           itself inside every run.  Slow and fragile at scale.
wxsFile    One ``domainAverageInitialization`` run per hour, using the wind,
           temperature and cloud cover from ``inputs/weather.wxs``.

The hourly window comes from weather.wxs so the band count lines up with the
Nelson fuel-moisture output.  wxModel/hrrrLocal runs cover the window in
chunks of at most ``WINDNINJA_MAX_WINDOW_DAYS`` days (WindNinja refuses longer
runs); each chunk / hour runs in its own subdirectory and its ASCII grids are
staged into ``inputs/windninja/`` before ``wn_to_geotiff`` stacks them.  The
whole workspace is deleted once ws.tif / wd.tif exist.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import textwrap
import time
from pathlib import Path

import pandas as pd
import rasterio

import pipelineConfig as cfg
import wn_to_geotiff
from case_metadata import read_case_metadata
from common import case_window, fmt_duration, for_each_case, progress, require, skipped
from parallel_api import hours, run_subprocess

INPUTS        = cfg.INPUTS_SUBDIR_NAME
MODE          = cfg.WINDNINJA_MODE
CFG_FILENAME  = cfg.WINDNINJA_CFG_FILENAME
LANDSCAPE     = "LANDFIRE.tif"
CLI_LOG       = "windninja_cli.log"
STAGE_SUFFIXES = {".asc", ".prj", ".json", ".kml", ".kmz", ".csv", ".txt"}
MODES = ("hrrrLocal", "wxModel", "wxsFile")


# ---------------------------------------------------------------------------
# weather.wxs -> hourly window
# ---------------------------------------------------------------------------

def read_wxs(path: Path) -> pd.DataFrame:
    """Parse a RAWS .wxs file into a DataFrame indexed by naive-UTC hour."""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    header = next((i for i, ln in enumerate(lines)
                   if ln.strip().startswith("Year") and "WindSpd" in ln), None)
    if header is None:
        raise ValueError(f"no 'Year ... WindSpd' header line in {path}")
    rows = []
    for ln in lines[header + 1:]:
        p = re.split(r"\s+", ln.strip())
        if len(p) < 10:
            continue
        hhmm = p[3].zfill(4)
        ts = pd.Timestamp(int(p[0]), int(p[1]), int(p[2]), int(hhmm[:2]), int(hhmm[2:]))
        rows.append((ts, *map(float, p[4:10])))
    df = pd.DataFrame(rows, columns=["dt", "temp_C", "rh", "pcp", "wind_kph", "wind_dir_deg", "cloud_pct"])
    if df.empty:
        raise ValueError(f"no data rows in {path}")
    return df.set_index("dt").sort_index()


def hourly_window(case_dir: Path, wxs: pd.DataFrame) -> pd.DataFrame:
    """WXS rows WindNinja must cover for this case (same bounds as before the refactor).

    wxsFile: [floor(start), floor(end)].  wxModel/hrrrLocal: one extra hour each
    side, [floor(start - 1h), floor(end + 1h)].
    """
    start, end = case_window(read_case_metadata(case_dir), cfg.COL_SATELLITE_IGNITION, cfg.COL_SATELLITE_END)
    if MODE != "wxsFile":
        start, end = start - pd.Timedelta(hours=1), end + pd.Timedelta(hours=1)
    lo, hi = start.floor("h"), end.floor("h")
    win = wxs.loc[(wxs.index >= lo) & (wxs.index <= hi)]
    if win.empty:
        raise ValueError(f"weather.wxs has no records between {lo} and {hi}")
    return win


def split_chunks(hours: pd.DatetimeIndex, max_days: int) -> list[pd.DatetimeIndex]:
    """Split consecutive hours into balanced, non-overlapping chunks of <= max_days."""
    max_len = max_days * 24
    n = math.ceil(len(hours) / max_len)
    size = math.ceil(len(hours) / n)
    return [hours[i:i + size] for i in range(0, len(hours), size)]


# ---------------------------------------------------------------------------
# WindNinja config + CLI
# ---------------------------------------------------------------------------

def _common_cfg(landscape: Path, out_dir: Path, cellsize: float) -> str:
    return f"""
        num_threads                = {cfg.WINDNINJA_NUM_THREADS}
        elevation_file             = {landscape.resolve()}
        time_zone                  = {cfg.WINDNINJA_TIME_ZONE}
        output_path                = {out_dir.resolve()}
        output_speed_units         = mph
        output_wind_height         = {cfg.WINDNINJA_OUTPUT_HEIGHT}
        units_output_wind_height   = {cfg.WINDNINJA_OUTPUT_HEIGHT_UNITS}
        diurnal_winds              = true
        non_neutral_stability      = true
        mesh_resolution            = {cfg.WINDNINJA_MESH_RESOLUTION_FACTOR * cellsize:.1f}
        units_mesh_resolution      = {cfg.WINDNINJA_MESH_UNITS}
        write_ascii_output         = true
        ascii_out_resolution       = {cellsize:.1f}
        units_ascii_out_resolution = m
        ascii_out_aaigrid          = true
        ascii_out_json             = false
    """


def _pastcast_cfg(hours: pd.DatetimeIndex) -> str:
    """WindNinja downloads PASTCAST-GCP-HRRR itself for [first, last] hour."""
    a, b = hours[0], hours[-1]
    return f"""
        initialization_method      = wxModelInitialization
        wx_model_type              = {cfg.WINDNINJA_WX_MODEL_TYPE}
        number_time_steps          = {len(hours)}
        start_year   = {a.year}
        start_month  = {a.month}
        start_day    = {a.day}
        start_hour   = {a.hour}
        start_minute = {a.minute}
        stop_year    = {b.year}
        stop_month   = {b.month}
        stop_day     = {b.day}
        stop_hour    = {b.hour}
        stop_minute  = {b.minute}
    """


def _local_hrrr_cfg(zip_path: Path) -> str:
    """Pre-downloaded pastcast zip; WindNinja runs every hour inside it."""
    return f"""
        initialization_method      = wxModelInitialization
        forecast_filename          = {zip_path.resolve()}
    """


def _hour_cfg(ts: pd.Timestamp, row: pd.Series) -> str:
    return f"""
        initialization_method      = domainAverageInitialization
        input_speed                = {row.wind_kph:.2f}
        input_speed_units          = kph
        input_direction            = {row.wind_dir_deg:.1f}
        input_wind_height          = 10
        units_input_wind_height    = m
        uni_air_temp               = {row.temp_C:.1f}
        air_temp_units             = C
        uni_cloud_cover            = {row.cloud_pct:.1f}
        cloud_cover_units          = percent
        year                       = {ts.year}
        month                      = {ts.month}
        day                        = {ts.day}
        hour                       = {ts.hour}
        minute                     = {ts.minute}
        write_farsite_atm          = true
    """


def _cli_cmd(cfg_path: Path) -> list[str]:
    """WindNinja_cli from PATH, or via ``conda run -n WINDNINJA_CONDA_ENV`` if that is set."""
    if cfg.WINDNINJA_CONDA_ENV:
        conda = os.environ.get("CONDA_EXE", "conda")
        return [conda, "run", "--no-capture-output", "-n", cfg.WINDNINJA_CONDA_ENV, "WindNinja_cli", str(cfg_path)]
    if not shutil.which("WindNinja_cli"):
        raise FileNotFoundError("WindNinja_cli is not on PATH (activate its conda env, "
                                "or set WINDNINJA_CONDA_ENV in pipelineConfig)")
    return ["WindNinja_cli", str(cfg_path)]


def _run_cli(run_dir: Path, cfg_text: str, label: str, main_dir: Path) -> None:
    """Write the config, run WindNinja in run_dir, stage its grids into main_dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = run_dir / CFG_FILENAME
    cfg_path.write_text(textwrap.dedent(cfg_text).strip() + "\n", encoding="utf-8")
    cmd = _cli_cmd(cfg_path)
    log_path = run_dir / CLI_LOG
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write("COMMAND:\n" + " ".join(cmd) + "\n\n")
        logf.flush()
        rc = run_subprocess(cmd, check=False, log=logf, cwd=run_dir,
                            timeout=hours(cfg.WINDNINJA_TIMEOUT_H)).returncode
    if rc != 0:
        tail = log_path.read_text(errors="ignore").strip().splitlines()[-15:]
        print("    --- tail of " + str(log_path) + " ---\n    " + "\n    ".join(tail))
        raise RuntimeError(f"WindNinja {label} failed (exit {rc}); see {log_path}")

    for src in run_dir.iterdir():
        if src.is_file() and src.suffix.lower() in STAGE_SUFFIXES and src.name != CFG_FILENAME:
            dst = main_dir / src.name
            if dst.exists():
                raise FileExistsError(f"two WindNinja runs produced {src.name} (overlapping time windows?)")
            shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------

def _process(case_dir: Path):
    if MODE not in MODES:
        raise ValueError(f"WINDNINJA_MODE must be one of {MODES}, got {MODE!r}")
    inputs = case_dir / INPUTS
    if (inputs / cfg.WS_TIF_NAME).exists():
        return skipped(f"{cfg.WS_TIF_NAME} already exists")

    landscape, wxs_path = case_dir / LANDSCAPE, inputs / cfg.WXS_FILE_NAME
    require(landscape, hint="run the landfire step first")
    require(wxs_path, hint="run the weather step first")
    with rasterio.open(landscape) as src:
        cellsize = src.res[0]

    window = hourly_window(case_dir, read_wxs(wxs_path))
    hours = window.index
    workdir = inputs / cfg.WINDNINJA_SUBDIR
    # Leftovers from an interrupted run: drop partial WindNinja output but keep
    # downloaded HRRR hours (the downloader skips hours it already has).
    workdir.mkdir(parents=True, exist_ok=True)
    for item in workdir.iterdir():
        if item.name != cfg.HRRR_LOCAL_SUBDIR:
            shutil.rmtree(item) if item.is_dir() else item.unlink()

    print(f"  Mode {MODE}: {len(hours)} hourly fields {hours[0]} -> {hours[-1]} UTC, "
          f"mesh {cfg.WINDNINJA_MESH_RESOLUTION_FACTOR * cellsize:.0f} m, output {cellsize:.0f} m")
    t0 = time.monotonic()

    if MODE == "wxsFile":
        for i, (ts, row) in enumerate(window.iterrows()):
            progress(f"WindNinja hour {i + 1}/{len(hours)}  {ts:%Y-%m-%d %H:%M}  "
                     f"ws={row.wind_kph:.0f} kph wd={row.wind_dir_deg:.0f}")
            d = workdir / f"step_{i:03d}"
            _run_cli(d, _common_cfg(landscape, d, cellsize) + _hour_cfg(ts, row),
                     f"hour {i + 1}/{len(hours)}", workdir)
    else:
        if MODE == "hrrrLocal":
            import downloadHrrr
            hrrr_dir = workdir / cfg.HRRR_LOCAL_SUBDIR
            downloadHrrr.download_hrrr(landscape, hours[0], hours[-1], hrrr_dir)

        chunks = split_chunks(hours, cfg.WINDNINJA_MAX_WINDOW_DAYS)
        for i, chunk in enumerate(chunks):
            progress(f"WindNinja chunk {i + 1}/{len(chunks)}: {chunk[0]} -> {chunk[-1]} ({len(chunk)} h)")
            t_chunk = time.monotonic()
            d = workdir / f"chunk_{i:03d}"
            if MODE == "hrrrLocal":
                z = downloadHrrr.pack_zip(hrrr_dir, chunk, hrrr_dir / f"{downloadHrrr.ZIP_PREFIX}_chunk{i:03d}.zip")
                init = _local_hrrr_cfg(z)
            else:
                init = _pastcast_cfg(chunk)
            _run_cli(d, _common_cfg(landscape, d, cellsize) + init, f"chunk {i + 1}/{len(chunks)}", workdir)
            print(f"    chunk {i + 1} done in {fmt_duration(time.monotonic() - t_chunk)}")

    print(f"  WindNinja finished in {fmt_duration(time.monotonic() - t0)}; stacking to GeoTIFF")
    wn_to_geotiff.main(workdir, inputs, landscape, clean=True)


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
