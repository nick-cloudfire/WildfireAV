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

from dataclasses import dataclass, field
from pathlib import Path
import logging
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
        toa_rel  = "farsite/outputs/farsite_Arrival Time.tif",
        band     = 1,
        toa_unit = "min",
        color    = (0.0, 0.4, 1.0),
        alpha    = 0.35,
    ),
]

# ---------------------------------------------------------------------------
# Other user-configurable settings
# ---------------------------------------------------------------------------

ROOT_DIR         = Path(r"/home/nick/elmfire_validation/FirePairs")
OUT_PDF          = ROOT_DIR / "validation_report.pdf"

BURN_THRESHOLD   = 1.0
ALL_TOUCHED_OBS  = False
CURVE_MAX_POINTS = 300
MODEL_CURVE_BINS = 600

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


def _fire_name_year(case_dir: Path) -> tuple[str | None, int | None]:
    try:
        meta = read_case_metadata(case_dir)
        name = meta.get("perim_name") or meta.get("point_name")
        raw  = meta.get("perim_ignition")
        year = pd.to_datetime(raw, errors="coerce").year if raw else None
        return (str(name) if name else None), (int(year) if year and not pd.isna(year) else None)
    except Exception:
        return None, None


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

def _require_projected(ds: rasterio.io.DatasetReader, label: str) -> None:
    if ds.crs is None or getattr(ds.crs, "is_geographic", False):
        raise ValueError(
            f"{label}: CRS is {ds.crs}. Rasters must be in a projected (meter) CRS."
        )


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


def _reproject_mask(ds_src, ds_dst, mask_u8: np.ndarray) -> np.ndarray:
    dst = np.zeros((ds_dst.height, ds_dst.width), dtype=np.uint8)
    reproject(
        source=mask_u8, destination=dst,
        src_transform=ds_src.transform, src_crs=ds_src.crs,
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

    if base_toa:
        with rasterio.open(base_toa) as base_ds:
            _require_projected(base_ds, f"{base_mc.label} TOA")
            _plot_barrier(ax_map, case_dir / "inputs" / "barrier.tif", base_ds)

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
                            _require_projected(ds2, f"{mc.label} TOA")
                            raw_u8 = _burn_mask(ds2, mc.band, BURN_THRESHOLD).astype(np.uint8)
                            on_base = _reproject_mask(ds2, base_ds, raw_u8).astype(bool)
                            _plot_mask(ax_map, base_ds, on_base, mc.rgba)
                except Exception as e:
                    log.warning("Case %s: %s map overlay failed: %s", c.case_id, mc.label, e)

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

    # Map title: Jaccard scores per model
    j_parts = []
    for mc in MODELS:
        j = c.metrics.get(f"{mc.label.lower()}_jaccard", np.nan)
        if np.isfinite(j):
            j_parts.append(f"J({mc.label})={j:.3f}")
    ax_map.set_title("Burn masks" + ("  |  " + "  ".join(j_parts) if j_parts else ""))

    # Legend
    handles = [Line2D([0], [0], color="black", linewidth=2, label="Observed")]
    for mc in MODELS:
        if c.model_toas.get(mc.label):
            handles.append(Patch(facecolor=mc.color, alpha=mc.alpha, label=f"{mc.label} burn"))
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
        toa_path = c.model_toas.get(mc.label)
        if not toa_path:
            continue
        try:
            with rasterio.open(toa_path) as ds:
                toa_arr = np.asarray(ds.read(mc.band, masked=True).filled(np.nan), np.float64)
                t_vals, a_vals = _model_curve(toa_arr, BURN_THRESHOLD, _pixel_area_m2(ds), MODEL_CURVE_BINS)
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
                float(c.metrics.get(f"{prefix}_jaccard",  np.nan)),
                float(c.metrics.get(f"{prefix}_sorensen", np.nan)),
                float(c.metrics.get(f"{prefix}_kappa",    np.nan)),
            )
            for c in cases if c.model_toas.get(mc.label)
        ]
        if rows:
            arr = np.array(rows, dtype=float)
        else:
            arr = np.empty((0, 5))
        out[mc.label] = {
            "obs_acres": arr[:, 0] if arr.size else np.array([]),
            "tstop_h":   arr[:, 1] if arr.size else np.array([]),
            "jaccard":   arr[:, 2] if arr.size else np.array([]),
            "sorensen":  arr[:, 3] if arr.size else np.array([]),
            "kappa":     arr[:, 4] if arr.size else np.array([]),
        }
    return out


def _summary_scatter(model_arrays: dict[str, dict[str, np.ndarray]]) -> plt.Figure:
    """3×2 grid: rows = metrics, cols = [burn size, duration]."""
    x_configs = [
        ("obs_acres", "Observed Burn Area (acres)", True),   # log x
        ("tstop_h",   "Fire Duration (hours)",      False),
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

            for mc in MODELS:
                x = model_arrays[mc.label][x_key]
                y = model_arrays[mc.label][m_key]
                ok = np.isfinite(x) & np.isfinite(y)
                if ok.any():
                    ax.scatter(x[ok], y[ok], color=mc.color, alpha=0.65, s=18,
                               label=f"{mc.label} (n={ok.sum()})", zorder=3)

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


def _summary_pages(cases: list[Case]) -> list[plt.Figure]:
    arrays = _collect_model_arrays(cases)
    return [_summary_scatter(arrays), _summary_histograms(arrays)]


# ---------------------------------------------------------------------------
# Per-case processing
# ---------------------------------------------------------------------------

def process_case(case_dir: Path) -> Case:
    case_id = case_dir.name

    if not (case_dir / FIRESCAR_NAME).exists():
        raise FileNotFoundError(f"Missing {FIRESCAR_NAME}")

    fire_name, fire_year = _fire_name_year(case_dir)
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

        # Metrics for each model
        for mc in MODELS:
            toa = model_toas.get(mc.label)
            if not toa:
                continue
            try:
                prefix = mc.label.lower()
                if toa == model_toas[base_mc.label]:
                    sim_mask = _burn_mask(base_ds, mc.band, BURN_THRESHOLD)
                    obs_mask_ = _obs_mask(base_ds, obs_geom_base)
                    metrics.update(_raster_metrics(obs_mask_, sim_mask, _pixel_area_m2(base_ds), prefix))
                else:
                    with rasterio.open(toa) as ds2:
                        _require_projected(ds2, f"{mc.label} TOA")
                        obs_geom2 = obs_src.to_crs(ds2.crs).iloc[0]
                        try:
                            obs_geom2 = obs_geom2.buffer(0)
                        except Exception:
                            pass
                        sim_mask  = _burn_mask(ds2, mc.band, BURN_THRESHOLD)
                        obs_mask_ = _obs_mask(ds2, obs_geom2)
                        metrics.update(_raster_metrics(obs_mask_, sim_mask, _pixel_area_m2(ds2), prefix))
            except Exception as e:
                log.warning("Case %s: %s metrics failed: %s", case_id, mc.label, e)

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

    # Pass 1: process all cases (no figures yet — we need all results before
    # writing summary pages)
    log.info("Processing %d cases …", len(case_dirs_list))
    results: list[tuple[Path, Case | None, Exception | None]] = []
    for d in case_dirs_list:
        try:
            c = process_case(d)
            results.append((d, c, None))
            log.info("OK  %s  (%s %s)  sat_pts=%d",
                     d.name, c.fire_name or "—", c.fire_year or "—", len(c.sat_times))
        except Exception as e:
            log.warning("FAIL %s: %s", d.name, e)
            results.append((d, None, e))

    good_cases = [c for _, c, _ in results if c is not None]
    log.info("%d / %d cases OK", len(good_cases), len(results))

    # Pass 2: write PDF — summary pages first, then per-case pages
    rows: list[dict[str, Any]] = []
    with PdfPages(OUT_PDF) as pdf:
        if good_cases:
            for fig in _summary_pages(good_cases):
                pdf.savefig(fig)
                plt.close(fig)

        for d, c, err in results:
            if c is not None:
                pdf.savefig(_case_page(c, d))
                rows.append(c.metrics)
            else:
                pdf.savefig(_error_page(d.name, str(err)))
            plt.close("all")

    log.info("Wrote %s  (%d pages total)", OUT_PDF, 2 + len(results))
    # pd.DataFrame(rows).to_csv(OUT_PDF.with_suffix(".csv"), index=False)


if __name__ == "__main__":
    main()
