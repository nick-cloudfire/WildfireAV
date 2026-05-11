# -*- coding: utf-8 -*-
"""
One multi-page PDF: model comparison validation report (one page per case folder).

Models are defined in MODELS below — add or remove entries to cover any
combination of simulators without touching the rest of the script.

Page layout (A4 landscape)
--------------------------
Left   : burn-mask map (all models + observed outline + ignition marker)
Top-right  : text block (file paths + metrics table)
Bottom-right: area-evolution curves (sim-time axis + satellite datetime axis)
Suptitle: fire name, year, case ID
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
import logging
import os
import re
from typing import Any

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import rasterize
from rasterio.plot import plotting_extent
from rasterio.warp import reproject, Resampling
import matplotlib.pyplot as plt
import matplotlib as mpl
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import matplotlib.dates as mdates

import pipelineConfig as PC
from case_metadata import read_case_metadata

log = logging.getLogger("validation")

mpl.rcParams["pdf.fonttype"] = 42
mpl.rcParams["ps.fonttype"]  = 42
mpl.rcParams["font.family"]  = "DejaVu Sans Mono"

M2_TO_ACRES     = 0.000247105381
AREA_CRS        = "EPSG:5070"
_UNIT_TO_HOURS  = {"s": 1 / 3600.0, "sec": 1 / 3600.0, "min": 1 / 60.0, "h": 1.0, "hr": 1.0}

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------
# Add / remove / reorder entries freely — the rest of the script adapts.
# toa_rel  : path relative to case_dir; use forward slashes.
# is_glob  : if True, treat toa_rel as a glob and pick the newest match.
# toa_unit : time unit stored in the TOA raster ("s", "min", or "h").
# color    : RGB tuple used for the burn mask and curve.
# alpha    : transparency of the burn-mask overlay on the map.
# ---------------------------------------------------------------------------

@dataclass
class ModelConfig:
    label:    str
    toa_rel:  str
    band:     int   = 1
    toa_unit: str   = "s"
    is_glob:  bool  = False
    color:    tuple = (1.0, 0.0, 0.0)
    alpha:    float = 0.35

    def find_toa(self, case_dir: Path) -> Path | None:
        if self.is_glob:
            parent, pattern = self.toa_rel.rsplit("/", 1)
            try:
                return _newest_glob(case_dir / parent, pattern)
            except FileNotFoundError:
                return None
        p = case_dir / self.toa_rel
        return p if p.exists() else None

    @property
    def scale_to_hours(self) -> float:
        return _UNIT_TO_HOURS.get(self.toa_unit.lower(), 1 / 3600.0)

    @property
    def rgba(self) -> tuple:
        return (*self.color, self.alpha)


MODELS: list[ModelConfig] = [
    ModelConfig(
        label    = "ELMFIRE",
        toa_rel  = "outputs/time_of_arrival_*.tif",
        band     = 1,
        toa_unit = "s",
        is_glob  = True,
        color    = (1.0, 0.0, 0.0),
        alpha    = 0.35,
    ),
    ModelConfig(
        label    = "FARSITE",
        toa_rel  = "farsite/outputs/farsite_ArrivalTime.asc",
        band     = 1,
        toa_unit = "min",
        color    = (0.0, 0.4, 1.0),
        alpha    = 0.35,
    ),
]

# ---------------------------------------------------------------------------
# Other user-configurable settings
# ---------------------------------------------------------------------------

ROOT_DIR         = PC.FIRE_ROOT
OUT_PDF          = ROOT_DIR / "validation_report.pdf"

BURN_THRESHOLD        = 1.0
ALL_TOUCHED_OBS       = False
CURVE_MAX_POINTS      = 300
MODEL_CURVE_BINS      = 600
NON_IGNITION_JACCARD  = 0.01   # cases where max Jaccard < this are treated as non-ignited

FIRESCAR_NAME  = getattr(PC, "BURN_SHAPE_NAME",            "firescar.gpkg")
IGNITION_NAME  = getattr(PC, "IGNITION_POINT_SHP_NAME",    "ignition_point.gpkg")
SAT_ENABLED    = True
SAT_GPKG_NAME  = getattr(PC, "CASE_SAT_GPKG_NAME",         "satellite_points.gpkg")
SAT_LAYER      = "points_in_burn"
SAT_DATE_COL   = getattr(PC, "SAT_DATE_COL",               "ACQ_DATE")
SAT_TIME_COL   = getattr(PC, "SAT_TIME_COL",               "ACQ_TIME")
SAT_CHAIN_MAX_GAP   = pd.Timedelta(days=float(getattr(PC, "SAT_CHAIN_MAX_GAP_DAYS",   7)))
SAT_BUFFER_M        = float(getattr(PC, "SAT_HOTSPOT_BUFFER_DIST", 200))
SAT_COVERAGE_FRAC   = float(getattr(PC, "COVERAGE_FRACTION",        0.9))


# ---------------------------------------------------------------------------
# Case data container
# ---------------------------------------------------------------------------

@dataclass
class Case:
    case_id:       str
    fire_name:     str | None
    fire_year:     int | None
    model_toas:    dict[str, Path | None]   # label → toa path
    obs_geom_base: Any                      # shapely geom in ELMFIRE CRS
    ign_base:      gpd.GeoDataFrame
    tstop_h:       float | None
    tstop_src:     str | None

    obs_true_m2:   float | None             = None
    avg_wind_kph:  float | None             = None
    # toa_arrays: label → (float64 toa array in native units, pixel_area_m2)
    # Cached here to avoid re-opening rasters in _case_page.
    toa_arrays:    dict[str, tuple[np.ndarray, float]] = field(default_factory=dict)
    discovery_dt:  pd.Timestamp | None      = None
    sat_times:     list[pd.Timestamp]       = field(default_factory=list)
    sat_areas_m2:  list[float]              = field(default_factory=list)
    sat_end:       pd.Timestamp | pd.NaT    = pd.NaT
    sat_target_m2: float | None             = None
    metrics:       dict[str, Any]           = field(default_factory=dict)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _newest_glob(parent: Path, pattern: str) -> Path:
    hits = sorted(parent.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not hits:
        raise FileNotFoundError(f"No match: {parent}/{pattern}")
    return hits[-1]


def _rel_or_name(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except Exception:
        return p.name


def _tstop_hours(case_dir: Path) -> tuple[float | None, str | None]:
    data = case_dir / f"{case_dir.name}.data"
    if not data.exists():
        return None, None
    m = re.search(r"SIMULATION_TSTOP\s*=\s*([0-9]+(?:\.[0-9]+)?)", data.read_text(errors="ignore"))
    return (float(m.group(1)) / 3600.0 if m else None), str(data)


def _fire_name_year(case_dir: Path, meta: dict | None = None) -> tuple[str | None, int | None]:
    try:
        if meta is None:
            meta = read_case_metadata(case_dir)
        name = meta.get("perim_name") or meta.get("point_name")
        raw  = meta.get("perim_ignition")
        year = pd.to_datetime(raw, errors="coerce").year if raw else None
        return (str(name) if name else None), (int(year) if year and not pd.isna(year) else None)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# WXS wind helper
# ---------------------------------------------------------------------------

def _wxs_avg_wind_kph(case_dir: Path, meta: dict | None = None) -> float | None:
    """Return mean wind speed (kph) from the WXS file over the fire window.

    The fire window is defined by SatelliteIgnitionTime → SatelliteEndTime from
    case metadata (mirroring the logic in downloadAndRunWindninja_WXS.py).
    Falls back to the full file average if the window cannot be determined.
    Pass *meta* (already loaded by the caller) to avoid a redundant file read.
    """
    wxs_path = case_dir / PC.INPUTS_SUBDIR_NAME / PC.WXS_FILE_NAME
    if not wxs_path.exists():
        return None
    try:
        lines = wxs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        header_idx = next(
            (i for i, ln in enumerate(lines)
             if ln.strip().startswith("Year") and "WindSpd" in ln),
            None,
        )
        if header_idx is None:
            return None
        # Single-pass: collect wind values (and optional timestamps) together
        timestamps, wind_vals = [], []
        for ln in lines[header_idx + 1:]:
            parts = ln.split()
            if len(parts) < 10:
                continue
            wind_vals.append(float(parts[7]))
            timestamps.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3].zfill(4)))
        if not wind_vals:
            return None
        # Filter to the fire simulation window if metadata available
        try:
            if meta is None:
                meta = read_case_metadata(case_dir)
            start = pd.to_datetime(meta.get(PC.COL_SATELLITE_IGNITION))
            end   = pd.to_datetime(meta.get(PC.COL_SATELLITE_END))
            if pd.notna(start) and pd.notna(end):
                if getattr(start, "tzinfo", None) is not None:
                    start = start.tz_convert("UTC").tz_localize(None)
                if getattr(end, "tzinfo", None) is not None:
                    end = end.tz_convert("UTC").tz_localize(None)
                start_h = start.floor("h")
                stop_h  = (end + pd.Timedelta(hours=1)).floor("h")
                window = [
                    w for (yr, mo, dy, hhmm), w in zip(timestamps, wind_vals)
                    if start_h
                    <= pd.Timestamp(year=yr, month=mo, day=dy,
                                    hour=int(hhmm[:2]), minute=int(hhmm[2:]))
                    <= stop_h
                ]
                if window:
                    return float(sum(window) / len(window))
        except Exception:
            pass
        return float(sum(wind_vals) / len(wind_vals))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _firescar_union(path: Path) -> gpd.GeoSeries:
    gdf  = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"No geometries in {path}")
    geom = gdf.geometry.union_all()
    try:
        geom = geom.buffer(0)
    except Exception:
        pass
    return gpd.GeoSeries([geom], crs=gdf.crs)


def _read_ignition(case_dir: Path) -> gpd.GeoDataFrame:
    p = case_dir / IGNITION_NAME
    if not p.exists():
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    gdf = gpd.read_file(p)
    return gdf if not gdf.empty else gpd.GeoDataFrame(geometry=[], crs=gdf.crs or "EPSG:4326")


# ---------------------------------------------------------------------------
# Raster helpers
# ---------------------------------------------------------------------------

def _require_projected(ds: rasterio.io.DatasetReader, label: str,
                       fallback_crs=None):
    """Return the CRS to use for *ds*.

    If the raster has no embedded CRS and *fallback_crs* is provided (e.g. the
    base-model CRS), the fallback is returned with a debug log instead of
    raising — useful for FARSITE outputs that lack CRS metadata but are known
    to be in the same projected system as the ELMFIRE raster.
    Raises if the CRS is geographic or missing with no fallback.
    """
    if ds.crs is None:
        if fallback_crs is not None:
            log.debug("%s: no CRS embedded — assuming base CRS %s", label, fallback_crs)
            return fallback_crs
        raise ValueError(f"{label}: CRS is None and no fallback provided.")
    if getattr(ds.crs, "is_geographic", False):
        raise ValueError(
            f"{label}: CRS is {ds.crs}. Rasters must be in a projected (meter) CRS."
        )
    return ds.crs


def _pixel_area_m2(ds: rasterio.io.DatasetReader) -> float:
    t = ds.transform
    return float(abs(t.a * t.e - t.b * t.d))


def _burn_mask(ds: rasterio.io.DatasetReader, band: int, threshold: float) -> np.ndarray:
    arr  = ds.read(band, masked=True)
    data = np.asarray(arr.filled(np.nan), dtype=np.float32)
    if ds.nodata is not None:
        data = np.where(np.isclose(data, ds.nodata), np.nan, data)
    return np.isfinite(data) & (data >= threshold)


def _obs_mask(ds: rasterio.io.DatasetReader, geom) -> np.ndarray:
    m = rasterize(
        [(geom, 1)],
        out_shape=(ds.height, ds.width),
        transform=ds.transform,
        fill=0,
        all_touched=ALL_TOUCHED_OBS,
        dtype=np.uint8,
    )
    return m.astype(bool)


def _safe_div(n: float, d: float) -> float:
    return 0.0 if d == 0 else n / d


def _raster_metrics(obs: np.ndarray, sim: np.ndarray, px_m2: float, prefix: str) -> dict[str, Any]:
    obs, sim = obs.astype(bool), sim.astype(bool)
    tp = int(np.logical_and(obs,  sim).sum())
    fp = int(np.logical_and(~obs, sim).sum())
    fn = int(np.logical_and(obs, ~sim).sum())
    tn = int(np.logical_and(~obs, ~sim).sum())
    obs_m2  = obs.sum() * px_m2
    sim_m2  = sim.sum() * px_m2
    union   = tp + fp + fn
    jaccard = _safe_div(tp, union)
    sorensen = _safe_div(2 * tp, obs.sum() + sim.sum())
    total   = tp + fp + fn + tn
    if total:
        pa = (tp + tn) / total
        pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / total ** 2
        kappa = 0.0 if (1 - pe) == 0 else (pa - pe) / (1 - pe)
    else:
        kappa = 0.0
    return {
        f"{prefix}_observed_m2":    float(obs_m2),
        f"{prefix}_simulated_m2":   float(sim_m2),
        f"{prefix}_observed_acres": float(obs_m2  * M2_TO_ACRES),
        f"{prefix}_simulated_acres":float(sim_m2  * M2_TO_ACRES),
        f"{prefix}_jaccard":        float(jaccard),
        f"{prefix}_sorensen":       float(sorensen),
        f"{prefix}_kappa":          float(kappa),
        f"{prefix}_ratio_of_areas": float(_safe_div(sim_m2, obs_m2)),
        f"{prefix}_burn_px":        int(sim.sum()),
    }


def _model_curve(toa: np.ndarray, threshold: float, px_m2: float, bins: int
                 ) -> tuple[np.ndarray, np.ndarray]:
    v = toa[np.isfinite(toa) & (toa >= threshold)]
    if v.size == 0:
        return np.array([]), np.array([])
    vmin, vmax = float(v.min()), float(v.max())
    if vmax <= vmin:
        a = float(v.size) * px_m2
        return np.array([vmin, vmax]), np.array([a, a])
    counts, edges = np.histogram(v, bins=bins, range=(vmin, vmax))
    return edges[1:], np.cumsum(counts).astype(np.float64) * px_m2


def _reproject_mask(ds_src, ds_dst, mask_u8: np.ndarray, src_crs=None) -> np.ndarray:
    dst = np.zeros((ds_dst.height, ds_dst.width), dtype=np.uint8)
    reproject(
        source=mask_u8, destination=dst,
        src_transform=ds_src.transform, src_crs=src_crs or ds_src.crs,
        dst_transform=ds_dst.transform, dst_crs=ds_dst.crs,
        resampling=Resampling.nearest, src_nodata=0, dst_nodata=0,
    )
    return dst


def _decimate(x: np.ndarray, y: np.ndarray, max_pts: int):
    if max_pts <= 0 or len(x) <= max_pts:
        return x, y
    idx = np.unique(np.linspace(0, len(x) - 1, max_pts).round().astype(int))
    return x[idx], y[idx]


def _norm(y: np.ndarray) -> np.ndarray:
    y   = np.asarray(y, dtype=np.float64)
    den = float(np.nanmax(y)) if y.size else 0.0
    return (y / den) if (np.isfinite(den) and den > 0) else y


# ---------------------------------------------------------------------------
# Satellite helpers
# ---------------------------------------------------------------------------

def _sat_datetime(df: pd.DataFrame) -> pd.Series:
    t = df[SAT_TIME_COL].astype(str).str.replace(r"\D+", "", regex=True).str.zfill(4).str[:4]
    return pd.to_datetime(df[SAT_DATE_COL].astype(str).str.strip() + " " + t, errors="coerce")


def _read_case_sat(case_dir: Path) -> gpd.GeoDataFrame | None:
    p = case_dir / SAT_GPKG_NAME
    if not p.exists():
        return None
    gdf = gpd.read_file(p, layer=SAT_LAYER) if SAT_LAYER else gpd.read_file(p)
    if gdf.empty:
        return None
    if gdf.crs is None:
        b = gdf.total_bounds
        if (-180 <= b[0] <= 180) and (-90 <= b[1] <= 90):
            gdf = gdf.set_crs("EPSG:4326")
        else:
            raise ValueError(f"Satellite CRS missing (bounds={b}) in {p.name}")
    gdf["sat_dt"] = _sat_datetime(gdf)
    gdf = gdf.dropna(subset=["sat_dt"])
    return None if gdf.empty else gdf


def _sat_chain(pts_area, burn_geom, discovery_dt, buffer_m, max_gap, frac
               ) -> tuple[pd.Timestamp | pd.NaT, list, list, float | None]:
    pts = pts_area.loc[pts_area["sat_dt"] >= discovery_dt].sort_values("sat_dt")
    if pts.empty:
        return pd.NaT, [], [], None
    times, areas, cum, last = [], [], None, None
    for _, r in pts.iterrows():
        t = r["sat_dt"]
        if last is not None and (t - last) > max_gap:
            break
        g = r.geometry.buffer(buffer_m)
        cum = g if cum is None else cum.union(g)
        areas.append(float(cum.intersection(burn_geom).area))
        times.append(t)
        last = t
    eff    = min(float(burn_geom.area), float(areas[-1]))
    target = float(frac * eff)
    end    = next((t for t, a in zip(times, areas) if a >= target), times[-1])
    return end, times, areas, target


# ---------------------------------------------------------------------------
# Map helpers
# ---------------------------------------------------------------------------

def _plot_mask(ax, ds, mask: np.ndarray, rgba) -> None:
    if mask is None or not mask.any():
        return
    r, g, b, a = rgba
    img = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.float32)
    m   = mask.astype(np.float32)
    img[..., 0] = m * r
    img[..., 1] = m * g
    img[..., 2] = m * b
    img[..., 3] = m * a
    ax.imshow(img, extent=plotting_extent(ds), origin="upper", interpolation="nearest")


def _plot_barrier(ax, barrier_path: Path, base_ds, alpha: float = 0.15, gray: float = 0.2) -> None:
    if not barrier_path.exists():
        return
    try:
        with rasterio.open(barrier_path) as src:
            raw = src.read(1)
            dst = np.zeros((base_ds.height, base_ds.width), dtype=raw.dtype)
            reproject(
                source=raw, destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=base_ds.transform, dst_crs=base_ds.crs,
                resampling=Resampling.nearest,
            )
        mask = (dst != 0).astype(np.float32)
        if not mask.any():
            return
        img = np.zeros((*mask.shape, 4), dtype=np.float32)
        img[..., :3] = mask[..., None] * gray
        img[..., 3]  = mask * alpha
        ax.imshow(img, extent=plotting_extent(base_ds), origin="upper",
                  interpolation="nearest", zorder=0)
    except Exception as e:
        log.warning("Barrier overlay failed: %s", e)


# ---------------------------------------------------------------------------
# Text block
# ---------------------------------------------------------------------------

def _fmt(v: Any, kind: str = "") -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, float) and not np.isfinite(v):
            return ""
    except Exception:
        pass
    try:
        if kind == "m2": return f"{float(v):,.0f}"
        if kind == "ac": return f"{float(v):,.2f}"
        if kind == "f4": return f"{float(v):.4f}"
        if kind == "r3": return f"{float(v):.3f}"
        return str(v)
    except Exception:
        return str(v)


def _text_block(c: Case, case_dir: Path) -> str:
    m       = c.metrics
    active  = [mc for mc in MODELS if c.model_toas.get(mc.label)]
    cw      = max(10, max((len(mc.label) for mc in active), default=10))  # column width
    pad     = 14  # label column width

    lines = []

    # — files —
    lines += ["FILES"]
    for mc in MODELS:
        toa = c.model_toas.get(mc.label)
        val = _rel_or_name(toa, case_dir) if toa else "(not found)"
        lines.append(f"  {mc.label + ' TOA':<{pad}}: {val}")
    lines.append(f"  {'Threshold':<{pad}}: {BURN_THRESHOLD}")
    if c.tstop_h is not None:
        lines.append(f"  {'TSTOP':<{pad}}: {c.tstop_h:.2f} h")
    else:
        lines.append(f"  {'TSTOP':<{pad}}: (not found)")
    if c.obs_true_m2 is not None:
        lines.append(
            f"  {'Obs area':<{pad}}: "
            f"{_fmt(c.obs_true_m2, 'm2')} m²  "
            f"({_fmt(c.obs_true_m2 * M2_TO_ACRES, 'ac')} ac)"
        )

    # — metrics table —
    header = f"\n{'METRICS':14}" + "".join(f"  {mc.label:>{cw}}" for mc in active)
    sep    = "─" * (14 + len(active) * (cw + 2))
    lines += [header, sep]
    rows = [
        ("Sim m²",     "m2", "simulated_m2"),
        ("Obs m²",     "m2", "observed_m2"),
        ("Jaccard",    "f4", "jaccard"),
        ("Sorensen",   "f4", "sorensen"),
        ("Kappa",      "f4", "kappa"),
        ("Area ratio", "r3", "ratio_of_areas"),
        ("Burn px",    "",   "burn_px"),
    ]
    for lbl, kind, key in rows:
        cols = "".join(
            f"  {_fmt(m.get(f'{mc.label.lower()}_{key}'), kind):>{cw}}"
            for mc in active
        )
        lines.append(f"{lbl:<14}{cols}")

    # — satellite —
    if SAT_ENABLED:
        lines += ["\nSATELLITE"]
        if c.discovery_dt is not None and not pd.isna(c.discovery_dt):
            lines.append(f"  Discovery   : {c.discovery_dt:%Y-%m-%d %H:%M}")
        lines.append(f"  Points      : {len(c.sat_times)}")
        if c.sat_areas_m2:
            lines.append(f"  Final area  : {_fmt(c.sat_areas_m2[-1], 'm2')} m²")
        if c.sat_end is not None and not pd.isna(c.sat_end):
            lines.append(f"  End time    : {c.sat_end}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def _case_page(c: Case, case_dir: Path) -> plt.Figure:
    fire_label = c.fire_name or ""
    year_label = str(c.fire_year) if c.fire_year else ""
    if fire_label and year_label:
        suptitle = f"{fire_label}  ({year_label})  —  Case {c.case_id}"
    elif fire_label:
        suptitle = f"{fire_label}  —  Case {c.case_id}"
    else:
        suptitle = f"Case {c.case_id}"

    fig = plt.figure(figsize=(11.69, 8.27))  # A4 landscape
    fig.suptitle(suptitle, fontsize=12, fontweight="bold", y=0.99)

    gs      = fig.add_gridspec(2, 2, width_ratios=[1.45, 1.0], height_ratios=[1.0, 0.75])
    ax_map  = fig.add_subplot(gs[:, 0])
    ax_txt  = fig.add_subplot(gs[0, 1])
    ax_ts   = fig.add_subplot(gs[1, 1])
    ax_txt.axis("off")

    # ------------------------------------------------------------------
    # MAP
    # ------------------------------------------------------------------
    ax_map.grid(True, alpha=0.2, linewidth=0.6)
    ax_map.set_aspect("equal", "box")

    # Use first available model as spatial reference
    base_mc  = next((mc for mc in MODELS if c.model_toas.get(mc.label)), None)
    base_toa = c.model_toas[base_mc.label] if base_mc else None

    plotted_models: list[str] = []   # models whose mask was successfully drawn
    failed_models:  list[str] = []   # models whose overlay raised an exception

    if base_toa:
        with rasterio.open(base_toa) as base_ds:
            _require_projected(base_ds, f"{base_mc.label} TOA")
            _plot_barrier(ax_map, case_dir / PC.INPUTS_SUBDIR_NAME / PC.BARRIER_FILE_NAME, base_ds)

            for mc in MODELS:
                toa = c.model_toas.get(mc.label)
                if not toa:
                    continue
                try:
                    if toa == base_toa:
                        mask = _burn_mask(base_ds, mc.band, BURN_THRESHOLD)
                        _plot_mask(ax_map, base_ds, mask, mc.rgba)
                    else:
                        with rasterio.open(toa) as ds2:
                            eff_crs = _require_projected(ds2, f"{mc.label} TOA",
                                                         fallback_crs=base_ds.crs)
                            raw_u8 = _burn_mask(ds2, mc.band, BURN_THRESHOLD).astype(np.uint8)
                            on_base = _reproject_mask(ds2, base_ds, raw_u8,
                                                      src_crs=eff_crs).astype(bool)
                            _plot_mask(ax_map, base_ds, on_base, mc.rgba)
                    plotted_models.append(mc.label)
                except Exception as e:
                    log.warning("Case %s: %s map overlay failed: %s", c.case_id, mc.label, e)
                    failed_models.append(mc.label)

            gpd.GeoSeries([c.obs_geom_base], crs=base_ds.crs).plot(
                ax=ax_map, facecolor="none", edgecolor="black", linewidth=2
            )
            if c.ign_base is not None and not c.ign_base.empty:
                c.ign_base.plot(ax=ax_map, marker="x", markersize=60, zorder=5)

            try:
                minx, miny, maxx, maxy = c.obs_geom_base.bounds
                pad = 0.1 * max(maxx - minx, maxy - miny)
                ax_map.set_xlim(minx - pad, maxx + pad)
                ax_map.set_ylim(miny - pad, maxy + pad)
            except Exception:
                pass

    log.info("Case %s map: plotted=%s%s", c.case_id, plotted_models,
             f"  FAILED={failed_models}" if failed_models else "")

    # Map title: Jaccard scores per model
    j_parts = []
    for mc in MODELS:
        j = c.metrics.get(f"{mc.label.lower()}_jaccard", np.nan)
        if np.isfinite(j):
            j_parts.append(f"J({mc.label})={j:.3f}")
    ax_map.set_title("Burn masks" + ("  |  " + "  ".join(j_parts) if j_parts else ""))

    # Legend — only list models that were actually drawn; mark failures explicitly
    handles = [Line2D([0], [0], color="black", linewidth=2, label="Observed")]
    for mc in MODELS:
        if mc.label in plotted_models:
            handles.append(Patch(facecolor=mc.color, alpha=mc.alpha, label=f"{mc.label} burn"))
        elif mc.label in failed_models:
            handles.append(Patch(facecolor="none", edgecolor=mc.color, linewidth=1,
                                 linestyle="--", label=f"{mc.label} (overlay error)"))
        elif not c.model_toas.get(mc.label):
            handles.append(Patch(facecolor="none", edgecolor="gray", linewidth=1,
                                 linestyle=":", label=f"{mc.label} (not found)"))
    if c.ign_base is not None and not c.ign_base.empty:
        handles.append(Line2D([0], [0], marker="x", linestyle="None", markersize=10, label="Ignition"))
    ax_map.legend(handles=handles, loc="upper right", fontsize=8)

    # ------------------------------------------------------------------
    # TEXT
    # ------------------------------------------------------------------
    ax_txt.text(
        0.0, 1.0, _text_block(c, case_dir),
        va="top", ha="left", fontsize=8, family="monospace", transform=ax_txt.transAxes,
    )

    # ------------------------------------------------------------------
    # AREA EVOLUTION
    # ------------------------------------------------------------------
    ax_sim = ax_ts
    ax_sat = ax_ts.twiny()

    ax_sim.grid(True, alpha=0.3, linewidth=0.6)
    ax_sim.set_title("Area evolution (each curve normalized to its own max)")
    ax_sim.set_ylabel("Area / max")
    ax_sim.set_ylim(0.0, 1.05)
    ax_sim.set_xlabel("Simulation time (hours)")

    xmax = 0.0
    for mc in MODELS:
        if not c.model_toas.get(mc.label):
            continue
        try:
            cached = c.toa_arrays.get(mc.label)
            if cached is not None:
                toa_arr, px_m2 = cached
            else:
                # Fallback: re-open if not cached (shouldn't normally happen).
                with rasterio.open(c.model_toas[mc.label]) as ds:
                    toa_arr = np.asarray(ds.read(mc.band, masked=True).filled(np.nan),
                                         np.float64)
                    px_m2 = _pixel_area_m2(ds)
            t_vals, a_vals = _model_curve(toa_arr, BURN_THRESHOLD, px_m2, MODEL_CURVE_BINS)
            if t_vals.size and a_vals.size:
                xh = t_vals * mc.scale_to_hours
                xh, yn = _decimate(xh, _norm(a_vals), CURVE_MAX_POINTS)
                ax_sim.plot(xh, yn, color=mc.color, linewidth=1.5, label=f"{mc.label} (sim)")
                xmax = max(xmax, float(xh[-1]))
        except Exception as e:
            log.warning("Case %s: %s curve failed: %s", c.case_id, mc.label, e)

    if c.tstop_h is not None and np.isfinite(c.tstop_h) and c.tstop_h > 0:
        xmax = float(c.tstop_h)
    if xmax > 0:
        ax_sim.set_xlim(0.0, xmax)

    # Satellite curve
    if c.sat_times and c.sat_areas_m2:
        t_sat = np.array(c.sat_times, dtype="datetime64[ns]")
        y_sat, t_sat = _norm(np.array(c.sat_areas_m2)), t_sat
        t_sat, y_sat = _decimate(t_sat, _norm(np.array(c.sat_areas_m2)), CURVE_MAX_POINTS)
        ax_sat.plot(t_sat, y_sat, color="black", linewidth=1.2, label="Satellite")
        ax_sat.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))
        ax_sat.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax_sat.xaxis.get_major_locator()))
        ax_sat.set_xlabel("Satellite time")
        if c.sat_end is not None and not pd.isna(c.sat_end):
            ax_sat.axvline(c.sat_end, color="black", linestyle="--", linewidth=1.0)
        if c.sat_target_m2 is not None and np.isfinite(c.sat_target_m2) and c.sat_areas_m2:
            den = float(np.nanmax(c.sat_areas_m2))
            if den > 0:
                ax_sim.axhline(c.sat_target_m2 / den, color="black", linestyle="--", linewidth=1.0)

    h1, l1 = ax_sim.get_legend_handles_labels()
    h2, l2 = ax_sat.get_legend_handles_labels()
    if h1 or h2:
        ax_sim.legend(h1 + h2, l1 + l2, fontsize=8, loc="lower right")

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def _error_page(case_id: str, msg: str) -> plt.Figure:
    fig = plt.figure(figsize=(11.69, 8.27))
    ax  = fig.add_subplot(111)
    ax.axis("off")
    ax.text(0.02, 0.98, f"CASE: {case_id}\n\nERROR:\n{msg}",
            va="top", ha="left", family="monospace", fontsize=12)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Summary pages (scatter + histogram)
# ---------------------------------------------------------------------------

_METRICS_INFO = [
    ("jaccard",  "Jaccard Index"),
    ("sorensen", "Sørensen Coeff."),
    ("kappa",    "Cohen's κ"),
]


def _collect_model_arrays(cases: list[Case]) -> dict[str, dict[str, np.ndarray]]:
    """Build per-model numpy arrays of x-variables and similarity metrics."""
    out: dict[str, dict[str, np.ndarray]] = {}
    for mc in MODELS:
        prefix = mc.label.lower()
        rows = [
            (
                c.obs_true_m2 * M2_TO_ACRES if c.obs_true_m2 else np.nan,
                c.tstop_h if c.tstop_h is not None else np.nan,
                c.avg_wind_kph if c.avg_wind_kph is not None else np.nan,
                float(c.metrics.get(f"{prefix}_jaccard",  np.nan)),
                float(c.metrics.get(f"{prefix}_sorensen", np.nan)),
                float(c.metrics.get(f"{prefix}_kappa",    np.nan)),
            )
            for c in cases if c.model_toas.get(mc.label)
        ]
        if rows:
            arr = np.array(rows, dtype=float)
        else:
            arr = np.empty((0, 6))
        out[mc.label] = {
            "obs_acres":    arr[:, 0] if arr.size else np.array([]),
            "tstop_h":      arr[:, 1] if arr.size else np.array([]),
            "avg_wind_kph": arr[:, 2] if arr.size else np.array([]),
            "jaccard":      arr[:, 3] if arr.size else np.array([]),
            "sorensen":     arr[:, 4] if arr.size else np.array([]),
            "kappa":        arr[:, 5] if arr.size else np.array([]),
        }
    return out


def _add_bestfit(ax, x: np.ndarray, y: np.ndarray, color, use_log: bool = False,
                 h_anchor: str = "left", y_pos: float = 0.04) -> None:
    """Overlay a linear best-fit line and annotate with R² and equation.

    Parameters
    ----------
    h_anchor : str
        Horizontal side for the annotation box: ``"left"`` or ``"right"``.
    y_pos : float
        Vertical position (axes fraction, bottom edge) for the annotation box.
        Pass increasing values to stack multiple model annotations.
    """
    if x.size < 3:
        return
    x_fit = np.log10(x) if use_log else x
    try:
        coeffs = np.polyfit(x_fit, y, 1)
    except (np.linalg.LinAlgError, ValueError):
        return
    slope, intercept = coeffs
    r2 = np.corrcoef(x_fit, y)[0, 1] ** 2

    x_sorted = np.sort(x)
    x_fit_sorted = np.log10(x_sorted) if use_log else x_sorted
    y_line = slope * x_fit_sorted + intercept

    ax.plot(x_sorted, y_line, color=color, linewidth=1.2, linestyle="--",
            alpha=0.85, zorder=4)

    if use_log:
        eq_str = f"y={slope:+.3f}·log₁₀(x){intercept:+.3f}"
    else:
        eq_str = f"y={slope:+.4f}·x{intercept:+.3f}"
    label = f"R²={r2:.3f}  {eq_str}"

    x_pos = 0.02 if h_anchor == "left" else 0.98
    ha    = "left" if h_anchor == "left" else "right"
    ax.text(x_pos, y_pos, label, transform=ax.transAxes,
            fontsize=6.5, color=color, ha=ha, va="bottom",
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color,
                      alpha=0.7, linewidth=0.6), zorder=5)


def _summary_scatter(model_arrays: dict[str, dict[str, np.ndarray]]) -> plt.Figure:
    """3×2 grid: rows = metrics, cols = [burn size, duration]."""
    x_configs = [
        ("obs_acres", "Observed Burn Area (acres)", True),   # log x
        ("tstop_h",   "Fire Duration (hours)",      True),   # log x
    ]

    fig, axes = plt.subplots(3, 2, figsize=(11.69, 8.27), squeeze=False)
    fig.suptitle("Similarity Scores vs. Fire Characteristics",
                 fontsize=13, fontweight="bold", y=0.99)

    for col, (x_key, x_label, use_log) in enumerate(x_configs):
        for row, (m_key, m_label) in enumerate(_METRICS_INFO):
            ax = axes[row, col]
            ax.set_ylim(-0.05, 1.05)
            ax.grid(True, alpha=0.3, linewidth=0.5)
            if col == 0:
                ax.set_ylabel(m_label)
            if row == len(_METRICS_INFO) - 1:
                ax.set_xlabel(x_label)
            if row == 0:
                ax.set_title(x_label.split(" (")[0])
            if use_log:
                ax.set_xscale("log")

            # Stack all fit annotations on the left, one above the other, so
            # none of them can fall behind the legend which sits at lower right.
            ANN_STEP = 0.12   # axes-fraction gap between stacked boxes
            ann_y    = 0.04
            for mc in MODELS:
                x = model_arrays[mc.label][x_key]
                y = model_arrays[mc.label][m_key]
                ok = np.isfinite(x) & np.isfinite(y)
                if ok.any():
                    ax.scatter(x[ok], y[ok], color=mc.color, alpha=0.65, s=18,
                               label=f"{mc.label} (n={ok.sum()})", zorder=3)
                    _add_bestfit(ax, x[ok], y[ok], mc.color, use_log=use_log,
                                 h_anchor="left", y_pos=ann_y)
                    ann_y += ANN_STEP

            if row == 0 and col == 0:
                ax.legend(fontsize=7, loc="lower right")

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def _summary_histograms(model_arrays: dict[str, dict[str, np.ndarray]]) -> plt.Figure:
    """N_models × 3 grid of histograms: rows = models, cols = metrics."""
    n_models  = len(MODELS)
    n_metrics = len(_METRICS_INFO)

    fig, axes = plt.subplots(n_models, n_metrics, figsize=(11.69, 8.27), squeeze=False)
    fig.suptitle("Similarity Score Distributions",
                 fontsize=13, fontweight="bold", y=0.99)

    for row, mc in enumerate(MODELS):
        for col, (m_key, m_label) in enumerate(_METRICS_INFO):
            ax   = axes[row, col]
            vals = model_arrays[mc.label][m_key]
            vals = vals[np.isfinite(vals)]

            ax.set_xlim(-0.05, 1.05)
            ax.grid(True, alpha=0.3, axis="y")
            if row == n_models - 1:
                ax.set_xlabel("Score")
            if row == 0:
                ax.set_title(m_label)
            if col == 0:
                ax.set_ylabel(f"{mc.label}\nCount")

            if vals.size:
                n_bins = min(25, max(5, vals.size // 3))
                ax.hist(vals, bins=n_bins, range=(0, 1),
                        color=mc.color, alpha=0.75, edgecolor="white", linewidth=0.5)
                mean_v = float(np.mean(vals))
                med_v  = float(np.median(vals))
                ax.axvline(mean_v, color="black",   linestyle="--", linewidth=1.2,
                           label=f"mean={mean_v:.3f}")
                ax.axvline(med_v,  color="dimgray", linestyle=":",  linewidth=1.0,
                           label=f"med={med_v:.3f}")
                ax.legend(fontsize=7, loc="upper left")
                ax.text(0.98, 0.95, f"n={vals.size}", transform=ax.transAxes,
                        ha="right", va="top", fontsize=8)
            else:
                ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                        ha="center", va="center", fontsize=9, color="gray")

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def _summary_wind_scatter(model_arrays: dict[str, dict[str, np.ndarray]]) -> plt.Figure:
    """Grid of similarity metrics vs. average simulation wind speed, one column per model."""
    n_metrics = len(_METRICS_INFO)
    n_models  = len(MODELS)

    fig, axes = plt.subplots(n_metrics, n_models, figsize=(11.69, 8.27), squeeze=False)
    fig.suptitle("Similarity Scores vs. Average Simulation Wind Speed (kph)",
                 fontsize=13, fontweight="bold", y=0.99)

    for col, mc in enumerate(MODELS):
        x = model_arrays[mc.label]["avg_wind_kph"]
        for row, (m_key, m_label) in enumerate(_METRICS_INFO):
            ax = axes[row, col]
            ax.set_ylim(-0.05, 1.05)
            ax.grid(True, alpha=0.3, linewidth=0.5)
            if col == 0:
                ax.set_ylabel(m_label)
            if row == 0:
                ax.set_title(mc.label)
            if row == n_metrics - 1:
                ax.set_xlabel("Avg Wind Speed (kph)")

            y  = model_arrays[mc.label][m_key]
            ok = np.isfinite(x) & np.isfinite(y)
            if ok.any():
                ax.scatter(x[ok], y[ok], color=mc.color, alpha=0.65, s=18, zorder=3)
                _add_bestfit(ax, x[ok], y[ok], mc.color)
                ax.text(0.98, 0.95, f"n={ok.sum()}", transform=ax.transAxes,
                        ha="right", va="top", fontsize=8)
            else:
                ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                        ha="center", va="center", fontsize=9, color="gray")

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def _summary_area_bias(cases: list[Case]) -> plt.Figure:
    """One subplot per model: horizontal bars of log₂(sim/obs) ratio per case.

    Only the N most extreme over- and under-estimating cases are labelled.
    All model subplots share the same x-axis limits.
    """
    N_LABEL   = 7    # (kept for potential future use)

    n_models  = len(MODELS)

    # ---- first pass: collect data and compute shared x-limits -----------
    model_data: dict[str, tuple[list[str], np.ndarray]] = {}
    all_log2: list[float] = []

    for mc in MODELS:
        prefix = mc.label.lower()
        rows: list[tuple[str, float]] = []
        for c in cases:
            if not c.model_toas.get(mc.label):
                continue
            ratio = float(c.metrics.get(f"{prefix}_ratio_of_areas", np.nan))
            if not np.isfinite(ratio) or ratio <= 0:
                continue
            rows.append(((c.fire_name or c.case_id)[:24], ratio))
        if rows:
            rows.sort(key=lambda r: r[1])
            lbls, rats = zip(*rows)
            log2_vals = np.log2(np.array(rats, dtype=float))
            model_data[mc.label] = (list(lbls), log2_vals)
            all_log2.extend(log2_vals.tolist())

    # Shared x-axis: pad 20 % beyond each side independently so the range
    # reflects how far overestimates and underestimates actually reach.
    # Always show at least ±log2(4)=2 (i.e. 4× / 1/4×) on both sides.
    if all_log2:
        pos_vals  = [v for v in all_log2 if v > 0] or [0.0]
        neg_vals  = [v for v in all_log2 if v < 0] or [0.0]
        x_pos_max = max(2.0, max(pos_vals) * 1.20)
        x_neg_min = min(-2.0, min(neg_vals) * 1.20)
        shared_xlim = (x_neg_min, x_pos_max)
    else:
        shared_xlim = (-3, 3)

    # ---- figure ----------------------------------------------------------
    fig, axes = plt.subplots(1, n_models, figsize=(11.69, 8.27), squeeze=False)
    fig.suptitle(
        "Area Estimation Bias per Case   "
        "(log₂ scale:  0 = perfect,  +1 = 2× over,  −1 = 2× under)",
        fontsize=11, fontweight="bold", y=0.99,
    )

    for col, mc in enumerate(MODELS):
        ax = axes[0, col]

        if mc.label not in model_data:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                    ha="center", va="center", fontsize=9, color="gray")
            ax.set_title(mc.label)
            ax.set_xlim(shared_xlim)
            continue

        labels, log2_vals = model_data[mc.label]
        ratios  = 2.0 ** log2_vals
        n       = len(labels)
        y_pos   = np.arange(n)

        # ---- bars --------------------------------------------------------
        over_color  = (*mc.color, 0.75)
        under_color = (0.50, 0.50, 0.50, 0.65)
        bar_colors  = [over_color if v >= 0 else under_color for v in log2_vals]

        ax.barh(y_pos, log2_vals, color=bar_colors,
                edgecolor="none", linewidth=0, height=0.85)

        # ---- reference lines (one per integer log2 step in the range) ------
        ax.axvline(0, color="black", linewidth=1.0, zorder=5)
        neg_ref = int(np.ceil(abs(shared_xlim[0])))
        pos_ref = int(np.ceil(shared_xlim[1]))
        for k in range(1, max(neg_ref, pos_ref) + 1):
            for sign in (+1, -1):
                v = sign * k
                if shared_xlim[0] <= v <= shared_xlim[1]:
                    ax.axvline(v, color="gray", linestyle=":",
                               linewidth=0.7, alpha=0.55, zorder=3)

        ax.set_xlim(shared_xlim)
        ax.set_xlabel("log₂(simulated / observed area)")
        ax.set_title(mc.label)
        ax.grid(True, axis="x", alpha=0.25, linewidth=0.5)
        ax.tick_params(axis="y", length=0)   # hide tick marks

        # ---- y-tick labels: none (bars are too numerous to label usefully)
        ax.set_yticks(y_pos)
        ax.set_yticklabels([""] * n)

        # ---- stats box ---------------------------------------------------
        n_over    = int((log2_vals > 0).sum())
        med_ratio = float(np.median(ratios))
        stats_txt = (
            f"n = {n}\n"
            f"median ratio = {med_ratio:.2f}×\n"
            f"overestimate: {n_over}/{n} ({100*n_over/n:.0f}%)"
        )
        ax.text(0.98, 0.01, stats_txt, transform=ax.transAxes,
                fontsize=7, ha="right", va="bottom",
                bbox=dict(boxstyle="round,pad=0.3", fc="white",
                          ec="gray", alpha=0.8, linewidth=0.6))

        # ---- legend ------------------------------------------------------
        legend_handles = [
            Patch(color=mc.color, alpha=0.80, label="Overestimate  (ratio > 1)"),
            Patch(color=(0.50, 0.50, 0.50), alpha=0.75, label="Underestimate  (ratio < 1)"),
        ]
        ax.legend(handles=legend_handles, fontsize=7, loc="upper left")

        # ---- x-axis reference tick labels --------------------------------
        # Build ticks to cover the actual (asymmetric) range: powers of 2 up
        # to the nearest integer log2 on each side.
        neg_steps = int(np.ceil(abs(shared_xlim[0])))   # e.g. 3 → 1/2×, 1/4×, 1/8×
        pos_steps = int(np.ceil(shared_xlim[1]))         # e.g. 2 → 2×, 4×
        xtick_set: set[int] = {0}
        for k in range(1, pos_steps + 1):
            xtick_set.add(k)
        for k in range(1, neg_steps + 1):
            xtick_set.add(-k)
        xticks = sorted(xtick_set)
        ax.set_xticks(xticks)
        xlabels = []
        for v in xticks:
            if v == 0:
                xlabels.append("1×")
            elif v > 0:
                xlabels.append(f"{2**v:.0f}×")
            else:
                xlabels.append(f"1/{2**(-v):.0f}×")
        ax.set_xticklabels(xlabels, fontsize=9)
        ax.set_xlabel("simulated / observed area")

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return fig


def _summary_pages(cases: list[Case]) -> list[plt.Figure]:
    arrays = _collect_model_arrays(cases)
    return [
        _summary_wind_scatter(arrays),
        _summary_scatter(arrays),
        _summary_area_bias(cases),
        _summary_histograms(arrays),
    ]


# ---------------------------------------------------------------------------
# Case classification
# ---------------------------------------------------------------------------

def _case_status(c: Case) -> str:
    """Return 'success' or 'non_ignited'.

    A case is non-ignited when every model with results has Jaccard < NON_IGNITION_JACCARD,
    indicating the simulation burned little or nothing.
    """
    jaccards = [
        float(c.metrics.get(f"{mc.label.lower()}_jaccard", np.nan))
        for mc in MODELS if c.model_toas.get(mc.label)
    ]
    valid = [j for j in jaccards if np.isfinite(j)]
    if not valid or max(valid) < NON_IGNITION_JACCARD:
        return "non_ignited"
    return "success"


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------

def _cover_page(
    results:     list[tuple],
    successful:  list[Case],
    non_ignited: list[Case],
) -> plt.Figure:
    n_total   = len(results)
    n_failed  = sum(1 for _, c, _ in results if c is None)
    n_non_ign = len(non_ignited)
    n_success = len(successful)

    fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle("Validation Report — Summary", fontsize=14, fontweight="bold", y=0.99)

    n_cols = max(len(MODELS), 1)
    gs = fig.add_gridspec(
        3, n_cols,
        height_ratios=[0.10, 0.45, 0.45],
        hspace=0.45, wspace=0.12,
        left=0.04, right=0.98, top=0.93, bottom=0.02,
    )

    # ── Stats row ────────────────────────────────────────────────────────────
    ax_stats = fig.add_subplot(gs[0, :])
    ax_stats.axis("off")
    stats_lines = (
        f"Total cases: {n_total}     "
        f"Successful: {n_success}     "
        f"Non-ignited (Jaccard < {NON_IGNITION_JACCARD:.2f}): {n_non_ign}     "
        f"Failed (no outputs): {n_failed}"
    )
    ax_stats.text(0.5, 0.5, stats_lines, ha="center", va="center",
                  fontsize=10, family="monospace", transform=ax_stats.transAxes,
                  bbox=dict(boxstyle="round,pad=0.4", facecolor="#e8e8e8", linewidth=0))

    # ── Per-model top / bottom 10 tables ─────────────────────────────────────
    col_labels = ["Case", "Fire", "Jaccard", "Sørensen", "κ"]

    for col, mc in enumerate(MODELS):
        prefix = mc.label.lower()
        scored = sorted(
            (
                (c, float(c.metrics.get(f"{prefix}_jaccard", np.nan)))
                for c in successful
                if np.isfinite(float(c.metrics.get(f"{prefix}_jaccard", np.nan)))
            ),
            key=lambda t: t[1],
            reverse=True,          # best first
        )

        subsets = [
            (scored[:10],               "Top 10 by Jaccard",    False),
            (list(reversed(scored[-10:])), "Bottom 10 by Jaccard", True),
        ]

        for row_idx, (subset, title, is_bottom) in enumerate(subsets, start=1):
            ax = fig.add_subplot(gs[row_idx, col])
            ax.axis("off")
            ax.set_title(f"{mc.label} — {title}",
                         fontsize=9, fontweight="bold", color=mc.color, pad=3)

            if not subset:
                ax.text(0.5, 0.5, "no data", ha="center", va="center",
                        fontsize=9, color="gray", transform=ax.transAxes)
                continue

            rows_data = [
                [
                    c.case_id,
                    (c.fire_name or "")[:16],
                    f"{c.metrics.get(f'{prefix}_jaccard',  np.nan):.4f}",
                    f"{c.metrics.get(f'{prefix}_sorensen', np.nan):.4f}",
                    f"{c.metrics.get(f'{prefix}_kappa',    np.nan):.4f}",
                ]
                for c, _ in subset
            ]

            tbl = ax.table(
                cellText=rows_data,
                colLabels=col_labels,
                loc="center",
                cellLoc="center",
                bbox=[0, 0, 1, 1],
            )
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(7.5)

            hdr_color  = "#b0b0b0"
            even_color = "#f0f0f0"
            for j in range(len(col_labels)):
                tbl[(0, j)].set_facecolor(hdr_color)
                tbl[(0, j)].set_text_props(fontweight="bold")
            for i in range(1, len(rows_data) + 1):
                for j in range(len(col_labels)):
                    tbl[(i, j)].set_facecolor(even_color if i % 2 == 0 else "white")

    return fig


# ---------------------------------------------------------------------------
# Per-case processing
# ---------------------------------------------------------------------------

def process_case(case_dir: Path) -> Case:
    case_id = case_dir.name

    if not (case_dir / FIRESCAR_NAME).exists():
        raise FileNotFoundError(f"Missing {FIRESCAR_NAME}")

    # Load metadata once; share with helpers to avoid redundant reads.
    try:
        meta = read_case_metadata(case_dir)
    except Exception:
        meta = {}

    fire_name, fire_year = _fire_name_year(case_dir, meta)
    tstop_h, tstop_src   = _tstop_hours(case_dir)

    obs_src = _firescar_union(case_dir / FIRESCAR_NAME)
    ign_src = _read_ignition(case_dir)

    # Resolve TOA paths for every model
    model_toas = {mc.label: mc.find_toa(case_dir) for mc in MODELS}

    # Use the first available TOA as the spatial reference
    base_mc = next((mc for mc in MODELS if model_toas.get(mc.label)), None)
    if base_mc is None:
        raise FileNotFoundError("No model TOA files found for this case")

    metrics: dict[str, Any] = {
        "case":            case_id,
        "burn_threshold":  BURN_THRESHOLD,
    }

    # toa_arrays: cache raw float64 arrays and pixel areas to avoid re-opening
    # rasters in _case_page for the area-evolution curves.
    toa_arrays: dict[str, tuple[np.ndarray, float]] = {}

    with rasterio.open(model_toas[base_mc.label]) as base_ds:
        _require_projected(base_ds, f"{base_mc.label} TOA")

        if obs_src.crs is None:
            obs_src = obs_src.set_crs("EPSG:4326")
        obs_geom_base = obs_src.to_crs(base_ds.crs).iloc[0]
        try:
            obs_geom_base = obs_geom_base.buffer(0)
        except Exception:
            pass

        if ign_src.crs is None:
            ign_src = ign_src.set_crs("EPSG:4326")
        ign_base = ign_src.to_crs(base_ds.crs) if not ign_src.empty \
                   else gpd.GeoDataFrame(geometry=[], crs=base_ds.crs)

        obs_true_m2 = float(
            gpd.GeoSeries([obs_geom_base], crs=base_ds.crs).to_crs(AREA_CRS).iloc[0].area
        )

        # Metrics for each model; also cache the TOA array for curve rendering.
        for mc in MODELS:
            toa = model_toas.get(mc.label)
            if not toa:
                continue
            try:
                prefix = mc.label.lower()
                if toa == model_toas[base_mc.label]:
                    raw = np.asarray(base_ds.read(mc.band, masked=True).filled(np.nan),
                                     dtype=np.float64)
                    px_m2 = _pixel_area_m2(base_ds)
                    toa_arrays[mc.label] = (raw, px_m2)
                    sim_mask  = np.isfinite(raw) & (raw >= BURN_THRESHOLD)
                    obs_mask_ = _obs_mask(base_ds, obs_geom_base)
                    metrics.update(_raster_metrics(obs_mask_, sim_mask, px_m2, prefix))
                else:
                    with rasterio.open(toa) as ds2:
                        eff_crs = _require_projected(ds2, f"{mc.label} TOA",
                                                     fallback_crs=base_ds.crs)
                        obs_geom2 = obs_src.to_crs(eff_crs).iloc[0]
                        try:
                            obs_geom2 = obs_geom2.buffer(0)
                        except Exception:
                            pass
                        raw   = np.asarray(ds2.read(mc.band, masked=True).filled(np.nan),
                                           dtype=np.float64)
                        px_m2 = _pixel_area_m2(ds2)
                        toa_arrays[mc.label] = (raw, px_m2)
                        sim_mask  = np.isfinite(raw) & (raw >= BURN_THRESHOLD)
                        obs_mask_ = _obs_mask(ds2, obs_geom2)
                        metrics.update(_raster_metrics(obs_mask_, sim_mask, px_m2, prefix))
            except Exception as e:
                log.warning("Case %s: %s metrics failed: %s", case_id, mc.label, e)

    avg_wind = _wxs_avg_wind_kph(case_dir, meta)
    if avg_wind is not None:
        metrics["avg_wind_kph"] = avg_wind

    c = Case(
        case_id       = case_id,
        fire_name     = fire_name,
        fire_year     = fire_year,
        model_toas    = model_toas,
        obs_geom_base = obs_geom_base,
        ign_base      = ign_base,
        tstop_h       = tstop_h,
        tstop_src     = tstop_src,
        obs_true_m2   = obs_true_m2,
        avg_wind_kph  = avg_wind,
        toa_arrays    = toa_arrays,
        metrics       = metrics,
    )

    if SAT_ENABLED:
        sat = _read_case_sat(case_dir)
        if sat is not None:
            sat_area     = sat.to_crs(AREA_CRS)
            burn_geom    = gpd.GeoSeries([obs_geom_base], crs=ign_base.crs).to_crs(AREA_CRS).iloc[0]
            pts          = sat_area.loc[sat_area.geometry.within(burn_geom)].copy()
            if not pts.empty:
                discovery = pd.Timestamp(pts["sat_dt"].min())
                c.discovery_dt = discovery
                end, times, areas, target = _sat_chain(
                    pts, burn_geom, discovery, SAT_BUFFER_M, SAT_CHAIN_MAX_GAP, SAT_COVERAGE_FRAC
                )
                c.sat_end, c.sat_times, c.sat_areas_m2, c.sat_target_m2 = end, times, areas, target
                c.metrics["satellite_end_time"]     = str(end) if not pd.isna(end) else ""
                c.metrics["satellite_final_area_m2"] = float(areas[-1]) if areas else np.nan

    return c


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def iter_cases(root: Path) -> list[Path]:
    return [d for d in sorted(root.iterdir()) if d.is_dir() and (d / FIRESCAR_NAME).exists()]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    case_dirs_list = iter_cases(ROOT_DIR)
    if not case_dirs_list:
        raise RuntimeError(f"No cases found under {ROOT_DIR}")

    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)

    # Pass 1: process all cases in parallel (no figures yet — we need all
    # results before writing summary pages).
    n_workers = min(16, os.cpu_count() or 4)
    log.info("Processing %d cases with %d workers …", len(case_dirs_list), n_workers)
    result_map: dict[Path, tuple[Case | None, Exception | None]] = {}
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        future_to_dir = {pool.submit(process_case, d): d for d in case_dirs_list}
        for future in as_completed(future_to_dir):
            d = future_to_dir[future]
            try:
                c = future.result()
                result_map[d] = (c, None)
                found = [mc.label for mc in MODELS if c.model_toas.get(mc.label)]
                missing = [mc.label for mc in MODELS if not c.model_toas.get(mc.label)]
                log.info(
                    "OK  %s  (%s %s)  models=[%s]%s  sat_pts=%d",
                    d.name, c.fire_name or "—", c.fire_year or "—",
                    ", ".join(found),
                    f"  missing=[{', '.join(missing)}]" if missing else "",
                    len(c.sat_times),
                )
            except Exception as e:
                log.warning("FAIL %s: %s", d.name, e)
                result_map[d] = (None, e)
    # Restore original sorted order for deterministic PDF output.
    results: list[tuple[Path, Case | None, Exception | None]] = [
        (d, *result_map[d]) for d in case_dirs_list
    ]

    good_cases  = [c for _, c, _ in results if c is not None]
    successful  = [c for c in good_cases if _case_status(c) == "success"]
    non_ignited = [c for c in good_cases if _case_status(c) == "non_ignited"]
    n_failed    = len(results) - len(good_cases)
    log.info(
        "%d / %d cases OK  —  %d successful, %d non-ignited, %d failed",
        len(good_cases), len(results), len(successful), len(non_ignited), n_failed,
    )

    # Pass 2: write PDF
    #   page 1        : cover (counts + top/bottom 10 tables)
    #   pages 2–4     : summary scatter / histograms (successful cases only)
    #   pages 5+      : per-case pages (all cases)
    rows: list[dict[str, Any]] = []
    with PdfPages(OUT_PDF) as pdf:
        pdf.savefig(_cover_page(results, successful, non_ignited))
        plt.close("all")

        if successful:
            for fig in _summary_pages(successful):
                pdf.savefig(fig)
                plt.close(fig)

        for d, c, err in results:
            if c is not None:
                pdf.savefig(_case_page(c, d))
                rows.append(c.metrics)
            else:
                pdf.savefig(_error_page(d.name, str(err)))
            plt.close("all")

    n_summary = 4 if successful else 0
    log.info("Wrote %s  (%d pages total)", OUT_PDF, 1 + n_summary + len(results))
    # pd.DataFrame(rows).to_csv(OUT_PDF.with_suffix(".csv"), index=False)


if __name__ == "__main__":
    main()
