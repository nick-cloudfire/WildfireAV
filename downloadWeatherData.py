#!/usr/bin/env python
from pathlib import Path
import math
import requests
import numpy as np
import pandas as pd
import rasterio
import fiona
import pipelineConfig as cfg
from dataclasses import dataclass
from pathlib import Path
import pandas as pd
from pyproj import Transformer

from parallel_api import run_parallel, make_logger, get_thread_session, retry_call


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

FIRE_ROOT       = cfg.FIRE_ROOT
DEM_NAME        = cfg.LANDFIRE_BAND_FILE_NAMES[0] + ".tif"
INPUTS_FOLDER   = cfg.INPUTS_SUBDIR_NAME
OPENMETEO_URL   = cfg.OPENMETEO_URL
FOLDER_COL      = cfg.WS_WD_FOLDER_COL
START_COL       = cfg.WS_WD_START_COL
END_COL         = cfg.WS_WD_END_COL
csv_path        = cfg.FIRE_SUMMARY_WITH_SATELLITE_CSV
WS_TIF_NAME     = cfg.WS_TIF_NAME
WD_TIF_NAME     = cfg.WD_TIF_NAME
MAX_WORKERS     = 2
DAYS_BEFORE     = cfg.CONDITIONING_DAYS
WXS_NAME        = cfg.WXS_FILE_NAME
# ----------------------------------------------------------------------
# Parallel processing
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class WeatherTask:
    folder_name: str
    dem_path: Path
    wxs_path: Path
    inputs_dir: Path
    ws_out: Path
    wd_out: Path
    lat: float
    lon: float
    center_dem: float
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    
def derive_lat_lon_from_dem_center(dem_path: Path):
    dem_path = Path(dem_path)
    if not dem_path.exists():
        raise FileNotFoundError(f"DEM not found: {dem_path}")

    with rasterio.open(dem_path) as ds:
        crs = ds.crs
        height, width = ds.height, ds.width
        row = height // 2
        col = width // 2
        band1 = ds.read(1)
        elev_val = float(band1[row, col])
        x, y = ds.xy(row, col)
        crs_str = crs.to_string().lower() if crs is not None else ""
        if getattr(ds.crs, "is_geographic", False):
            lon, lat = float(x), float(y)
            return lat, lon, elev_val
        transformer = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
        lon, lat = transformer.transform(x, y)
        return float(lat), float(lon), elev_val
        print("  DEM CRS is projected or unknown")

def fetch_hourly_data(task: WeatherTask):
    start_date = task.start_time.date().isoformat()
    end_date = task.end_time.date().isoformat()
    params = {
        "latitude": task.lat,
        "longitude": task.lon,
        "start_date": start_date,
        "end_date": end_date,
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
            # Log EVERYTHING useful
            raise RuntimeError(
                f"[{task.folder_name}] Open-Meteo request failed\n"
                f"URL: {r.url}\n"
                f"Status: {r.status_code}\n"
                f"Response: {r.text[:500]}"
                )
    
        data = r.json()
    
        if "hourly" not in data:
            raise RuntimeError(
                f"Open-Meteo response missing 'hourly'\n"
                f"Response JSON: {data}"
            )
    
        return data

    data = retry_call(_do, tries=4)

    hourly = data["hourly"]
    times = pd.to_datetime(hourly["time"])
    return times, hourly

def write_hourly_raster(template_path: Path,
                        out_path: Path,
                        values: np.ndarray,
                        times: pd.DatetimeIndex | None = None):

    values = np.asarray(values, dtype=float)
    if values.size == 0:
        raise RuntimeError(f"No values to write for raster {out_path}")

    with rasterio.open(template_path) as src:
        profile = src.profile.copy()
        height = src.height
        width = src.width

    count = int(values.size)

    profile.update(
        driver="GTiff",
        count=count,
        dtype="float32",
        nodata=None,
        compress="lzw",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Writing {count} bands to {out_path}")

    with rasterio.open(out_path, "w", **profile) as dst:
        for i, val in enumerate(values, start=1):
            data = np.full((height, width), float(val), dtype=np.float32)
            dst.write(data, i)

            # Optional: set band description with timestamp
            if times is not None and len(times) >= i:
                ts = pd.to_datetime(times[i - 1])
                ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")  # UTC ISO-like
                try:
                    dst.set_band_description(i, ts_str)
                except Exception as exc:
                    # Non-fatal; just log
                    print(f"    Warning: could not set band description for band {i}: {exc}")

def write_wxs(df: pd.DataFrame, out_path: Path, elevation_m: float, start_time: pd.Timestamp, end_time: pd.Timestamp):
    elev_int = int(round(elevation_m))

    with open(out_path, "w", encoding="utf-8") as f:
        # Header lines
        f.write(f"RAWS_ELEVATION: {elev_int}\n")
        f.write("RAWS_UNITS: METRIC\n")
        f.write("RAWS_WINDS: OpenMeteo_ERA5_center_of_DEM\n")
        f.write("Year Mth Day Time Temp RH HrlyPcp WindSpd WindDir CloudCov\n")
        for _, row in df.iterrows():
            t = pd.to_datetime(row["time"]).to_pydatetime()
            if end_time > t > start_time:
                time_int = t.hour * 100 + t.minute
    
                temp_int = int(round(float(row["temperature_2m"])))
                rh_int = max(0, min(99, int(round(float(row["relative_humidity_2m"])))))
                precip_mm = float(row["precipitation"])
                wspd_kmh_int = int(round(float(row["wind_speed_10m"]) * 3.6))
                wdir_int = int(round(float(row["wind_direction_10m"]))) % 360
                cloud_int = max(0, min(100, int(round(float(row["cloud_cover"])))))
    
                f.write(
                    f"{t.year:4d} {t.month:2d} {t.day:2d} "
                    f"{time_int:04d} "
                    f"{temp_int:4d} {rh_int:3d} "
                    f"{precip_mm:7.3f} "
                    f"{wspd_kmh_int:3d} {wdir_int:3d} {cloud_int:3d}\n"
                )
        
def _parse_fire_times(row: pd.Series):
    start = pd.to_datetime(row[START_COL], utc=True).tz_localize(None)
    end = pd.to_datetime(row[END_COL], utc=True).tz_localize(None)
    return start, end

def build_wind_tasks(df: pd.DataFrame) -> list[WeatherTask]:
    tasks = []
    for _, row in df.iterrows():
        folder_name = f"{int(row[FOLDER_COL]):05d}"
        start_time, end_time = _parse_fire_times(row)
        dem_path = FIRE_ROOT / folder_name / INPUTS_FOLDER / DEM_NAME
        wxs_path = FIRE_ROOT / folder_name / INPUTS_FOLDER / WXS_NAME
        if not dem_path.exists():
            print(f"[{folder_name}] DEM not found in {dem_path}, skipping")
            continue

        lat, lon, elev = derive_lat_lon_from_dem_center(dem_path)

        inputs_dir = FIRE_ROOT / folder_name / INPUTS_FOLDER
        ws_out = inputs_dir / WS_TIF_NAME
        wd_out = inputs_dir / WD_TIF_NAME

        tasks.append(
            WeatherTask(
                folder_name=folder_name,
                dem_path=dem_path,
                wxs_path = wxs_path,
                inputs_dir=inputs_dir,
                ws_out=ws_out,
                wd_out=wd_out,
                lat=lat,
                lon=lon,
                center_dem=elev,
                start_time=start_time - pd.Timedelta(days=DAYS_BEFORE),
                end_time=end_time,
            )
        )

    return tasks

def main():
    df = pd.read_csv(csv_path)
    for col in (FOLDER_COL, START_COL, END_COL):
        if col not in df.columns:
            raise KeyError(f"Missing column {col}")
    tasks = build_wind_tasks(df)
    print(f"Prepared {len(tasks)} Open-Meteo requests")
    log = make_logger("OPENMETEO")

    def worker(task: WeatherTask):
        try:
            times, hourly = fetch_hourly_data(task)
    
            if len(times) == 0:
                print(f"[{task.folder_name}] No hourly data returned")
                return (task.folder_name, "no data")
    
            ws = np.asarray(hourly["wind_speed_10m"], dtype=float)
            wd = np.asarray(hourly["wind_direction_10m"], dtype=float)
    
            task.inputs_dir.mkdir(parents=True, exist_ok=True)
    
            write_hourly_raster(
                task.dem_path,
                task.ws_out,
                ws[DAYS_BEFORE*24 + task.start_time.hour : len(ws)-24+task.end_time.hour],
                times,
            )
    
            write_hourly_raster(
                task.dem_path,
                task.wd_out,
                wd[DAYS_BEFORE*24 + task.start_time.hour : len(wd)-24+task.end_time.hour],
                times,
            )
    
            write_wxs(
                pd.DataFrame(hourly),
                task.wxs_path,
                task.center_dem,
                task.start_time,
                task.end_time,
            )
    
            return (task.folder_name, "done")
    
        except Exception as exc:
            print(f"[{task.folder_name}] FAILED: {type(exc).__name__}: {exc}")
            return (task.folder_name, "failed")


    # for task in tasks:
    #     print("Running task:", task.folder_name)
    #     worker(task)   # let it raise; you'll see the full traceback
    
    outcomes = run_parallel(tasks, worker, max_workers=MAX_WORKERS, log=log)
    ok = sum(o.ok for o in outcomes)
    print(f"Finished: {ok} ok, {len(outcomes) - ok} failed/skipped")

if __name__ == "__main__":
    main()
