#!/usr/bin/env python3
"""
Step "live_moisture": live herbaceous / woody fuel moisture from the Growing
Season Index -> inputs/live_fuel_moisture.json.

The GSI (Jolly, Nemani & Running 2005, Global Change Biology 11:619-632) is
the product of three 0-1 ramps: minimum temperature, vapour pressure deficit
and daylength.  NFDRS4 averages it over 21 days and maps it linearly onto a
live moisture range once it passes the greenup threshold.  This is a port of
firelab/NFDRS4 ``LiveFuelMoisture`` (livefuelmoisture.cpp), including the
annual-herb curing rule, driven by ERA5 (OpenMeteo) at the DEM centre from
1 December of the year before ignition, so the herb state starts each year
the way NFDRS4's does.

One value per case (at the simulation start) feeds both ELMFIRE and FARSITE.
The model takes latitude and weather only - no terrain or fuel type - so a
per-cell raster would add nothing.  Not ported: NFDRS4's snow-day override
and its optional precipitation index (off by default in NFDRS4 too).

    python liveFuelMoisture.py /path/to/case    # one case
"""

from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path

import pandas as pd

import pipelineConfig as cfg
from case_metadata import read_case_metadata
from common import atomic_write, case_window, for_each_case, require, skipped
from downloadWeatherData import DEM_NAME, _dem_centre, _fetch_hourly

OUT_NAME = "live_fuel_moisture.json"


# ---------------------------------------------------------------------------
# GSI model (NFDRS4 LiveFuelMoisture)
# ---------------------------------------------------------------------------

def _ramp(x: float, lo: float, hi: float) -> float:
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def daylength_s(lat: float, doy: int) -> float:
    """Daylength (s) from MT-CLIM, as in NFDRS4 CalcDayl."""
    lat = max(-1.5707, min(1.5707, math.radians(lat)))
    decl = -0.4092797 * math.cos((doy + 10.25) * 0.017214)
    coshss = -(math.sin(lat) * math.sin(decl)) / (math.cos(lat) * math.cos(decl))
    return 2.0 * math.acos(max(-1.0, min(1.0, coshss))) * 13750.9871


def _vp_pa(t_c: float) -> float:
    return 610.7 * math.exp(17.38 * t_c / (239 + t_c))


def daily_gsi(tmin_c: float, tmax_c: float, min_rh: float, lat: float, doy: int) -> float:
    """iTmin * iVPD * iDaylength; VPD at Tmax with the day's minimum RH (floored at 5 %)."""
    vpd = max(0.0, _vp_pa(tmax_c) * (1 - max(min_rh, 5.0) / 100))
    return (_ramp(tmin_c, *cfg.GSI_TMIN_C)
            * (1 - _ramp(vpd, *cfg.GSI_VPD_PA))
            * _ramp(daylength_s(lat, doy), *cfg.GSI_DAYLENGTH_S))


def _scale(gsi_avg: float, lo: float, hi: float) -> float:
    """Linear from lo at the greenup threshold to hi at GSI_MAX; lo below greenup."""
    g = cfg.GSI_GREENUP
    r = min(1.0, max(0.0, gsi_avg / cfg.GSI_MAX))
    if r < g:
        return lo
    slope = (hi - lo) / (1.0 - g)
    return slope * r + hi - slope


def march(daily: pd.DataFrame, lat: float) -> pd.DataFrame:
    """Run the model over consecutive days (index: date; columns tmin, tmax, min_rh in °C / %).

    Returns columns gsi (21-day mean), herb, woody (% dry weight).  The herb
    state resets each 1 January, as in NFDRS4.  For annual herbs, once the herb
    moisture has exceeded 120 % and falls back below it, it can only decrease
    (curing) for the rest of that year.
    """
    herb_lo, herb_hi = cfg.GSI_HERB_MC_RANGE
    q: deque[float] = deque(maxlen=cfg.GSI_AVERAGING_DAYS)
    rows, prev_day = [], None
    for day, r in daily.iterrows():
        if prev_day is None or day.year != prev_day.year:
            greened = can_rise = over_120 = False
            last_herb = None
        if prev_day is not None and (day - prev_day).days > 1:      # gap: drop stale values
            for _ in range(min((day - prev_day).days - 1, len(q))):
                q.popleft()
        prev_day = day

        q.append(daily_gsi(r.tmin, r.tmax, r.min_rh, lat, day.dayofyear))
        gsi = sum(q) / len(q)

        herb = _scale(gsi, herb_lo, herb_hi)
        if gsi / cfg.GSI_MAX >= cfg.GSI_GREENUP and not greened:
            greened = can_rise = True
        if not can_rise and last_herb is not None:
            herb = min(herb, last_herb)
        over_120 |= herb >= 120
        if over_120 and herb < 120 and cfg.GSI_HERB_ANNUAL:
            can_rise = False
        last_herb = herb

        rows.append((day, gsi, herb, _scale(gsi, *cfg.GSI_WOODY_MC_RANGE)))
    return pd.DataFrame(rows, columns=["date", "gsi", "herb", "woody"]).set_index("date")


def to_daily(hourly: pd.DataFrame, lon: float) -> pd.DataFrame:
    """Hourly UTC temperature/RH -> NFDRS4 daily values: the 24 h ending at the
    observation hour (GSI_OBS_HOUR, local solar time), labelled by that day."""
    local = hourly["time"] + pd.to_timedelta(lon / 15.0, unit="h")
    label = (local + pd.Timedelta(hours=23 - cfg.GSI_OBS_HOUR)).dt.floor("D")
    g = hourly.groupby(label.values)
    daily = pd.DataFrame({"tmin": g["temperature_2m"].min(),
                          "tmax": g["temperature_2m"].max(),
                          "min_rh": g["relative_humidity_2m"].min()}).dropna()
    daily.index = pd.DatetimeIndex(daily.index)
    return daily


# ---------------------------------------------------------------------------
# Case step
# ---------------------------------------------------------------------------

def live_moisture(case_dir: Path) -> tuple[float, float]:
    """(herbaceous, woody) live moisture in % for this case, per LIVE_FUEL_MOISTURE_SOURCE."""
    if cfg.LIVE_FUEL_MOISTURE_SOURCE == "constant":
        return float(cfg.LIVE_HERB_MC), float(cfg.LIVE_WOODY_MC)
    path = Path(case_dir) / cfg.INPUTS_SUBDIR_NAME / OUT_NAME
    require(path, hint="created by the live_moisture step")
    d = json.loads(path.read_text(encoding="utf-8"))
    return float(d["herb_mc"]), float(d["woody_mc"])


def _process(case_dir: Path):
    if cfg.LIVE_FUEL_MOISTURE_SOURCE == "constant":
        return skipped(f"LIVE_FUEL_MOISTURE_SOURCE = 'constant' "
                       f"({cfg.LIVE_HERB_MC:g} / {cfg.LIVE_WOODY_MC:g} %)")
    if cfg.LIVE_FUEL_MOISTURE_SOURCE != "gsi":
        raise ValueError(f"LIVE_FUEL_MOISTURE_SOURCE must be 'gsi' or 'constant', "
                         f"not {cfg.LIVE_FUEL_MOISTURE_SOURCE!r}")
    inputs = case_dir / cfg.INPUTS_SUBDIR_NAME
    out = inputs / OUT_NAME
    if out.exists():
        return skipped(f"{OUT_NAME} already exists")
    dem_path = inputs / DEM_NAME
    require(dem_path, hint="run the split_bands step first")

    start, _ = case_window(read_case_metadata(case_dir), cfg.WS_WD_START_COL, cfg.WS_WD_END_COL)
    lat, lon, _ = _dem_centre(dem_path)
    spinup = pd.Timestamp(year=start.year - 1, month=12, day=1)
    print(f"  GSI from ERA5 at {lat:.4f}, {lon:.4f}: {spinup:%Y-%m-%d} -> {start:%Y-%m-%d %H:%M} UTC")

    hourly = _fetch_hourly(lat, lon, spinup, start, ["temperature_2m", "relative_humidity_2m"])
    hourly = hourly.dropna()
    series = march(to_daily(hourly, lon), lat)

    # The value in force at simulation start: the last daily update (obs hour, local) before it.
    start_local = start + pd.Timedelta(hours=lon / 15.0)
    updated = series.index + pd.Timedelta(hours=cfg.GSI_OBS_HOUR) <= start_local
    if not updated.any():
        raise RuntimeError("no GSI day before the simulation start (is the fire too recent for ERA5?)")
    day = series[updated].iloc[-1]
    result = {
        "herb_mc": round(float(day.herb), 1),
        "woody_mc": round(float(day.woody), 1),
        "gsi": round(float(day.gsi), 3),
        "date": f"{day.name:%Y-%m-%d}",
        "lat": round(lat, 4), "lon": round(lon, 4),
        "herb_annual": cfg.GSI_HERB_ANNUAL,
        "season_max_herb_mc": round(float(series.loc[str(start.year)].herb.max()), 1),
        "source": "NFDRS4 GSI (Jolly et al. 2005) on ERA5 via OpenMeteo",
    }
    with atomic_write(out) as tmp:
        tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"  GSI {result['gsi']:.2f} on {result['date']} -> herbaceous {result['herb_mc']:.0f} %, "
          f"woody {result['woody_mc']:.0f} %")


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else None)
