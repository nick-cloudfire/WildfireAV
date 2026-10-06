#!/usr/bin/env python3
"""
Step "weather": hourly ERA5 weather from OpenMeteo -> inputs/weather.wxs.

Fetches [SatelliteIgnitionTime - CONDITIONING_DAYS, SatelliteEndTime] at the
DEM centre and writes a RAWS-format .wxs file (metric) consumed by Nelson,
FARSITE and WindNinja (wxsFile mode).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import rasterio
import requests
from pyproj import Transformer

import pipelineConfig as cfg
from case_metadata import read_case_metadata
from common import atomic_write, case_window, for_each_case, require, skipped
from parallel_api import get_thread_session, retry_call

DEM_NAME = cfg.LANDFIRE_BAND_FILE_NAMES[0] + ".tif"
VARIABLES = ["temperature_2m", "relative_humidity_2m", "precipitation",
             "wind_speed_10m", "wind_direction_10m", "cloud_cover"]


def _dem_centre(dem_path: Path) -> tuple[float, float, float]:
    """Return (lat, lon, elevation_m) of the DEM centre pixel."""
    with rasterio.open(dem_path) as ds:
        row, col = ds.height // 2, ds.width // 2
        elev = float(ds.read(1, window=((row, row + 1), (col, col + 1)))[0, 0])
        x, y = ds.xy(row, col)
        if ds.crs.is_geographic:
            return float(y), float(x), elev
        lon, lat = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform(x, y)
    return float(lat), float(lon), elev


def _fetch_hourly(lat: float, lon: float, start: pd.Timestamp, end: pd.Timestamp,
                  variables: list[str] = VARIABLES) -> pd.DataFrame:
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
        "hourly": ",".join(variables), "timezone": "UTC", "model": cfg.OPENMETEO_MODEL,
    }
    s = get_thread_session()

    def _do():
        r = s.get(cfg.OPENMETEO_URL, params=params, timeout=60)
        if r.status_code in (429, 502, 503, 504):
            raise requests.RequestException(f"transient HTTP {r.status_code} from OpenMeteo")
        if not r.ok:
            raise RuntimeError(f"OpenMeteo HTTP {r.status_code} for {r.url}: {r.text[:300]}")
        data = r.json()
        if "hourly" not in data:
            raise RuntimeError(f"OpenMeteo response has no 'hourly' block: {str(data)[:300]}")
        return data["hourly"]

    # Rate limiting needs long back-off: 5, 10, 20, 40, 60 s
    hourly = retry_call(_do, tries=6, base_sleep_s=5.0, max_sleep_s=60.0, log=print)
    df = pd.DataFrame(hourly)
    df["time"] = pd.to_datetime(df["time"])
    return df


def _write_wxs(df: pd.DataFrame, out_path: Path, elevation_m: float) -> int:
    """Write a RAWS .wxs file; returns the number of records."""
    lines = [
        f"RAWS_ELEVATION: {int(round(elevation_m))}",
        "RAWS_UNITS: METRIC",
        "RAWS_WINDS: OpenMeteo_ERA5_center_of_DEM",
        "Year Mth Day Time Temp RH HrlyPcp WindSpd WindDir CloudCov",
    ]
    for r in df.itertuples(index=False):
        t = r.time
        lines.append(
            f"{t.year:4d} {t.month:2d} {t.day:2d} {t.hour * 100 + t.minute:04d} "
            f"{int(round(r.temperature_2m)):4d} {max(0, min(99, int(round(r.relative_humidity_2m)))):3d} "
            f"{float(r.precipitation):7.3f} "
            f"{int(round(r.wind_speed_10m)):3d} {int(round(r.wind_direction_10m)) % 360:3d} "
            f"{max(0, min(100, int(round(r.cloud_cover)))):3d}"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 4


def _process(case_dir: Path):
    inputs = case_dir / cfg.INPUTS_SUBDIR_NAME
    wxs_path = inputs / cfg.WXS_FILE_NAME
    if wxs_path.exists():
        return skipped(f"{cfg.WXS_FILE_NAME} already exists")
    dem_path = inputs / DEM_NAME
    require(dem_path, hint="run the split_bands step first")

    start, end = case_window(read_case_metadata(case_dir), cfg.WS_WD_START_COL, cfg.WS_WD_END_COL)
    start = start - pd.Timedelta(days=cfg.CONDITIONING_DAYS)
    lat, lon, elev = _dem_centre(dem_path)
    print(f"  OpenMeteo {cfg.OPENMETEO_MODEL} at {lat:.4f}, {lon:.4f} (elev {elev:.0f} m): "
          f"{start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC "
          f"(incl. {cfg.CONDITIONING_DAYS} conditioning days)")

    df = _fetch_hourly(lat, lon, start, end)
    # Snap to whole hours; keep through ceil(end) so downstream windows are complete.
    lo, hi = start.floor("h"), (end + pd.Timedelta(hours=1)).floor("h")
    df = df[(df.time >= lo) & (df.time <= hi)]
    if df.empty:
        raise RuntimeError(f"OpenMeteo returned no hours between {lo} and {hi}")
    gaps = df[VARIABLES].isna().any(axis=1)
    if gaps.any():
        first = df.time[gaps].iloc[0]
        raise RuntimeError(
            f"OpenMeteo returned {int(gaps.sum())} hour(s) with missing values, first at {first} "
            f"(ERA5 lags real time by ~5 days; is the fire too recent?)")

    with atomic_write(wxs_path) as tmp:
        n = _write_wxs(df, tmp, elev)
    print(f"  Wrote {wxs_path.name}: {n} hourly records")


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
