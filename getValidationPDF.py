# -*- coding: utf-8 -*-
"""
ONE multi-page PDF: ELMFIRE vs FARSITE (one page per case folder)

User-specified paths:
- ROOT_DIR = FIRE_ROOT
- ELMFIRE TOA:  <case>/outputs/time_of_arrival_*.tif (newest)
- FARSITE  TOA: <case>/farsite/outputs/farsite_Arrival Time.tif
- .data:        <case>/<case>.data
- Satellite points: per-case gpkg (from pipelineConfig if present)

Key fixes vs your previous run:
- FARSITE path is exact + always processed (no “candidates” guessing)
- TOA burn masks treat nodata explicitly
- Satellite read is per-case (no master CSV); discovery_dt = earliest in-burn hotspot
- Area evolution normalization uses true observed area (equal-area CRS) + final model areas + final satellite area
- If rasters are in geographic CRS, areas/curves will be wrong -> raises with clear message
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

log = logging.getLogger("validation")

mpl.rcParams["pdf.fonttype"] = 42
mpl.rcParams["ps.fonttype"] = 42
mpl.rcParams["font.family"] = "DejaVu Sans Mono"

M2_TO_ACRES = 0.000247105381

# -------------------------
# USER INPUTS (as you specified)
# -------------------------
ROOT_DIR = Path(r"/home/nick/elmfire_validation/FirePairs")  # <-- set FIRE_ROOT here
OUT_PDF = ROOT_DIR / "elmfire_vs_farsite_validation_report.pdf"

ELM_OUTPUTS_SUBDIR = "outputs"
ELM_TOA_GLOB = "time_of_arrival_*.tif"
ELM_BAND = 1

FARSITE_TOA_REL = Path("farsite/outputs/farsite_Arrival Time.tif")
FAR_BAND = 1

BURN_THRESHOLD = 1.0
ALL_TOUCHED_OBS = False

CURVE_MAX_POINTS = 300
MODEL_CURVE_BINS = 600
TOA_TIME_UNIT = "s"

AREA_CRS = "EPSG:5070"  # for true areas + normalization

import pipelineConfig as PC  # type: ignore

FIRESCAR_NAME = getattr(PC, "BURN_SHAPE_NAME", "firescar.gpkg")
IGNITION_NAME = getattr(PC, "IGNITION_POINT_SHP_NAME", "ignition_point.gpkg")

SAT_ENABLED = True
SAT_GPKG_NAME = getattr(PC, "CASE_SAT_GPKG_NAME", "satellite_points.gpkg")
SAT_LAYER = "points_in_burn"
SAT_DATE_COL = getattr(PC, "SAT_DATE_COL", "ACQ_DATE")
SAT_TIME_COL = getattr(PC, "SAT_TIME_COL", "ACQ_TIME")

SAT_CHAIN_MAX_GAP = pd.Timedelta(days=float(getattr(PC, "SAT_CHAIN_MAX_GAP_DAYS", 7)))
SAT_BUFFER_M = float(getattr(PC, "SAT_HOTSPOT_BUFFER_DIST", 200))
SAT_COVERAGE_FRAC = float(getattr(PC, "COVERAGE_FRACTION", 0.9))



@dataclass
class Case:
    case_id: str
    elm_toa: Path
    far_toa: Path | None
    obs_geom_base: Any
    ign_base: gpd.GeoDataFrame
    tstop_h: float | None
    tstop_src: str | None

    obs_true_m2: float | None = None  # observed polygon area in AREA_CRS
    discovery_dt: pd.Timestamp | None = None
    sat_times: list[pd.Timestamp] = field(default_factory=list)
    sat_areas_m2: list[float] = field(default_factory=list)
    sat_end: pd.Timestamp | pd.NaT = pd.NaT
    sat_target_m2: float | None = None

    metrics: dict[str, Any] = field(default_factory=dict)


# -------------------------
# File helpers (minimal + deterministic)
# -------------------------
def newest_glob(parent: Path, pattern: str) -> Path:
    hits = sorted(parent.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not hits:
        raise FileNotFoundError(f"No match: {parent}/{pattern}")
    return hits[-1]


def tstop_from_case_data(case_dir: Path) -> tuple[float | None, str | None]:
    data = case_dir / f"{case_dir.name}.data"  # exact as you specified
    if not data.exists():
        return None, None
    txt = data.read_text(errors="ignore")
    m = re.search(r"SIMULATION_TSTOP\s*=\s*([0-9]+(?:\.[0-9]+)?)", txt)
    return ((float(m.group(1)) / 3600.0) if m else None), str(data)

def rel_or_name(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except Exception:
        return p.name


# -------------------------
# Geometry
# -------------------------
def read_firescar_union(path: Path) -> gpd.GeoSeries:
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"No geometries in {path}")
    geom = gdf.geometry.union_all()
    try:
        geom = geom.buffer(0)
    except Exception:
        pass
    return gpd.GeoSeries([geom], crs=gdf.crs)


def read_ignition(case_dir: Path) -> gpd.GeoDataFrame:
    p = case_dir / IGNITION_NAME
    if not p.exists():
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    gdf = gpd.read_file(p)
    if gdf.empty:
        return gpd.GeoDataFrame(geometry=[], crs=gdf.crs or "EPSG:4326")
    return gdf


# -------------------------
# Raster masks + metrics
# -------------------------
def require_projected(ds: rasterio.io.DatasetReader, label: str):
    if ds.crs is None:
        raise ValueError(f"{label} CRS is missing.")
    if getattr(ds.crs, "is_geographic", False):
        raise ValueError(
            f"{label} CRS is geographic ({ds.crs}). "
            f"Reproject TOA rasters to a projected CRS (meters) or areas/curves will be wrong."
        )


def pixel_area_m2(ds: rasterio.io.DatasetReader) -> float:
    t = ds.transform
    return float(abs(t.a * t.e - t.b * t.d))  # determinant


def burn_mask(ds: rasterio.io.DatasetReader, band: int, threshold: float) -> np.ndarray:
    arr = ds.read(band, masked=True)
    data = np.asarray(arr.filled(np.nan), dtype=np.float32)
    nod = ds.nodata
    if nod is not None:
        data = np.where(np.isclose(data, nod), np.nan, data)
    return np.isfinite(data) & (data >= threshold)


def obs_mask(ds: rasterio.io.DatasetReader, geom) -> np.ndarray:
    m = rasterize(
        [(geom, 1)],
        out_shape=(ds.height, ds.width),
        transform=ds.transform,
        fill=0,
        all_touched=ALL_TOUCHED_OBS,
        dtype=np.uint8,
    )
    return m.astype(bool)


def safe_div(n: float, d: float) -> float:
    return 0.0 if d == 0 else n / d


def raster_metrics(obs: np.ndarray, sim: np.ndarray, px_m2: float, prefix: str) -> dict[str, Any]:
    obs = obs.astype(bool)
    sim = sim.astype(bool)
    tp = int(np.logical_and(obs, sim).sum())
    fp = int(np.logical_and(~obs, sim).sum())
    fn = int(np.logical_and(obs, ~sim).sum())
    tn = int(np.logical_and(~obs, ~sim).sum())
    obs_px = int(obs.sum())
    sim_px = int(sim.sum())
    union_px = tp + fp + fn

    obs_m2 = obs_px * px_m2
    sim_m2 = sim_px * px_m2

    j = safe_div(tp, union_px)
    s = safe_div(2 * tp, (obs_px + sim_px))

    total = tp + fp + fn + tn
    if total:
        pa = (tp + tn) / total
        pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (total**2)
        k = 0.0 if (1 - pe) == 0 else (pa - pe) / (1 - pe)
    else:
        k = 0.0

    return {
        f"{prefix}_observed_m2": float(obs_m2),
        f"{prefix}_simulated_m2": float(sim_m2),
        f"{prefix}_observed_acres": float(obs_m2 * M2_TO_ACRES),
        f"{prefix}_simulated_acres": float(sim_m2 * M2_TO_ACRES),
        f"{prefix}_jaccard": float(j),
        f"{prefix}_sorensen": float(s),
        f"{prefix}_kappa": float(k),
        f"{prefix}_ratio_of_areas": float(safe_div(sim_m2, obs_m2)),
        f"{prefix}_burn_px": int(sim_px),
    }


def model_curve(toa: np.ndarray, threshold: float, px_m2: float, bins: int) -> tuple[np.ndarray, np.ndarray]:
    v = toa[np.isfinite(toa) & (toa >= threshold)]
    if v.size == 0:
        return np.array([]), np.array([])
    vmin, vmax = float(v.min()), float(v.max())
    if vmax <= vmin:
        a = float(v.size) * px_m2
        return np.array([vmin, vmax]), np.array([a, a])
    counts, edges = np.histogram(v, bins=bins, range=(vmin, vmax))
    return edges[1:], np.cumsum(counts).astype(np.float64) * px_m2


def reproject_mask_to(ds_src, ds_dst, mask_u8: np.ndarray) -> np.ndarray:
    dst = np.zeros((ds_dst.height, ds_dst.width), dtype=np.uint8)
    reproject(
        source=mask_u8,
        destination=dst,
        src_transform=ds_src.transform,
        src_crs=ds_src.crs,
        dst_transform=ds_dst.transform,
        dst_crs=ds_dst.crs,
        resampling=Resampling.nearest,
        src_nodata=0,
        dst_nodata=0,
    )
    return dst


# -------------------------
# Satellite (per-case)
# -------------------------
def sat_datetime(df: pd.DataFrame, date_col: str, time_col: str) -> pd.Series:
    t = df[time_col].astype(str).str.replace(r"\D+", "", regex=True).str.zfill(4).str[:4]
    return pd.to_datetime(df[date_col].astype(str).str.strip() + " " + t, errors="coerce")


def read_case_sat(case_dir: Path) -> gpd.GeoDataFrame | None:
    p = case_dir / SAT_GPKG_NAME
    if not p.exists():
        return None
    gdf = gpd.read_file(p, layer=SAT_LAYER) if SAT_LAYER else gpd.read_file(p)
    if gdf.empty:
        return None

    # CRS auto:
    if gdf.crs is None:
        # very conservative auto-infer: if bounds look like lon/lat, assume EPSG:4326; else require user fix
        b = gdf.total_bounds  # minx, miny, maxx, maxy
        if (-180 <= b[0] <= 180) and (-180 <= b[2] <= 180) and (-90 <= b[1] <= 90) and (-90 <= b[3] <= 90):
            gdf = gdf.set_crs("EPSG:4326")
        else:
            raise ValueError(f"Satellite CRS missing and cannot infer (bounds={b}). Fix CRS in {p.name}.")

    gdf["sat_dt"] = sat_datetime(gdf, SAT_DATE_COL, SAT_TIME_COL)
    gdf = gdf.dropna(subset=["sat_dt"])
    return None if gdf.empty else gdf


def sat_chain(
    pts_area: gpd.GeoDataFrame,
    burn_area_geom,
    discovery_dt: pd.Timestamp,
    buffer_m: float,
    max_gap: pd.Timedelta,
    frac: float,
) -> tuple[pd.Timestamp | pd.NaT, list[pd.Timestamp], list[float], float | None]:
    pts = pts_area.loc[pts_area["sat_dt"] >= discovery_dt].sort_values("sat_dt")
    if pts.empty:
        return pd.NaT, [], [], None

    times: list[pd.Timestamp] = []
    areas: list[float] = []

    cum = None
    last = None
    for _, r in pts.iterrows():
        t = r["sat_dt"]
        if last is not None and (t - last) > max_gap:
            break
        g = r.geometry.buffer(buffer_m)
        cum = g if cum is None else cum.union(g)
        areas.append(float(cum.intersection(burn_area_geom).area))
        times.append(t)
        last = t

    burn_area = float(burn_area_geom.area)
    eff = min(burn_area, float(areas[-1]))
    target = float(frac * eff)

    end = pd.NaT
    for t, a in zip(times, areas):
        if a >= target:
            end = t
            break
    if pd.isna(end):
        end = times[-1]
    return end, times, areas, target


# -------------------------
# Report text + plotting
# -------------------------
def _fmt(v: Any, kind: str = "") -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, float) and not np.isfinite(v):
            return ""
    except Exception:
        pass
    try:
        if kind == "m2":
            return f"{float(v):,.0f}"
        if kind == "ac":
            return f"{float(v):,.2f}"
        if kind == "f4":
            return f"{float(v):.4f}"
        if kind == "r3":
            return f"{float(v):.3f}"
        return str(v)
    except Exception:
        return str(v)


def text_block(c: Case, case_dir: Path) -> str:
    m = c.metrics
    lines = [
        f"CASE: {c.case_id}",
        "",
        "FILES",
        f"ELMFIRE TOA: {rel_or_name(c.elm_toa, case_dir)}",
        f"FARSITE TOA: {rel_or_name(c.far_toa, case_dir)}",
        f"Threshold : {BURN_THRESHOLD}",
        f"Duration (TSTOP): {c.tstop_h:.2f} h" if c.tstop_h is not None else "Duration (TSTOP): (not found)",
    ]
    if c.tstop_src:
        lines.append(f"TSTOP source: {Path(c.tstop_src).name}")
    if c.obs_true_m2 is not None:
        lines.append(f"Observed area (vec, {AREA_CRS}): {_fmt(c.obs_true_m2,'m2')} m² ({_fmt(c.obs_true_m2*M2_TO_ACRES,'ac')} ac)")
    if c.discovery_dt is not None and not pd.isna(c.discovery_dt):
        lines.append(f"Discovery_dt (sat earliest in-burn): {c.discovery_dt}")

    lines += ["", "METRICS (raster-space, per-model grid)"]
    head = f"{'Metric':<14} | {'ELMFIRE':>10} | {'FARSITE':>10}"
    lines += [head, "-" * len(head)]
    rows = [
        ("Sim m²", "m2", "simulated_m2"),
        ("Obs m²", "m2", "observed_m2"),
        ("Jaccard", "f4", "jaccard"),
        ("Sorensen", "f4", "sorensen"),
        ("Kappa", "f4", "kappa"),
        ("Area ratio", "f3", "ratio_of_areas"),
        ("Burn px", "", "burn_px"),
    ]
    for lbl, kind, key in rows:
        e = _fmt(m.get(f"elm_{key}", np.nan), kind)
        f = _fmt(m.get(f"far_{key}", np.nan), kind) if c.far_toa else ""
        lines.append(f"{lbl:<14} | {e:>10} | {f:>10}")

    if SAT_ENABLED:
        lines += ["", "SATELLITE (per-case)"]
        lines.append(f"sat file: {SAT_GPKG_NAME}")
        lines.append(f"sat layer: {SAT_LAYER}")
        lines.append(f"sat points in chain: {len(c.sat_times)}")
        if c.sat_times:
            lines.append(f"sat final area m²: {_fmt(c.sat_areas_m2[-1],'m2')}")
            lines.append(f"sat end time: {c.sat_end}")
    return "\n".join(lines)


def plot_mask(ax, ds, mask: np.ndarray, rgba):
    if mask is None or not mask.any():
        return
    ext = plotting_extent(ds)
    r, g, b, a = rgba
    img = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.float32)
    m = mask.astype(np.float32)
    img[..., 0] = m * r
    img[..., 1] = m * g
    img[..., 2] = m * b
    img[..., 3] = m * a
    ax.imshow(img, extent=ext, origin="upper", interpolation="nearest")


def decimate_xy(x: np.ndarray, y: np.ndarray, max_points: int):
    if max_points <= 0 or len(x) <= max_points:
        return x, y
    idx = np.unique(np.linspace(0, len(x) - 1, max_points).round().astype(int))
    return x[idx], y[idx]


def case_page(c: Case, case_dir: Path) -> plt.Figure:
    """
    Page layout:
      - Left: map (Observed outline + ELMFIRE mask + FARSITE mask)
      - Right-top: text block
      - Right-bottom: evolution plot with TWO X axes:
          * bottom = simulation time (hours, from TOA)
          * top    = satellite datetime
      - Y axis: per-curve normalized (each curve goes 0..1)
    """

    fig = plt.figure(figsize=(11.69, 8.27))  # A4 landscape
    gs = fig.add_gridspec(2, 2, width_ratios=[1.45, 1.0], height_ratios=[1.0, 0.75])

    ax_map = fig.add_subplot(gs[:, 0])
    ax_txt = fig.add_subplot(gs[0, 1])
    ax_ts = fig.add_subplot(gs[1, 1])
    ax_txt.axis("off")

    elm_rgba = (1.0, 0.0, 0.0, 0.35)
    far_rgba = (0.0, 0.4, 1.0, 0.35)
    
    elm_color = elm_rgba[:3]
    far_color = far_rgba[:3]
    sat_color = "black"  # or choose something else

    # -----------------
    # MAP
    # -----------------
    ax_map.grid(True, alpha=0.2, linewidth=0.6)
    ax_map.set_aspect("equal", "box")

    with rasterio.open(c.elm_toa) as base:
        require_projected(base, "ELMFIRE TOA")
        # ---- Barrier background
        barrier_path = case_dir / "inputs" / "barrier.tif"
        plot_barrier_background(ax_map, barrier_path, base, alpha=0.15)
        elm_burn = burn_mask(base, ELM_BAND, BURN_THRESHOLD)
        plot_mask(ax_map, base, elm_burn, elm_rgba)

        if c.far_toa:
            try:
                with rasterio.open(c.far_toa) as far_ds:
                    require_projected(far_ds, "FARSITE TOA")
                    far_burn_u8 = burn_mask(far_ds, FAR_BAND, BURN_THRESHOLD).astype(np.uint8)
                    far_on_base = reproject_mask_to(far_ds, base, far_burn_u8).astype(bool)
                    plot_mask(ax_map, base, far_on_base, far_rgba)
            except Exception as e:
                log.warning("Case %s: FARSITE plot failed: %s", c.case_id, e)

        # Observed outline + ignition
        gpd.GeoSeries([c.obs_geom_base], crs=base.crs).plot(
            ax=ax_map, facecolor="none", edgecolor="black", linewidth=2
        )
        if c.ign_base is not None and not c.ign_base.empty:
            c.ign_base.plot(ax=ax_map, marker="x", markersize=60, zorder=5)

        # zoom to observed
        try:
            minx, miny, maxx, maxy = c.obs_geom_base.bounds
            pad = 0.1 * max(maxx - minx, maxy - miny)
            ax_map.set_xlim(minx - pad, maxx + pad)
            ax_map.set_ylim(miny - pad, maxy + pad)
        except Exception:
            pass

    ej = c.metrics.get("elm_jaccard", np.nan)
    fj = c.metrics.get("far_jaccard", np.nan)
    title = f"Case {c.case_id} — Burn masks"
    if np.isfinite(ej):
        title += f" | J(elm)={ej:.3f}"
    if c.far_toa and np.isfinite(fj):
        title += f" J(far)={fj:.3f}"
    ax_map.set_title(title)

    handles = [
        Line2D([0], [0], color="black", linewidth=2, label="Observed"),
        Patch(facecolor=elm_rgba[:3], alpha=elm_rgba[3], label="ELMFIRE burn"),
    ]
    if c.far_toa:
        handles.append(Patch(facecolor=far_rgba[:3], alpha=far_rgba[3], label="FARSITE burn"))
    if c.ign_base is not None and not c.ign_base.empty:
        handles.append(Line2D([0], [0], marker="x", linestyle="None", markersize=10, label="Ignition"))
    ax_map.legend(handles=handles, loc="upper right")

    # -----------------
    # TEXT
    # -----------------
    ax_txt.text(
        0.0, 1.0, text_block(c, case_dir),
        va="top", ha="left", fontsize=8.5, family="monospace"
    )

    # -----------------
    # EVOLUTION: two X axes + per-curve normalization
    #   bottom axis: simulation time (hours from TOA)
    #   top axis: satellite datetime
    # -----------------
    ax_sim = ax_ts
    ax_sat = ax_ts.twiny()

    ax_sim.grid(True, alpha=0.3, linewidth=0.6)
    ax_sim.set_title("Area evolution (each curve normalized to its own max)")
    ax_sim.set_ylabel("Area / max (per curve)")
    ax_sim.set_ylim(0.0, 1.05)
    ax_sim.set_xlabel("Simulation time (hours)")

    def _norm(y: np.ndarray) -> np.ndarray:
        y = np.asarray(y, dtype=np.float64)
        if y.size == 0:
            return y
        den = float(np.nanmax(y))
        return (y / den) if (np.isfinite(den) and den > 0) else y

    UNIT_TO_HOURS = {"s": 1/3600.0, "sec": 1/3600.0, "min": 1/60.0, "m": 1/60.0, "h": 1.0, "hr": 1.0}
    scale_h = UNIT_TO_HOURS.get(str(TOA_TIME_UNIT).lower(), 1/3600.0)

    # ---- ELM curve (simulation hours)
    t_elm = np.array([], dtype=np.float64)
    a_elm = np.array([], dtype=np.float64)
    with rasterio.open(c.elm_toa) as ds:
        toa = np.asarray(ds.read(ELM_BAND, masked=True).filled(np.nan), dtype=np.float64)
        t_elm, a_elm = model_curve(toa, BURN_THRESHOLD, pixel_area_m2(ds), MODEL_CURVE_BINS)
    if t_elm.size and a_elm.size:
        xh = t_elm * scale_h
        xh, y = decimate_xy(xh, _norm(a_elm), CURVE_MAX_POINTS)
        ax_sim.plot(xh, y, color=elm_color, linewidth=1.5, label="ELMFIRE (sim)")

    # ---- FARSITE curve (simulation hours)
    t_far = np.array([], dtype=np.float64)
    a_far = np.array([], dtype=np.float64)
    if c.far_toa:
        try:
            with rasterio.open(c.far_toa) as ds2:
                toa2 = np.asarray(ds2.read(FAR_BAND, masked=True).filled(np.nan), dtype=np.float64)
                toa2 = toa2 * 60
                t_far, a_far = model_curve(toa2, BURN_THRESHOLD, pixel_area_m2(ds2), MODEL_CURVE_BINS)
            if t_far.size and a_far.size:
                xh = t_far * scale_h
                xh, y = decimate_xy(xh, _norm(a_far), CURVE_MAX_POINTS)
                ax_sim.plot(xh, y, color=far_color, linewidth=1.5, label="FARSITE (sim)")
        except Exception as e:
            log.warning("Case %s: FARSITE curve failed: %s", c.case_id, e)

    # ---- Set sim x-limits: prefer tstop, else max of plotted sim curves
    xmax = 0.0
    try:
        if c.tstop_h is not None and np.isfinite(c.tstop_h) and c.tstop_h > 0:
            xmax = float(c.tstop_h)
        else:
            if t_elm.size:
                xmax = max(xmax, float(np.nanmax(t_elm * scale_h)))
            if t_far.size:
                xmax = max(xmax, float(np.nanmax(t_far * scale_h)))
        if xmax > 0:
            ax_sim.set_xlim(0.0, xmax)
    except Exception:
        pass

    # ---- Satellite curve (top axis, datetime)
    if c.sat_times and c.sat_areas_m2:
        t_sat = np.array(c.sat_times, dtype="datetime64[ns]")
        y_sat = _norm(np.array(c.sat_areas_m2, dtype=np.float64))
        t_sat, y_sat = decimate_xy(t_sat, y_sat, CURVE_MAX_POINTS)

        ax_sat.plot(t_sat, y_sat, color="black", linewidth=1.2, label="Satellite (datetime)")

        ax_sat.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))
        ax_sat.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax_sat.xaxis.get_major_locator()))
        ax_sat.set_xlabel("Satellite time")

        # optional markers
        if c.sat_end is not None and not pd.isna(c.sat_end):
            ax_sat.axvline(c.sat_end, color="black", linestyle="--", linewidth=1.0)
        if c.sat_target_m2 is not None and np.isfinite(c.sat_target_m2):
            den = float(np.nanmax(c.sat_areas_m2)) if c.sat_areas_m2 else 1.0
            if den > 0:
                ax_sim.axhline(float(c.sat_target_m2) / den, color="black", linestyle="--", linewidth=1.0)

    # ---- Legend: merge both axes
    h1, l1 = ax_sim.get_legend_handles_labels()
    h2, l2 = ax_sat.get_legend_handles_labels()
    if h1 or h2:
        ax_sim.legend(h1 + h2, l1 + l2, fontsize=8, loc="lower right")

    fig.tight_layout()
    return fig


def error_page(case_id: str, msg: str) -> plt.Figure:
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_subplot(111)
    ax.axis("off")
    ax.text(0.02, 0.98, f"CASE: {case_id}\n\nERROR:\n{msg}", va="top", ha="left", family="monospace", fontsize=12)
    fig.tight_layout()
    return fig

def plot_barrier_background(ax, barrier_path: Path, base_ds, alpha=0.10, gray=0.2):
    """
    Soft, uniform barrier background:
      - any nonzero barrier value is drawn identically
      - reprojected to base grid
      - uses explicit RGBA (avoids imshow normalization artifacts)
    """
    if not barrier_path.exists():
        return

    try:
        with rasterio.open(barrier_path) as src:
            src_data = src.read(1)

            dst = np.zeros((base_ds.height, base_ds.width), dtype=src_data.dtype)
            reproject(
                source=src_data,
                destination=dst,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=base_ds.transform,
                dst_crs=base_ds.crs,
                resampling=Resampling.nearest,
            )

        mask = (dst != 0)

        if not mask.any():
            return

        extent = plotting_extent(base_ds)

        # RGBA image: constant gray wherever mask is True
        img = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.float32)
        m = mask.astype(np.float32)
        img[..., 0] = m * gray
        img[..., 1] = m * gray
        img[..., 2] = m * gray
        img[..., 3] = m * alpha

        ax.imshow(img, extent=extent, origin="upper", interpolation="nearest", zorder=0)

    except Exception as e:
        log.warning("Barrier background failed: %s", e)


# -------------------------
# Per-case processing
# -------------------------
def process_case(case_dir: Path) -> Case:
    case_id = case_dir.name

    firescar = case_dir / FIRESCAR_NAME
    if not firescar.exists():
        raise FileNotFoundError(f"Missing {FIRESCAR_NAME}")

    outputs = case_dir / ELM_OUTPUTS_SUBDIR
    if not outputs.exists():
        raise FileNotFoundError("Missing outputs/")

    elm_toa = newest_glob(outputs, ELM_TOA_GLOB)

    far_toa = case_dir / FARSITE_TOA_REL
    far_toa = far_toa if far_toa.exists() else None

    obs_src = read_firescar_union(firescar)
    ign_src = read_ignition(case_dir)
    tstop_h, tstop_src = tstop_from_case_data(case_dir)

    metrics = {"case": case_id, "burn_threshold": BURN_THRESHOLD, "elm_toa": str(elm_toa), "far_toa": str(far_toa) if far_toa else ""}

    with rasterio.open(elm_toa) as base:
        require_projected(base, "ELMFIRE TOA")

        if obs_src.crs is None:
            obs_src = obs_src.set_crs("EPSG:4326")
        obs_geom_base = obs_src.to_crs(base.crs).iloc[0]
        try:
            obs_geom_base = obs_geom_base.buffer(0)
        except Exception:
            pass

        if ign_src.crs is None:
            ign_src = ign_src.set_crs("EPSG:4326")
        ign_base = ign_src.to_crs(base.crs) if not ign_src.empty else gpd.GeoDataFrame(geometry=[], crs=base.crs)

        # true observed area
        obs_true_m2 = float(gpd.GeoSeries([obs_geom_base], crs=base.crs).to_crs(AREA_CRS).iloc[0].area)

        # ELM raster-space metrics
        elm_sim = burn_mask(base, ELM_BAND, BURN_THRESHOLD)
        elm_obs = obs_mask(base, obs_geom_base)
        metrics.update(raster_metrics(elm_obs, elm_sim, pixel_area_m2(base), "elm"))

    # FAR raster-space metrics
    if far_toa:
        try:
            with rasterio.open(far_toa) as far_ds:
                require_projected(far_ds, "FARSITE TOA")
                obs_geom_far = obs_src.to_crs(far_ds.crs).iloc[0]
                try:
                    obs_geom_far = obs_geom_far.buffer(0)
                except Exception:
                    pass
                far_sim = burn_mask(far_ds, FAR_BAND, BURN_THRESHOLD)
                far_obs = obs_mask(far_ds, obs_geom_far)
                metrics.update(raster_metrics(far_obs, far_sim, pixel_area_m2(far_ds), "far"))
        except Exception as e:
            log.warning("Case %s: FARSITE metrics failed: %s", case_id, e)

    c = Case(
        case_id=case_id,
        elm_toa=elm_toa,
        far_toa=far_toa,
        obs_geom_base=obs_geom_base,
        ign_base=ign_base,
        tstop_h=tstop_h,
        tstop_src=tstop_src,
        obs_true_m2=obs_true_m2,
        metrics=metrics,
    )

    # Satellite (per-case)
    if SAT_ENABLED:
        sat = read_case_sat(case_dir)
        if sat is not None:
            sat_area = sat.to_crs(AREA_CRS)
            burn_area_geom = gpd.GeoSeries([c.obs_geom_base], crs=ign_base.crs).to_crs(AREA_CRS).iloc[0]
            pts = sat_area.loc[sat_area.geometry.within(burn_area_geom)].copy()
            if not pts.empty:
                discovery = pd.Timestamp(pts["sat_dt"].min())
                c.discovery_dt = discovery
                end, times, areas, target = sat_chain(
                    pts, burn_area_geom, discovery, SAT_BUFFER_M, SAT_CHAIN_MAX_GAP, SAT_COVERAGE_FRAC
                )
                c.sat_end, c.sat_times, c.sat_areas_m2, c.sat_target_m2 = end, times, areas, target
                c.metrics["satellite_end_time"] = str(end) if not pd.isna(end) else ""
                c.metrics["satellite_final_area_m2"] = float(areas[-1]) if areas else np.nan

    return c


def iter_cases(root: Path) -> list[Path]:
    return [d for d in sorted(root.iterdir()) if d.is_dir() and (d / FIRESCAR_NAME).exists()]


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    cases = iter_cases(ROOT_DIR)
    if not cases:
        raise RuntimeError(f"No cases found under {ROOT_DIR} (need subdirs containing {FIRESCAR_NAME}).")

    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    with PdfPages(OUT_PDF) as pdf:
        for d in cases:
            try:
                c = process_case(d)
                pdf.savefig(case_page(c, Path(d)))
                plt.close("all")
                rows.append(c.metrics)
                log.info("Added: %s | far=%s | sat_pts=%d",
                         d.name,
                         "yes" if c.far_toa else "no",
                         len(c.sat_times))
            except Exception as e:
                log.warning("Case %s failed: %s", d.name, e)
                pdf.savefig(error_page(d.name, str(e)))
                plt.close("all")

    df = pd.DataFrame(rows)
    log.info("Wrote: %s | processed: %d", OUT_PDF, len(df))
    # df.to_csv(OUT_PDF.with_suffix(".csv"), index=False)


if __name__ == "__main__":
    main()