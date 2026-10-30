"""
Scores one case: simulated burn area vs. the observed MTBS perimeter.

For every fire model (MODELS) the time-of-arrival (TOA) raster is turned into
a burn mask (TOA >= BURN_THRESHOLD) and compared, cell by cell, with the
rasterised burn perimeter:

    Jaccard   = TP / (TP + FP + FN)             overlap / union, 0..1
    Sorensen  = 2 TP / (2 TP + FP + FN)         a.k.a. Dice / F1, 0..1
    Kappa     = Cohen's kappa over the raster   agreement beyond chance
    Area ratio = simulated / observed area

(TP = burned in both, FP = simulated only, FN = observed only.)  The area
growth curves of each model and of the satellite detections are kept for the
report; the rasters themselves are not, so memory stays flat with many cases.
"""

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.features import rasterize

import pipelineConfig as cfg
import satellite
from case_metadata import read_case_metadata

log = logging.getLogger("validation")

# rasterize() sometimes raises this spuriously when called from several threads;
# scores are identical to a serial run.
warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)

M2_TO_ACRES     = 0.000247105381
BURN_THRESHOLD  = 1.0      # TOA values >= this count as burned (TOA units, see ModelConfig)
CURVE_BINS      = 600      # time bins of the simulated area growth curves
_UNIT_TO_HOURS  = {"s": 1 / 3600.0, "min": 1 / 60.0, "h": 1.0}


# ---------------------------------------------------------------------------
# Fire models compared in the report — add an entry to compare another one
# ---------------------------------------------------------------------------

@dataclass
class ModelConfig:
    label:    str
    toa_path: str                         # relative to the case folder; may contain a * glob
    toa_unit: str   = "s"                 # unit of the TOA raster values: "s", "min" or "h"
    color:    str   = "#e34948"           # its burn mask, curves and markers in the report

    def find_toa(self, case_dir: Path) -> Path | None:
        """The TOA raster of this model in case_dir (newest match for a glob), or None."""
        hits = sorted(case_dir.glob(self.toa_path), key=lambda p: p.stat().st_mtime)
        return hits[-1] if hits else None

    @property
    def to_hours(self) -> float:
        return _UNIT_TO_HOURS[self.toa_unit]

    @property
    def key(self) -> str:
        return self.label.lower()


MODELS: list[ModelConfig] = [
    ModelConfig("ELMFIRE", "outputs/time_of_arrival_*.tif",            "s",   "#e34948"),
    ModelConfig("FARSITE", "farsite/outputs/farsite_Arrival Time.tif", "min", "#2a78d6"),
]


# ---------------------------------------------------------------------------
# Result of one case
# ---------------------------------------------------------------------------

@dataclass
class Case:
    case_id:      str
    case_dir:     Path
    fire_name:    str | None
    fire_year:    int | None
    model_toas:   dict[str, Path | None]       # label -> TOA raster (None if missing)
    observed:     gpd.GeoSeries                # burn perimeter (EPSG:5070)
    ignition:     gpd.GeoDataFrame             # ignition point (EPSG:5070)
    observed_m2:  float
    duration_h:   float | None                 # simulated duration (ELMFIRE SIMULATION_TSTOP)
    avg_wind_kph: float | None
    metrics:      dict[str, Any]               # "<model>_<metric>" -> value, one CSV row
    # label -> (hours since start, burned area m2): simulated area growth
    model_curves: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    sat_curve:    satellite.CoverageCurve | None = None
    sat_end:      pd.Timestamp | None = None   # SatelliteEndTime used to stop the simulations
    start:        pd.Timestamp | None = None   # SatelliteIgnitionTime = simulation time 0 (UTC)

    def score(self, model: ModelConfig, metric: str) -> float:
        return float(self.metrics.get(f"{model.key}_{metric}", np.nan))


# ---------------------------------------------------------------------------
# Raster comparison
# ---------------------------------------------------------------------------

def read_toa(ds: rasterio.DatasetReader) -> np.ndarray:
    """TOA band as float32 with nodata -> NaN."""
    toa = ds.read(1, masked=True).astype(np.float32).filled(np.nan)
    if ds.nodata is not None:
        toa[np.isclose(toa, ds.nodata)] = np.nan
    return toa


def toa_crs(ds: rasterio.DatasetReader, fallback):
    """CRS of a TOA raster.  FARSITE writes none: it is on the LANDFIRE grid."""
    if ds.crs is None:
        return fallback
    if ds.crs.is_geographic:
        raise ValueError(f"{ds.name}: geographic CRS {ds.crs}; TOA rasters must be projected (metres)")
    return ds.crs


SCORE_COLUMNS = ("observed_m2", "simulated_m2", "simulated_acres", "jaccard", "sorensen", "kappa", "ratio_of_areas")


def similarity(obs: np.ndarray, sim: np.ndarray, cell_m2: float, prefix: str) -> dict[str, float]:
    """Jaccard, Sorensen, kappa and areas of two boolean burn masks on the same grid."""
    tp = float(np.sum(obs & sim))
    fp = float(np.sum(~obs & sim))
    fn = float(np.sum(obs & ~sim))
    tn = float(obs.size) - tp - fp - fn
    n = tp + fp + fn + tn
    agree    = (tp + tn) / n if n else 0.0
    by_luck  = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / n ** 2 if n else 0.0
    obs_m2, sim_m2 = (tp + fn) * cell_m2, (tp + fp) * cell_m2
    return {
        f"{prefix}_observed_m2":    obs_m2,
        f"{prefix}_simulated_m2":   sim_m2,
        f"{prefix}_simulated_acres": sim_m2 * M2_TO_ACRES,
        f"{prefix}_jaccard":        tp / (tp + fp + fn) if tp + fp + fn else 0.0,
        f"{prefix}_sorensen":       2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0.0,
        f"{prefix}_kappa":          (agree - by_luck) / (1 - by_luck) if by_luck < 1 else 0.0,
        f"{prefix}_ratio_of_areas": sim_m2 / obs_m2 if obs_m2 else np.nan,
    }


def growth_curve(toa: np.ndarray, cell_m2: float, to_hours: float) -> tuple[np.ndarray, np.ndarray]:
    """Burned area (m2) against time (h) from a TOA raster, CURVE_BINS points."""
    t = toa[np.isfinite(toa) & (toa >= BURN_THRESHOLD)] * to_hours
    if t.size == 0:
        return np.array([]), np.array([])
    counts, edges = np.histogram(t, bins=CURVE_BINS, range=(float(t.min()), float(t.max()) or 1.0))
    return edges[1:], np.cumsum(counts) * cell_m2


# ---------------------------------------------------------------------------
# Case inputs
# ---------------------------------------------------------------------------

def to_crs_robust(gs, crs):
    """Reproject, falling back via EPSG:4326 when NAD83 datum grids are missing.

    Without the grids some PROJ installs return infinite coordinates for
    EPSG:4269 (NAD83, the MTBS CRS); NAD83 and WGS84 differ by 1-2 m in CONUS.
    """
    out = gs.to_crs(crs)
    if np.isfinite(out.total_bounds).all():
        return out
    log.debug("non-finite reprojection to %s; retrying as EPSG:4326", crs)
    return gs.set_crs("EPSG:4326", allow_override=True).to_crs(crs)


def _read_layer(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    return gdf if gdf.crs else gdf.set_crs("EPSG:4326")


def _simulated_hours(case_dir: Path) -> float | None:
    """SIMULATION_TSTOP of the ELMFIRE namelist, in hours."""
    namelist = case_dir / f"{case_dir.name}.data"
    if not namelist.exists():
        return None
    m = re.search(r"SIMULATION_TSTOP\s*=\s*([0-9.]+)", namelist.read_text(errors="ignore"))
    return float(m.group(1)) / 3600.0 if m else None


def _mean_wind_kph(case_dir: Path, start, end) -> float | None:
    """Mean ERA5 10-m wind speed (km/h) of weather.wxs over the satellite fire window [start, end]."""
    wxs = case_dir / cfg.INPUTS_SUBDIR_NAME / cfg.WXS_FILE_NAME
    if not wxs.exists():
        return None
    lines = wxs.read_text(errors="ignore").splitlines()
    header = next((i for i, ln in enumerate(lines) if ln.startswith("Year") and "WindSpd" in ln), None)
    if header is None:
        return None
    rows = [ln.split() for ln in lines[header + 1:] if len(ln.split()) >= 10]
    df = pd.DataFrame({
        "time": pd.to_datetime([f"{r[0]}-{r[1]}-{r[2]} {r[3].zfill(4)}" for r in rows], format="%Y-%m-%d %H%M"),
        "wind": [float(r[7]) for r in rows],
    })
    if pd.notna(start) and pd.notna(end):
        window = df[(df.time >= start.floor("h")) & (df.time <= (end + pd.Timedelta(hours=1)).floor("h"))]
        df = window if len(window) else df
    return float(df.wind.mean()) if len(df) else None


def _utc_naive(value) -> pd.Timestamp:
    ts = pd.to_datetime(value, errors="coerce")
    return ts.tz_convert("UTC").tz_localize(None) if pd.notna(ts) and ts.tzinfo else ts


def _satellite_curve(case_dir: Path, observed: gpd.GeoSeries, start) -> satellite.CoverageCurve | None:
    path = case_dir / cfg.CASE_SAT_GPKG_NAME
    if not path.exists() or pd.isna(start):
        return None
    pts = gpd.read_file(path, layer="points_in_burn").to_crs(satellite.AREA_CRS)
    pts["sat_datetime"] = satellite.detection_times(pts)
    curve = satellite.coverage_curve(pts.dropna(subset=["sat_datetime"]), observed.iloc[0], start)
    return curve if len(curve.times) else None


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------

def evaluate_case(case_dir: Path) -> Case:
    """Score every model of one case against the observed burn perimeter."""
    meta = read_case_metadata(case_dir)
    perimeter = _read_layer(case_dir / cfg.BURN_SHAPE_NAME)
    if perimeter.empty:
        raise ValueError(f"no geometry in {cfg.BURN_SHAPE_NAME}")
    perimeter = gpd.GeoSeries([perimeter.union_all().buffer(0)], crs=perimeter.crs)
    observed = to_crs_robust(perimeter, satellite.AREA_CRS)
    ign_path = case_dir / cfg.IGNITION_POINT_SHP_NAME
    ignition = _read_layer(ign_path).to_crs(satellite.AREA_CRS) if ign_path.exists() else \
        gpd.GeoDataFrame(geometry=[], crs=satellite.AREA_CRS)

    model_toas = {mc.label: mc.find_toa(case_dir) for mc in MODELS}
    if not any(model_toas.values()):
        raise FileNotFoundError("no model output (time-of-arrival raster) found")

    metrics: dict[str, Any] = {"case": case_dir.name, "observed_m2": float(observed.area.iloc[0])}
    curves = {}
    base_crs = None                                   # FARSITE TOA has no CRS: use ELMFIRE's
    for mc in MODELS:
        if not model_toas[mc.label]:
            continue
        try:
            with rasterio.open(model_toas[mc.label]) as ds:
                crs = toa_crs(ds, base_crs)
                if crs is None:
                    raise ValueError("TOA raster has no CRS and no other model gives one")
                base_crs = base_crs or crs
                toa = read_toa(ds)
                cell_m2 = abs(ds.transform.a * ds.transform.e)
                obs = rasterize([(to_crs_robust(perimeter, crs).iloc[0], 1)], out_shape=toa.shape,
                                transform=ds.transform, dtype=np.uint8).astype(bool)
            sim = np.isfinite(toa) & (toa >= BURN_THRESHOLD)
            metrics.update(similarity(obs, sim, cell_m2, mc.key))
            curves[mc.label] = growth_curve(toa, cell_m2, mc.to_hours)
        except Exception as e:
            log.warning("Case %s: %s not scored: %s", case_dir.name, mc.label, e)
            model_toas[mc.label] = None

    sat_start = _utc_naive(meta.get(cfg.COL_SATELLITE_IGNITION))
    sat_end   = _utc_naive(meta.get(cfg.COL_SATELLITE_END))
    wind = _mean_wind_kph(case_dir, sat_start, sat_end)
    metrics["avg_wind_kph"] = wind
    name = meta.get("perim_name") or meta.get("point_name")
    year = pd.to_datetime(meta.get("perim_ignition"), errors="coerce").year

    return Case(
        case_id      = case_dir.name,
        case_dir     = case_dir,
        fire_name    = str(name) if name else None,
        fire_year    = None if pd.isna(year) else int(year),
        model_toas   = model_toas,
        observed     = observed,
        ignition     = ignition,
        observed_m2  = metrics["observed_m2"],
        duration_h   = _simulated_hours(case_dir),
        avg_wind_kph = wind,
        metrics      = metrics,
        model_curves = curves,
        sat_curve    = _satellite_curve(case_dir, observed, sat_start),
        sat_end      = sat_end if pd.notna(sat_end) else None,
        start        = sat_start if pd.notna(sat_start) else None,
    )
