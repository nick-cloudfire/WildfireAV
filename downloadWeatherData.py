#!/usr/bin/env python3
"""
Step 4 of runPipelineParallel: download ERA5 weather from OpenMeteo.

For each case, fetches hourly ERA5 data for the period
[SatelliteIgnitionTime - CONDITIONING_DAYS, SatelliteEndTime]
and writes:

- inputs/weather.wxs   – RAWS-format text file consumed by WindNinja and Nelson
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer

import pipelineConfig as cfg
from case_metadata import read_case_metadata
from parallel_api import get_thread_session, retry_call

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIRE_ROOT     = cfg.FIRE_ROOT
DEM_NAME      = cfg.LANDFIRE_BAND_FILE_NAMES[0] + ".tif"
INPUTS_FOLDER = cfg.INPUTS_SUBDIR_NAME
OPENMETEO_URL = cfg.OPENMETEO_URL
FOLDER_COL    = cfg.WS_WD_FOLDER_COL
START_COL     = cfg.WS_WD_START_COL
END_COL       = cfg.WS_WD_END_COL
CSV_PATH      = cfg.FIRE_SUMMARY_SAT_CSV_PATH   # full path under FIRE_ROOT_LOGIN_NODE
WXS_NAME      = cfg.WXS_FILE_NAME
DAYS_BEFORE   = cfg.CONDITIONING_DAYS

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WeatherTask:
    folder_name: str
    dem_path: Path
    wxs_path: Path
    inputs_dir: Path
    lat: float
    lon: float
    center_elev_m: float
    start_time: pd.Timestamp   # already offset by -DAYS_BEFORE
    end_time: pd.Timestamp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_lat_lon_elev(dem_path: Path) -> tuple[float, float, float]:
    """Return (lat, lon, elevation_m) from the centre pixel of a DEM."""
    with rasterio.open(dem_path) as ds:
        row = ds.height // 2
        col = ds.width // 2
        elev_val = float(ds.read(1)[row, col])
        x, y = ds.xy(row, col)
        if getattr(ds.crs, "is_geographic", False):
            return float(y), float(x), elev_val
        transformer = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
        lon, lat = transformer.transform(x, y)
        return float(lat), float(lon), elev_val


def _fetch_hourly(task: WeatherTask) -> tuple[pd.DatetimeIndex, dict]:
    params = {
        "latitude":  task.lat,
        "longitude": task.lon,
        "start_date": task.start_time.date().isoformat(),
        "end_date":   task.end_time.date().isoformat(),
        "hourly": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "precipitation",
            "wind_speed_10m",
            "wind_direction_10m",
            "cloud_cover",
        ]),
        "timezone": "UTC",
        "model": cfg.OPENMETEO_MODEL,
    }
    s = get_thread_session()

    def _do():
        r = s.get(OPENMETEO_URL, params=params, timeout=60)
        if not r.ok:
            raise RuntimeError(
                f"[{task.folder_name}] OpenMeteo request failed\n"
                f"  URL: {r.url}\n"
                f"  Status: {r.status_code}\n"
                f"  Body: {r.text[:500]}"
            )
        data = r.json()
        if "hourly" not in data:
            raise RuntimeError(
                f"OpenMeteo response missing 'hourly'. Full response: {data}"
            )
        return data

    data = retry_call(_do, tries=4)
    hourly = data["hourly"]
    times = pd.to_datetime(hourly["time"])
    return times, hourly


def _write_wxs(
    df: pd.DataFrame,
    out_path: Path,
    elevation_m: float,
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
) -> None:
    """Write a RAWS-format .wxs file from an OpenMeteo hourly DataFrame."""
    elev_int = int(round(elevation_m))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"RAWS_ELEVATION: {elev_int}\n")
        f.write("RAWS_UNITS: METRIC\n")
        f.write("RAWS_WINDS: OpenMeteo_ERA5_center_of_DEM\n")
        f.write("Year Mth Day Time Temp RH HrlyPcp WindSpd WindDir CloudCov\n")
        for _, row in df.iterrows():
            t = pd.to_datetime(row["time"]).to_pydatetime()
            if not (start_time < t < end_time):
                continue
            time_int  = t.hour * 100 + t.minute
            temp_int  = int(round(float(row["temperature_2m"])))
            rh_int    = max(0, min(99, int(round(float(row["relative_humidity_2m"])))))
            precip_mm = float(row["precipitation"])
            wspd_int  = int(round(float(row["wind_speed_10m"])))
            wdir_int  = int(round(float(row["wind_direction_10m"]))) % 360
            cloud_int = max(0, min(100, int(round(float(row["cloud_cover"])))))
            f.write(
                f"{t.year:4d} {t.month:2d} {t.day:2d} "
                f"{time_int:04d} "
                f"{temp_int:4d} {rh_int:3d} "
                f"{precip_mm:7.3f} "
                f"{wspd_int:3d} {wdir_int:3d} {cloud_int:3d}\n"
            )


def _task_from_row(row: pd.Series) -> WeatherTask | None:
    folder_name = f"{int(row[FOLDER_COL]):05d}"
    try:
        start = pd.to_datetime(row[START_COL], utc=True).tz_localize(None)
        end   = pd.to_datetime(row[END_COL],   utc=True).tz_localize(None)
    except Exception as e:
        print(f"[{folder_name}] Cannot parse times: {e}")
        return None
    dem_path = FIRE_ROOT / folder_name / INPUTS_FOLDER / DEM_NAME
    if not dem_path.exists():
        print(f"[{folder_name}] DEM not found: {dem_path}, skipping")
        return None
    lat, lon, elev = _derive_lat_lon_elev(dem_path)
    inputs_dir = FIRE_ROOT / folder_name / INPUTS_FOLDER
    return WeatherTask(
        folder_name=folder_name,
        dem_path=dem_path,
        wxs_path=inputs_dir / WXS_NAME,
        inputs_dir=inputs_dir,
        lat=lat,
        lon=lon,
        center_elev_m=elev,
        start_time=start - pd.Timedelta(days=DAYS_BEFORE),
        end_time=end,
    )


def _task_from_case_dir(case_dir: Path) -> WeatherTask | None:
    meta = read_case_metadata(case_dir)
    try:
        start = pd.to_datetime(meta[START_COL], utc=True).tz_localize(None)
        end   = pd.to_datetime(meta[END_COL],   utc=True).tz_localize(None)
    except Exception as e:
        print(f"[{case_dir.name}] Cannot parse times from metadata: {e}")
        return None
    dem_path = case_dir / INPUTS_FOLDER / DEM_NAME
    if not dem_path.exists():
        print(f"[{case_dir.name}] DEM not found: {dem_path}, skipping")
        return None
    lat, lon, elev = _derive_lat_lon_elev(dem_path)
    inputs_dir = case_dir / INPUTS_FOLDER
    return WeatherTask(
        folder_name=case_dir.name,
        dem_path=dem_path,
        wxs_path=inputs_dir / WXS_NAME,
        inputs_dir=inputs_dir,
        lat=lat,
        lon=lon,
        center_elev_m=elev,
        start_time=start - pd.Timedelta(days=DAYS_BEFORE),
        end_time=end,
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker(task: WeatherTask) -> tuple[str, str]:
    try:
        times, hourly = _fetch_hourly(task)
        if len(times) == 0:
            return (task.folder_name, "no data")
        task.inputs_dir.mkdir(parents=True, exist_ok=True)
        _write_wxs(
            pd.DataFrame(hourly),
            task.wxs_path,
            task.center_elev_m,
            task.start_time,
            task.end_time,
        )
        return (task.folder_name, "done")
    except Exception as exc:
        print(f"[{task.folder_name}] FAILED: {type(exc).__name__}: {exc}")
        return (task.folder_name, "failed")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(case_dir=None) -> None:
    if case_dir is not None:
        wxs = Path(case_dir) / INPUTS_FOLDER / WXS_NAME
        if wxs.exists():
            print(f"  Skipped — {WXS_NAME} already exists.")
            return
        task = _task_from_case_dir(Path(case_dir))
        tasks = [] if task is None else [task]
    else:
        df = pd.read_csv(CSV_PATH)
        for col in (FOLDER_COL, START_COL, END_COL):
            if col not in df.columns:
                raise KeyError(f"Missing column '{col}' in {CSV_PATH}")
        tasks = [t for row in (df.iloc[i] for i in range(len(df))) if (t := _task_from_row(row)) is not None]

    print(f"Prepared {len(tasks)} OpenMeteo requests")
    results = [_worker(t) for t in tasks]
    ok = sum(1 for _, status in results if status not in ("failed",))
    print(f"Finished: {ok} ok, {len(results) - ok} failed/skipped")


if __name__ == "__main__":
    main()
