# -*- coding: utf-8 -*-
"""
Batch wildfire validation report generator (ONE multi-page PDF) — FAST VERSION (no polygonization).

One PDF, one page per case folder:
- Observed burn scar outline (firescar.gpkg)
- Predicted burned area shown as raster mask overlay from newest time_of_arrival_*.tif (NO polygonization)
- Ignition points (optional)
- Simulation duration (SIMULATION_TSTOP) + stats (optional)
- Similarity metrics + area stats (computed in raster-space, optional toggle)
- Optional: satellite cumulative hotspot coverage curve (preprojected + spatial index)

Requirements:
    geopandas, rasterio, shapely, pyproj, numpy, matplotlib, pandas
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import logging
import re
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import rasterize
from rasterio.plot import plotting_extent
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import matplotlib as mpl
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# -------------------------
# Logging / matplotlib defaults
# -------------------------
log = logging.getLogger(__name__)

mpl.rcParams["pdf.fonttype"] = 42
mpl.rcParams["ps.fonttype"] = 42
mpl.rcParams["font.family"] = "DejaVu Sans Mono"


# -------------------------
# Constants / heuristics
# -------------------------
M2_TO_ACRES = 0.000247105381  # exact

CSV_LON_COLS = ["lon", "longitude", "x", "X", "LONGITUDE", "LON"]
CSV_LAT_COLS = ["lat", "latitude", "y", "Y", "LATITUDE", "LAT"]
CSV_EPSG_COLS = ["epsg", "EPSG", "crs_epsg", "CRS_EPSG"]

DEFAULT_IGNITION_CANDIDATES = ["ignition_point.gpkg", "ignition_points.gpkg", "ignitions.csv"]
DEFAULT_STATS_CANDIDATES = [
    "outputs/simulation_stats.csv",
    "outputs/stats.csv",
    "simulation_stats.csv",
    "stats.csv",
    "outputs/run_summary.csv",
    "run_summary.csv",
    "outputs/run.log",
    "outputs/elmfire.log",
    "run.log",
    "elmfire.log",
]
DEFAULT_DATAFILE_CANDIDATES = ["*.data"]


# -------------------------
# Config
# -------------------------
@dataclass(frozen=True)
class ReportConfig:
    root_dir: Path
    output_pdf: Path

    case_ids: list[str] | None = None

    raster_pattern: str = "time_of_arrival_*.tif"
    raster_band: int = 1
    burn_threshold: float = 1.0

    # Plot controls (mask only, not polygonization)
    mask_alpha: float = 0.35

    # Raster-based metrics toggles
    compute_raster_metrics: bool = True
    all_touched_observed: bool = False  # rasterize observed polygon

    ignition_candidates: list[str] = field(default_factory=lambda: list(DEFAULT_IGNITION_CANDIDATES))
    stats_candidates: list[str] = field(default_factory=lambda: list(DEFAULT_STATS_CANDIDATES))
    datafile_candidates: list[str] = field(default_factory=lambda: list(DEFAULT_DATAFILE_CANDIDATES))
    # Satellite/model curve rendering controls (PDF size + speed)
    curve_max_points: int = 300          # decimate long time series
    curve_linewidth: float = 1.2
    curve_show_markers: bool = False     # keep False for small PDFs

    # Model area curve
    toa_time_unit: str = "s"             # TOA values assumed in seconds
    model_curve_bins: int = 600          # histogram bins for model curve (trade speed vs smoothness)

@dataclass(frozen=True)
class SatelliteConfig:
    enabled: bool

    gpkg: Path | None = None
    layer: str | None = None
    date_col: str | None = None
    time_col: str | None = None

    master_csv: Path | None = None
    folder_col: str | None = None
    discovery_col: str | None = None

    area_crs: str = "EPSG:5070"
    max_gap: pd.Timedelta = pd.Timedelta(days=2)
    coverage_fraction: float = 0.85
    hotspot_buffer_dist: float = 500.0


# -------------------------
# Case results
# -------------------------
@dataclass
class CaseResult:
    case_id: str
    raster_used: Path

    raster_crs: Any
    raster_transform: Any
    raster_shape: tuple[int, int]  # (H, W)
    pixel_area_m2: float

    obs_geom_raster_crs: Any  # shapely geometry (observed) in raster CRS
    ign_raster_crs: gpd.GeoDataFrame

    stats: dict[str, Any]
    tstop_hours: float | None
    tstop_source: str | None

    # Metrics storage (easy to add to)
    metrics: dict[str, Any] = field(default_factory=dict)

    # Satellite
    sat_times: list[pd.Timestamp] = field(default_factory=list)
    sat_areas: list[float] = field(default_factory=list)
    sat_end_time: pd.Timestamp | pd.NaT = pd.NaT
    sat_target_area: float | None = None
    
    discovery_dt: pd.Timestamp | None = None


# -------------------------
# Small helpers
# -------------------------
def safe_divide(num: float, denom: float) -> float:
    return 0.0 if denom == 0 else num / denom


def first_existing(case_dir: Path, relpaths: Iterable[str]) -> Path | None:
    """Return first existing file (supports globs)."""
    for rel in relpaths:
        if any(ch in rel for ch in ["*", "?", "["]):
            matches = sorted(case_dir.glob(rel))
            if matches:
                return matches[0]
        else:
            p = case_dir / rel
            if p.exists():
                return p
    return None

def model_area_curve_from_toa(
    toa: np.ndarray,
    burn_threshold: float,
    pixel_area_m2: float,
    *,
    bins: int = 600,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Returns (toa_time_values, cumulative_area_m2) in TOA units.
    Uses histogram to avoid sorting millions of pixels.
    """
    toa = np.asarray(toa)
    valid = np.isfinite(toa) & (toa >= burn_threshold)
    vals = toa[valid].astype(np.float64)
    if vals.size == 0:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)

    vmin = float(vals.min())
    vmax = float(vals.max())
    if vmax <= vmin:
        # single-valued -> step
        t = np.array([vmin, vmax], dtype=np.float64)
        a = np.array([vals.size * pixel_area_m2, vals.size * pixel_area_m2], dtype=np.float64)
        return t, a

    counts, edges = np.histogram(vals, bins=bins, range=(vmin, vmax))
    cum_counts = np.cumsum(counts)
    times = edges[1:]  # right edges correspond to cumulative threshold
    areas = cum_counts * float(pixel_area_m2)
    return times, areas

def decimate_xy(x: np.ndarray, y: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(x)
    if max_points is None or max_points <= 0 or n <= max_points:
        return x, y
    idx = np.linspace(0, n - 1, max_points).round().astype(int)
    idx = np.unique(idx)
    return x[idx], y[idx]


def newest_raster(outputs_dir: Path, pattern: str) -> Path:
    rasters = sorted(outputs_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not rasters:
        raise FileNotFoundError(f"No rasters matching '{pattern}' in {outputs_dir}")
    return rasters[-1]


def read_polygon_union(path: Path) -> gpd.GeoSeries:
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"No geometries in {path}")
    merged = gdf.geometry.union_all()
    return gpd.GeoSeries([merged], crs=gdf.crs)


def read_ignitions(case_dir: Path, cfg: ReportConfig) -> gpd.GeoDataFrame:
    p = first_existing(case_dir, cfg.ignition_candidates)
    if p is None:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    suf = p.suffix.lower()
    if suf in [".shp", ".geojson", ".gpkg"]:
        gdf = gpd.read_file(p)
        if gdf.empty:
            return gpd.GeoDataFrame(geometry=[], crs=gdf.crs or "EPSG:4326")
        return gdf

    if suf == ".csv":
        df = pd.read_csv(p)
        lon_col = next((c for c in CSV_LON_COLS if c in df.columns), None)
        lat_col = next((c for c in CSV_LAT_COLS if c in df.columns), None)
        if lon_col is None or lat_col is None or df.empty:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        crs: Any = "EPSG:4326"
        epsg_col = next((c for c in CSV_EPSG_COLS if c in df.columns), None)
        if epsg_col is not None:
            try:
                crs = f"EPSG:{int(df[epsg_col].iloc[0])}"
            except Exception:
                crs = "EPSG:4326"

        geom = gpd.points_from_xy(df[lon_col], df[lat_col])
        return gpd.GeoDataFrame(df, geometry=geom, crs=crs)

    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


def read_sim_stats(case_dir: Path, cfg: ReportConfig) -> dict[str, Any]:
    p = first_existing(case_dir, cfg.stats_candidates)
    if p is None:
        return {}

    out: dict[str, Any] = {"_stats_source": str(p)}
    if p.suffix.lower() == ".csv":
        try:
            df = pd.read_csv(p)
            if not df.empty:
                out.update(df.iloc[-1].to_dict())
        except Exception:
            pass
        return out

    try:
        text = p.read_text(errors="ignore")
    except Exception:
        return out

    kv_pairs = re.findall(r"([A-Za-z0-9_\-]+)\s*=\s*([0-9]+(?:\.[0-9]+)?)", text)
    for k, v in kv_pairs[:120]:
        if k in out:
            continue
        try:
            out[k] = float(v) if "." in v else int(v)
        except Exception:
            out[k] = v

    m = re.search(r"Runtime\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*s", text, flags=re.IGNORECASE)
    if m and "Runtime_s" not in out:
        out["Runtime_s"] = float(m.group(1))

    return out


def parse_tstop_hours(case_dir: Path, cfg: ReportConfig) -> tuple[float | None, str | None]:
    p = first_existing(case_dir, cfg.datafile_candidates)
    if p is None or not p.exists():
        return None, None

    try:
        txt = p.read_text(errors="ignore")
    except Exception:
        return None, str(p)

    m = re.search(r"SIMULATION_TSTOP\s*=\s*([0-9]+(?:\.[0-9]+)?)", txt)
    if not m:
        return None, str(p)

    seconds = float(m.group(1))
    return seconds / 3600.0, str(p)


# -------------------------
# Raster-space masks + metrics
# -------------------------
def sim_mask_from_toa(src: rasterio.io.DatasetReader, band: int, threshold: float) -> np.ndarray:
    """Boolean burned mask from TOA raster."""
    toa = src.read(band, masked=True)
    return np.asarray(toa >= threshold)


def obs_mask_from_polygon(
    src: rasterio.io.DatasetReader,
    obs_geom_raster_crs,
    *,
    all_touched: bool,
) -> np.ndarray:
    """Rasterize observed polygon onto the TOA grid."""
    mask = rasterize(
        [(obs_geom_raster_crs, 1)],
        out_shape=(src.height, src.width),
        transform=src.transform,
        fill=0,
        all_touched=all_touched,
        dtype=np.uint8,
    )
    return mask.astype(bool)


def compute_raster_metrics(
    obs_burn: np.ndarray,
    sim_burn: np.ndarray,
    pixel_area_m2: float,
) -> dict[str, Any]:
    """
    Metrics in raster-space (fast):
      - areas (m2, acres)
      - intersection/union (m2)
      - Jaccard, Sorensen
      - Kappa (domain is full raster grid)
      - ratio_of_areas = sim/obs
    """
    obs_burn = obs_burn.astype(bool)
    sim_burn = sim_burn.astype(bool)

    tp = np.logical_and(obs_burn, sim_burn).sum()
    fp = np.logical_and(~obs_burn, sim_burn).sum()
    fn = np.logical_and(obs_burn, ~sim_burn).sum()
    tn = np.logical_and(~obs_burn, ~sim_burn).sum()

    obs_px = obs_burn.sum()
    sim_px = sim_burn.sum()
    union_px = tp + fp + fn

    obs_m2 = obs_px * pixel_area_m2
    sim_m2 = sim_px * pixel_area_m2
    inter_m2 = tp * pixel_area_m2
    union_m2 = union_px * pixel_area_m2

    j = safe_divide(tp, union_px)
    s = safe_divide(2 * tp, (obs_px + sim_px))

    total = tp + fp + fn + tn
    if total == 0:
        k = 0.0
    else:
        pa = (tp + tn) / total
        pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (total ** 2)
        k = 0.0 if (1 - pe) == 0 else (pa - pe) / (1 - pe)

    return {
        "observed_m2": float(obs_m2),
        "simulated_m2": float(sim_m2),
        "intersection_m2": float(inter_m2),
        "union_m2": float(union_m2),
        "observed_acres": float(obs_m2 * M2_TO_ACRES),
        "simulated_acres": float(sim_m2 * M2_TO_ACRES),
        "jaccard": float(j),
        "sorensen": float(s),
        "kappa": float(k),
        "ratio_of_areas": float(safe_divide(sim_m2, obs_m2)),
    }


# -------------------------
# Satellite coverage (optional)
# -------------------------
def build_satellite_datetime(df: pd.DataFrame, date_col: str, time_col: str) -> pd.Series:
    time_str = df[time_col].astype(str).str.zfill(4)
    dt_str = df[date_col].astype(str) + " " + time_str
    return pd.to_datetime(dt_str, errors="coerce")


def cumulative_coverage_chain(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    discovery_dt: pd.Timestamp,
    buffer_dist: float,
    max_gap: pd.Timedelta,
):
    pts = pts_in_burn.loc[pts_in_burn["sat_datetime"] >= discovery_dt].copy()
    if pts.empty:
        return [], [], None

    pts = pts.sort_values("sat_datetime").reset_index(drop=True)
    times: list[pd.Timestamp] = []
    areas: list[float] = []

    cumulative_geom = None
    first_time = pts.loc[0, "sat_datetime"]
    cumulative_geom = pts.loc[0, "geometry"].buffer(buffer_dist).intersection(burn_geom)
    times.append(first_time)
    areas.append(float(cumulative_geom.area))
    last_time = first_time

    for i in range(1, len(pts)):
        t = pts.loc[i, "sat_datetime"]
        if (t - last_time) > max_gap:
            break
        buf = pts.loc[i, "geometry"].buffer(buffer_dist)
        cumulative_geom = cumulative_geom.union(buf).intersection(burn_geom)
        times.append(t)
        areas.append(float(cumulative_geom.area))
        last_time = t

    return times, areas, cumulative_geom


def satellite_end_time(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    discovery_dt: pd.Timestamp,
    buffer_dist: float,
    max_gap: pd.Timedelta,
    coverage_fraction: float,
):
    times, areas, _ = cumulative_coverage_chain(
        pts_in_burn, burn_geom, discovery_dt, buffer_dist, max_gap
    )
    if not times:
        return pd.NaT, [], [], 0.0

    burn_area = float(burn_geom.area)
    final_sat_area = float(areas[-1])
    effective_area = min(burn_area, final_sat_area)
    target_area = coverage_fraction * effective_area

    end_time = pd.NaT
    for t, a in zip(times, areas):
        if a >= target_area:
            end_time = t
            break
    if pd.isna(end_time):
        end_time = times[-1]

    return end_time, times, areas, target_area


# -------------------------
# Metrics + text formatting registry (easy to extend)
# -------------------------
@dataclass(frozen=True)
class MetricField:
    label: str
    key: str
    fmt: Callable[[Any], str] = str


def f_float(ndigits: int = 4) -> Callable[[Any], str]:
    def _fmt(v: Any) -> str:
        try:
            return f"{float(v):.{ndigits}f}"
        except Exception:
            return str(v)
    return _fmt


def f_int() -> Callable[[Any], str]:
    def _fmt(v: Any) -> str:
        try:
            return f"{int(round(float(v)))}"
        except Exception:
            return str(v)
    return _fmt


def f_m2_acres() -> Callable[[Any], str]:
    def _fmt(v: Any) -> str:
        try:
            m2 = float(v)
            acres = m2 * M2_TO_ACRES
            return f"{m2:,.0f} m²  ({acres:,.2f} acres)"
        except Exception:
            return str(v)
    return _fmt


# Default fields shown in the text panel (add/remove freely)
DEFAULT_TEXT_FIELDS: dict[str, list[MetricField]] = {
    "AREAS": [
        MetricField("Observed", "observed_m2", f_m2_acres()),
        MetricField("Predicted", "simulated_m2", f_m2_acres()),
        MetricField("Intersect", "intersection_m2", f_int()),
        MetricField("Union", "union_m2", f_int()),
        MetricField("Area ratio (pred/obs)", "ratio_of_areas", f_float(3)),
    ],
    "SIMILARITY": [
        MetricField("Jaccard", "jaccard", f_float(4)),
        MetricField("Sorensen", "sorensen", f_float(4)),
        MetricField("Kappa", "kappa", f_float(4)),
    ],
}


def build_text_block(
    res: CaseResult,
    cfg: ReportConfig,
    fields_by_section: dict[str, list[MetricField]] = DEFAULT_TEXT_FIELDS,
    max_stats_lines: int = 24,
) -> str:
    """
    Build the right-hand text panel.
    To add metrics: put them into res.metrics and add MetricField entries above.
    """
    lines: list[str] = [
        f"CASE: {res.case_id}",
        "",
        "FILES",
        f"TOA raster: {res.raster_used.name}",
        f"Threshold : {cfg.burn_threshold}",
        f"Duration (TSTOP): {res.tstop_hours:.2f} hours" if res.tstop_hours is not None else "Duration (TSTOP): (not found)",
    ]
    if res.tstop_source:
        lines.append(f"TSTOP source: {Path(res.tstop_source).name}")

    # Metrics sections
    for section, fields in fields_by_section.items():
        lines += ["", section]
        for mf in fields:
            v = res.metrics.get(mf.key, "")
            lines.append(f"{mf.label:<18}: {mf.fmt(v)}")

    # Simulation stats passthrough (from csv/log)
    if res.stats:
        lines += ["", "SIMULATION STATS"]
        src = res.stats.get("_stats_source", "")
        if src:
            lines.append(f"source: {Path(src).name}")

        keys = [k for k in res.stats.keys() if k != "_stats_source"]
        for k in keys[:max_stats_lines]:
            v = res.stats[k]
            if isinstance(v, float):
                lines.append(f"{k}: {v:.6g}")
            else:
                s = str(v)
                lines.append(f"{k}: {s[:57] + '...' if len(s) > 60 else s}")

    return "\n".join(lines)


# -------------------------
# Plotting (mask overlay; no polygonization)
# -------------------------
def plot_burn_mask(ax, src: rasterio.io.DatasetReader, burned: np.ndarray, alpha: float):
    if burned is None or not burned.any():
        return
    extent = plotting_extent(src)

    # RGBA image: red where burned, transparent elsewhere
    rgba = np.zeros((burned.shape[0], burned.shape[1], 4), dtype=np.float32)
    rgba[..., 0] = burned.astype(np.float32)      # R
    rgba[..., 3] = burned.astype(np.float32) * alpha  # A

    ax.imshow(rgba, extent=extent, origin="upper", interpolation="nearest")


def plot_case_page(
    res: CaseResult,
    cfg: ReportConfig,
    sat_cfg: SatelliteConfig,
    fields_by_section: dict[str, list[MetricField]] = DEFAULT_TEXT_FIELDS,
) -> plt.Figure:
    fig = plt.figure(figsize=(11.69, 8.27))  # A4-ish landscape
    gs = fig.add_gridspec(2, 2, width_ratios=[1.45, 1.0], height_ratios=[1.0, 0.75])

    ax_map = fig.add_subplot(gs[:, 0])
    ax_txt = fig.add_subplot(gs[0, 1])
    ax_ts = fig.add_subplot(gs[1, 1])

    ax_txt.axis("off")
    ax_map.grid(True)
    ax_map.set_aspect("equal", "box")

    # Open raster again here for plotting overlay (cheap compared to polygonization)
    with rasterio.open(res.raster_used) as src:
        sim_burn = sim_mask_from_toa(src, cfg.raster_band, cfg.burn_threshold)
        plot_burn_mask(ax_map, src, sim_burn, alpha=cfg.mask_alpha)

    # Observed outline (already in raster CRS)
    gpd.GeoSeries([res.obs_geom_raster_crs], crs=res.raster_crs).plot(
        ax=ax_map, facecolor="none", edgecolor="black", linewidth=2, label="Observed"
    )

    # Ignitions (already in raster CRS)
    if res.ign_raster_crs is not None and not res.ign_raster_crs.empty:
        res.ign_raster_crs.plot(ax=ax_map, marker="x", markersize=60, label="Ignition", zorder=5)

    # Title (use whatever metrics exist; degrade gracefully if missing)
    j = res.metrics.get("jaccard", np.nan)
    s = res.metrics.get("sorensen", np.nan)
    k = res.metrics.get("kappa", np.nan)
    r = res.metrics.get("ratio_of_areas", np.nan)
    ax_map.set_title(
        f"Case {res.case_id} — Observed outline + Predicted burn mask\n"
        f"J={j:.3f}  S={s:.3f}  K={k:.3f}  Ratio={r:.2f}"
        if cfg.compute_raster_metrics and np.isfinite(j)
        else f"Case {res.case_id} — Observed outline + Predicted burn mask"
    )
    handles = [
        Line2D([0], [0], color="black", linewidth=2, label="Observed"),
        Patch(facecolor="red", edgecolor="none", alpha=cfg.mask_alpha, label="Predicted (mask)"),
    ]
    if res.ign_raster_crs is not None and not res.ign_raster_crs.empty:
        handles.append(Line2D([0], [0], marker="x", linestyle="None", markersize=10, label="Ignition"))
    
    ax_map.legend(handles=handles, loc="upper right")
    # Right text panel (metrics are centrally formatted here)
    text = build_text_block(res, cfg, fields_by_section=fields_by_section)
    ax_txt.text(0.0, 1.0, text, va="top", ha="left", fontsize=10, family="monospace")

    # Satellite curve
# Satellite + model curves (lightweight PDF)
    ax_ts.grid(True)
    ax_ts.set_title("Area evolution (fraction of max)")
    
    # y-axis as fraction of maximum (0..1)
    # Use the maximum among: observed area (if computed), final satellite area, final model area
    obs_m2 = float(res.metrics.get("observed_m2", np.nan))
    sat_max = float(res.sat_areas[-1]) if res.sat_areas else np.nan
    
    # Build model curve in TOA-time; later convert to datetime if discovery_dt exists
    model_times = np.array([], dtype=np.float64)
    model_areas = np.array([], dtype=np.float64)
    
    with rasterio.open(res.raster_used) as src:
        toa = src.read(cfg.raster_band, masked=True)
        # NOTE: keep masked -> np.array(toa) makes masked values into fill; use np.asarray(toa) which preserves mask handling
        toa_arr = np.asarray(toa, dtype=np.float64)
        model_times, model_areas = model_area_curve_from_toa(
            toa_arr,
            burn_threshold=cfg.burn_threshold,
            pixel_area_m2=res.pixel_area_m2,
            bins=cfg.model_curve_bins,
        )
    
    model_max = float(model_areas[-1]) if model_areas.size else np.nan
    
    denom = np.nanmax([obs_m2, sat_max, model_max])
    if not np.isfinite(denom) or denom <= 0:
        denom = 1.0
    
    # Plot satellite curve as a simple line (no markers, decimated)
    if res.sat_times and res.sat_areas:
        t_sat = np.array(res.sat_times, dtype="datetime64[ns]")
        y_sat = (np.array(res.sat_areas, dtype=np.float64) / denom)
    
        t_sat, y_sat = decimate_xy(t_sat, y_sat, cfg.curve_max_points)
    
        ax_ts.plot(t_sat, y_sat, linewidth=cfg.curve_linewidth, label="Satellite (cum. clipped)")
    
        if res.sat_end_time is not None and not pd.isna(res.sat_end_time):
            ax_ts.axvline(res.sat_end_time, linestyle="--", linewidth=1.0, label="SatelliteEndTime")
    
    # Plot modelled area evolution (TOA cumulative) mapped onto datetime using discovery_dt if available
    if model_times.size and model_areas.size:
        y_mod = (model_areas / denom)
    
        # Convert model_time axis:
        # If discovery_dt exists, interpret TOA values as seconds from discovery_dt (cfg.toa_time_unit)
        if res.discovery_dt is not None:
            origin = pd.Timestamp(res.discovery_dt)
            # pandas timedelta supports 's', 'm', 'h', etc. via unit
            t_mod = origin.to_datetime64() + (pd.to_timedelta(model_times, unit=cfg.toa_time_unit).to_numpy())
            t_mod = np.array(t_mod, dtype="datetime64[ns]")
            # decimate to keep PDF light
            t_mod, y_mod = decimate_xy(t_mod, y_mod, cfg.curve_max_points)
            ax_ts.plot(t_mod, y_mod, linewidth=cfg.curve_linewidth, label="Model (TOA cumulative)")
            ax_ts.set_xlabel("Time (UTC, approx)")
        else:
            # fall back to TOA units on x-axis
            model_times_d, y_mod_d = decimate_xy(model_times, y_mod, cfg.curve_max_points)
            ax_ts.plot(model_times_d, y_mod_d, linewidth=cfg.curve_linewidth, label="Model (TOA cumulative)")
            ax_ts.set_xlabel(f"TOA time ({cfg.toa_time_unit})")
    
    # Target line shown as fraction too (optional)
    if res.sat_target_area is not None and np.isfinite(res.sat_target_area):
        ax_ts.axhline((float(res.sat_target_area) / denom), linestyle="--", linewidth=1.0,
                      label=f"{sat_cfg.coverage_fraction*100:.0f}% target")
    
    ax_ts.set_ylim(0.0, 1.05)
    ax_ts.set_ylabel("Area / max")
    
    # Rotate datetime labels only if x is datetime
    ax_ts.tick_params(axis="x", rotation=90)
    
    # If no curves at all:
    if (not res.sat_times or not res.sat_areas) and (model_times.size == 0):
        ax_ts.text(0.5, 0.5, "No satellite/model curve available",
                   ha="center", va="center", transform=ax_ts.transAxes)
        ax_ts.set_xticks([])
        ax_ts.set_yticks([])
    
    ax_ts.legend(fontsize=8)
    fig.tight_layout()
    return fig


# -------------------------
# Per-case pipeline (no polygonization)
# -------------------------
def process_case(
    case_dir: Path,
    cfg: ReportConfig,
    sat_cfg: SatelliteConfig,
    sat_gdf_5070: gpd.GeoDataFrame | None,
    sat_sindex: Any | None,
    master_df: pd.DataFrame | None,
) -> CaseResult:
    firescar_path = case_dir / "firescar.gpkg"
    outputs_dir = case_dir / "outputs"
    if not firescar_path.exists():
        raise FileNotFoundError(f"Missing firescar.gpkg: {firescar_path}")
    if not outputs_dir.exists():
        raise FileNotFoundError(f"Missing outputs/: {outputs_dir}")

    raster_file = newest_raster(outputs_dir, cfg.raster_pattern)

    # Read observed scar and ignitions (in their native CRS), then reproject into raster CRS
    obs_src = read_polygon_union(firescar_path)
    ign_src = read_ignitions(case_dir, cfg)

    with rasterio.open(raster_file) as src:
        raster_crs = src.crs
        raster_transform = src.transform
        raster_shape = (src.height, src.width)
        pixel_area_m2 = abs(src.transform.a * src.transform.e)  # width * height (height is negative)

        # observed geometry in raster CRS (for plotting & rasterize)
        if obs_src.crs is None:
            obs_src = obs_src.set_crs("EPSG:4326")
        obs_geom_raster = obs_src.to_crs(raster_crs).iloc[0].buffer(0)

        # ignition points in raster CRS (for plotting)
        if ign_src.crs is None:
            ign_src = ign_src.set_crs("EPSG:4326")
        try:
            ign_raster = ign_src.to_crs(raster_crs) if not ign_src.empty else gpd.GeoDataFrame(geometry=[], crs=raster_crs)
        except Exception:
            ign_raster = gpd.GeoDataFrame(geometry=[], crs=raster_crs)

        # raster-space metrics (optional, fast)
        metrics: dict[str, Any] = {
            "case": case_dir.name,
            "toa_raster_used": str(raster_file),
            "burn_threshold": cfg.burn_threshold,
        }

        if cfg.compute_raster_metrics:
            sim_burn = sim_mask_from_toa(src, cfg.raster_band, cfg.burn_threshold)
            obs_burn = obs_mask_from_polygon(src, obs_geom_raster, all_touched=cfg.all_touched_observed)
            metrics.update(compute_raster_metrics(obs_burn, sim_burn, pixel_area_m2))

    stats = read_sim_stats(case_dir, cfg)
    tstop_h, tstop_src = parse_tstop_hours(case_dir, cfg)

    # Satellite (optional): sat_gdf_5070 and sindex are precomputed once outside
    sat_times: list[pd.Timestamp] = []
    sat_areas: list[float] = []
    sat_end: pd.Timestamp | pd.NaT = pd.NaT
    sat_target: float | None = None

    if sat_cfg.enabled and sat_gdf_5070 is not None and master_df is not None and sat_sindex is not None:
        try:
            folder_int = int(case_dir.name)
            row = master_df.loc[master_df[sat_cfg.folder_col].astype(int) == folder_int]
            if not row.empty:
                disc_dt = pd.to_datetime(row.iloc[0][sat_cfg.discovery_col], errors="coerce")
                if disc_dt is not None and not pd.isna(disc_dt):
                    if getattr(disc_dt, "tzinfo", None) is not None:
                        disc_dt = disc_dt.tz_convert("UTC").tz_localize(None)

                    # burn polygon in 5070 from observed geometry
                    burn_5070 = gpd.GeoSeries([obs_geom_raster], crs=raster_crs).to_crs(sat_cfg.area_crs).iloc[0]

                    # spatial-index prefilter, then precise within()
                    cand_idx = list(sat_sindex.intersection(burn_5070.bounds))
                    pts_cand = sat_gdf_5070.iloc[cand_idx]
                    pts_in_burn = pts_cand.loc[pts_cand.geometry.within(burn_5070)].copy()

                    if not pts_in_burn.empty:
                        sat_end, sat_times, sat_areas, sat_target = satellite_end_time(
                            pts_in_burn=pts_in_burn,
                            burn_geom=burn_5070,
                            discovery_dt=disc_dt,
                            buffer_dist=sat_cfg.hotspot_buffer_dist,
                            max_gap=sat_cfg.max_gap,
                            coverage_fraction=sat_cfg.coverage_fraction,
                        )
                        metrics["satellite_end_time"] = str(sat_end) if not pd.isna(sat_end) else ""
                        metrics["satellite_target_area_m2"] = float(sat_target) if sat_target is not None else np.nan
                case_discovery_dt = disc_dt
        except Exception:
            metrics["satellite_end_time"] = ""
            metrics["satellite_target_area_m2"] = np.nan

    return CaseResult(
        case_id=case_dir.name,
        raster_used=raster_file,
        raster_crs=raster_crs,
        raster_transform=raster_transform,
        raster_shape=raster_shape,
        pixel_area_m2=pixel_area_m2,
        obs_geom_raster_crs=obs_geom_raster,
        ign_raster_crs=ign_raster,
        stats=stats,
        tstop_hours=tstop_h,
        tstop_source=tstop_src,
        metrics=metrics,
        sat_times=sat_times,
        sat_areas=sat_areas,
        sat_end_time=sat_end,
        sat_target_area=sat_target,
        discovery_dt=case_discovery_dt,
    )


# -------------------------
# Case discovery + PDF build
# -------------------------
def list_case_folders(cfg: ReportConfig) -> list[Path]:
    if cfg.case_ids is None:
        return [d for d in sorted(cfg.root_dir.iterdir()) if d.is_dir() and (d / "firescar.gpkg").exists()]
    return [cfg.root_dir / cid for cid in cfg.case_ids]


def build_report_pdf(
    cfg: ReportConfig,
    sat_cfg: SatelliteConfig,
    sat_gdf_5070: gpd.GeoDataFrame | None,
    sat_sindex: Any | None,
    master_df: pd.DataFrame | None,
    fields_by_section: dict[str, list[MetricField]] = DEFAULT_TEXT_FIELDS,
) -> pd.DataFrame:
    case_dirs = list_case_folders(cfg)
    if not case_dirs:
        raise RuntimeError("No cases found. Check ROOT_DIR / CASE_IDS and firescar.gpkg presence.")

    cfg.output_pdf.parent.mkdir(parents=True, exist_ok=True)

    metrics_list: list[dict[str, Any]] = []
    with PdfPages(cfg.output_pdf) as pdf:
        for case_dir in case_dirs:
            try:
                res = process_case(case_dir, cfg, sat_cfg, sat_gdf_5070, sat_sindex, master_df)
                fig = plot_case_page(res, cfg, sat_cfg, fields_by_section=fields_by_section)
                pdf.savefig(fig)
                plt.close(fig)
                metrics_list.append(res.metrics)
                log.info("Added page: %s", case_dir.name)
            except Exception as e:
                log.warning("SKIP %s: %s", case_dir.name, e)

    return pd.DataFrame(metrics_list)


# -------------------------
# Run
# -------------------------
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    ROOT_DIR = Path(r"/home/nick/elmfire_validation/FirePairs")
    cfg = ReportConfig(
        root_dir=ROOT_DIR,
        output_pdf=ROOT_DIR / "elmfire_validation_report.pdf",
        case_ids=None,
        burn_threshold=1.0,
        compute_raster_metrics=True,  # set False if you only want pictures
        all_touched_observed=False,
        mask_alpha=0.35,
    )

    # Optional: load from your pipelineConfig if present
    try:
        import pipelineConfig  # type: ignore

        sat_cfg = SatelliteConfig(
            enabled=True,
            gpkg=Path(pipelineConfig.SATELLITE_GPKG),
            layer=pipelineConfig.SATELLITE_LAYER_NAME,
            date_col=pipelineConfig.SAT_DATE_COL,
            time_col=pipelineConfig.SAT_TIME_COL,
            master_csv=Path(pipelineConfig.FIRE_SUMMARY_CSV),
            folder_col=pipelineConfig.COL_FOLDER,
            discovery_col=pipelineConfig.COL_POINT_DISCOVERY,
            area_crs="EPSG:5070",
            max_gap=pd.Timedelta(days=pipelineConfig.SAT_CHAIN_MAX_GAP_DAYS),
            coverage_fraction=0.85,
            hotspot_buffer_dist=getattr(pipelineConfig, "SAT_HOTSPOT_BUFFER_DIST", 500.0),
        )
    except Exception:
        sat_cfg = SatelliteConfig(enabled=False)

    sat_gdf_5070: gpd.GeoDataFrame | None = None
    sat_sindex = None
    master_df: pd.DataFrame | None = None

    if sat_cfg.enabled:
        log.info("Loading satellite points...")
        sat_gdf = gpd.read_file(sat_cfg.gpkg, layer=sat_cfg.layer)

        # If your satellite file has incorrect CRS metadata, you can override here.
        # Otherwise, remove allow_override=True and just rely on source metadata.
        sat_gdf = sat_gdf.set_crs(sat_cfg.area_crs, inplace=False, allow_override=True)

        sat_gdf["sat_datetime"] = build_satellite_datetime(sat_gdf, sat_cfg.date_col, sat_cfg.time_col)
        sat_gdf = sat_gdf.dropna(subset=["sat_datetime"])

        # Preproject ONCE and build spatial index ONCE (big speedup)
        sat_gdf_5070 = sat_gdf.to_crs(sat_cfg.area_crs)
        sat_sindex = sat_gdf_5070.sindex

        log.info("Loading master CSV...")
        master_df = pd.read_csv(sat_cfg.master_csv)

    # Customize which metrics appear in the right text panel by editing DEFAULT_TEXT_FIELDS
    df_metrics = build_report_pdf(
        cfg=cfg,
        sat_cfg=sat_cfg,
        sat_gdf_5070=sat_gdf_5070,
        sat_sindex=sat_sindex,
        master_df=master_df,
        fields_by_section=DEFAULT_TEXT_FIELDS,
    )

    log.info("Done. Wrote: %s", cfg.output_pdf)
    # Optional: save metrics table alongside PDF
    # df_metrics.to_csv(cfg.output_pdf.with_suffix(".csv"), index=False)


if __name__ == "__main__":
    main()