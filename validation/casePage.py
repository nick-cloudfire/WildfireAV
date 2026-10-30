"""
One report page per case (A4 landscape):

    left    one map per model, same extent: where the model and the observed
            perimeter agree (hit), simulated only (overprediction), observed
            only (missed), arrival-time isochrones, ignition, barriers.
            Side by side, or stacked for wide fires, whichever is larger.
    right   case facts, scores table (best value per row in bold) and area
            growth: every model and the satellite-detected area on one time
            axis, hours since the satellite ignition (= simulation time 0).

Map layers are drawn at the raster's own resolution (cropped to the map), and
the line work (isochrones, perimeter) is simplified to the cell size, so pages
stay sharp when zoomed and small on disk.  Rasters are read one case at a
time and released when the page is done.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from contourpy import contour_generator
from matplotlib.collections import LineCollection
from matplotlib.colors import to_rgb
from matplotlib.legend_handler import HandlerTuple
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter, LogLocator, MaxNLocator
from rasterio.features import rasterize
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window
from shapely.geometry import LineString

import pipelineConfig as cfg
from caseMetrics import BURN_THRESHOLD, M2_TO_ACRES, MODELS, Case, read_toa, to_crs_robust, toa_crs
from reportStyle import (BARRIER, INK, INK_2, INK_3, MISSED, PANEL, RULE, SATELLITE, new_page, shade,
                         tint)

log = logging.getLogger("validation")

MAP_MAX_PX     = 1400       # longest side of a map layer; larger rasters are thinned (nearest)
MAP_PAD        = 0.06       # margin around the fire, fraction of the extent
ISO_TARGET     = 6          # aim for about this many isochrones per map
ISO_STEPS_H    = (1, 2, 3, 6, 12, 24, 48, 72, 96, 168, 336, 720)
ISO_SMOOTH     = 2          # isochrones follow the TOA averaged over (2r+1)^2 cells ...
ISO_MIN_LENGTH = 8          # ... and skip contour pieces shorter than this many cells
OVER_TINT      = 0.62       # simulated-only cells: model colour this far towards white

# figure regions (fractions of the page)
MAPS_BOX  = (0.035, 0.165, 0.625, 0.705)          # left, bottom, width, height
RIGHT_X   = 0.69
RIGHT_W   = 0.275


# ---------------------------------------------------------------------------
# Rasters on one grid
# ---------------------------------------------------------------------------

@dataclass
class _Layers:
    crs:       object
    view:      tuple[float, float, float, float]     # left, right, bottom, top shown on the map
    extent:    tuple[float, float, float, float]     # the same for the cropped rasters (inside the domain)
    domain:    tuple[float, float, float, float]     # the same for the whole simulation grid
    cell:      float                                 # metres per drawn pixel
    observed:  np.ndarray                            # bool
    barrier:   np.ndarray | None                     # bool
    toa_h:     dict[str, np.ndarray]                 # model label -> TOA in hours (NaN = unburned)
    perimeter: object                                # observed perimeter, shapely, in crs


def _on_grid(src, src_crs, base) -> np.ndarray:
    """Band 1 of src (float32, nodata -> NaN) resampled (nearest) onto the base grid."""
    if src.name == base.name:
        return read_toa(src)
    out = np.full((base.height, base.width), np.nan, dtype=np.float32)
    reproject(read_toa(src), out, src_transform=src.transform, src_crs=src_crs,
              dst_transform=base.transform, dst_crs=base.crs or src_crs,
              resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
    return out


def _burned(toa: np.ndarray) -> np.ndarray:
    return np.isfinite(toa) & (toa >= BURN_THRESHOLD)


def _fire_bounds(base, perimeter, toas: dict[str, np.ndarray]) -> tuple[float, float, float, float]:
    """(left, right, bottom, top) around the observed perimeter and every simulated burn, padded."""
    minx, miny, maxx, maxy = perimeter.bounds
    for toa in toas.values():
        r, c = np.nonzero(_burned(toa))
        if r.size:
            (x0, y0), (x1, y1) = base.transform * (c.min(), r.max() + 1), base.transform * (c.max() + 1, r.min())
            minx, maxx, miny, maxy = min(minx, x0), max(maxx, x1), min(miny, y0), max(maxy, y1)
    pad = MAP_PAD * max(maxx - minx, maxy - miny)
    return minx - pad, maxx + pad, miny - pad, maxy + pad


def _load(c: Case, present, choose_view) -> _Layers:
    """Every map layer on the first model's grid, cropped to the view that choose_view(fire bounds) picks."""
    with rasterio.open(c.model_toas[present[0].label]) as base:
        crs = toa_crs(base, None)
        toas = {}
        for mc in present:
            try:
                with rasterio.open(c.model_toas[mc.label]) as src:
                    toas[mc.label] = _on_grid(src, toa_crs(src, crs), base) * mc.to_hours
            except Exception as e:
                log.warning("Case %s: %s not drawn on the map: %s", c.case_id, mc.label, e)
        perimeter = to_crs_robust(c.observed, crs).iloc[0]
        view = choose_view(_fire_bounds(base, perimeter, toas))

        win = rasterio.windows.from_bounds(view[0], view[2], view[1], view[3], base.transform)
        c0, r0 = max(0, int(np.floor(win.col_off))), max(0, int(np.floor(win.row_off)))
        c1 = min(base.width, int(np.ceil(win.col_off + win.width)))
        r1 = min(base.height, int(np.ceil(win.row_off + win.height)))
        win = Window(c0, r0, c1 - c0, r1 - r0)
        step = max(1, int(np.ceil(max(win.height, win.width) / MAP_MAX_PX)))
        crop = lambda a: a[r0:r1:step, c0:c1:step]

        observed = rasterize([(perimeter, 1)], out_shape=(base.height, base.width), transform=base.transform,
                             dtype=np.uint8).astype(bool)
        barrier = None
        path = c.case_dir / cfg.INPUTS_SUBDIR_NAME / cfg.BARRIER_FILE_NAME
        if path.exists():
            try:
                with rasterio.open(path) as src:
                    barrier = crop(_on_grid(src, src.crs or crs, base) >= 1)     # barrier width (m)
            except Exception as e:
                log.warning("Case %s: barriers not drawn: %s", c.case_id, e)
        left, bottom, right, top = rasterio.windows.bounds(win, base.transform)
        d_left, d_bottom, d_right, d_top = base.bounds
        return _Layers(crs, view, (left, right, bottom, top), (d_left, d_right, d_bottom, d_top),
                       abs(base.transform.a) * step, crop(observed), barrier,
                       {k: crop(v) for k, v in toas.items()}, perimeter)


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

def _iso_step(toas: dict[str, np.ndarray], duration_h: float | None) -> float:
    span = duration_h or max((float(np.nanmax(t)) for t in toas.values() if _burned(t).any()), default=24.0)
    return next((s for s in ISO_STEPS_H if span / s <= ISO_TARGET), ISO_STEPS_H[-1])


def _box_mean(a: np.ndarray, valid: np.ndarray, r: int) -> np.ndarray:
    """Mean of *a* over the valid cells of a (2r+1)^2 window (normalised box filter)."""
    def box(x):
        c = np.pad(x, r).cumsum(0).cumsum(1)
        c = np.pad(c, ((1, 0), (1, 0)))
        k = 2 * r + 1
        return c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    weight = box(valid.astype(np.float64))
    with np.errstate(invalid="ignore", divide="ignore"):
        return (box(np.where(valid, a, 0.0)) / weight).astype(np.float32)


def _isochrones(ax, L: _Layers, toa: np.ndarray, step_h: float, color) -> None:
    """Arrival-time contours every step_h hours, smoothed over ISO_SMOOTH cells, without specks."""
    burned = _burned(toa)
    if burned.sum() < 4:
        return
    levels = np.arange(step_h, float(toa[burned].max()), step_h)
    if not levels.size:
        return
    smooth = _box_mean(toa, burned, ISO_SMOOTH)
    h, w = toa.shape
    x = np.linspace(L.extent[0] + L.cell / 2, L.extent[1] - L.cell / 2, w)
    y = np.linspace(L.extent[3] - L.cell / 2, L.extent[2] + L.cell / 2, h)
    gen = contour_generator(x, y, np.ma.masked_where(~burned, smooth), line_type="Separate")
    segs = []
    for lvl in levels:
        for line in gen.lines(lvl):
            simple = LineString(line).simplify(0.75 * L.cell) if len(line) > 1 else None
            if simple is None or simple.length < ISO_MIN_LENGTH * L.cell:
                continue
            segs.append(np.asarray(simple.coords))
    ax.add_collection(LineCollection(segs, colors=[color], linewidths=0.45, alpha=0.9, zorder=3))


def _outline(geom, tolerance: float) -> list[np.ndarray]:
    geom = geom.simplify(tolerance)
    polys = getattr(geom, "geoms", [geom])
    return [np.asarray(ring.coords) for p in polys for ring in (p.exterior, *p.interiors)]


def _rgba(mask: np.ndarray, color, out: np.ndarray) -> None:
    out[mask] = (*to_rgb(color), 1.0)


def _scale_bar(ax, width_m: float) -> None:
    target = width_m / 4
    nice = max(n * 10 ** e for e in range(0, 7) for n in (1, 2, 5) if n * 10 ** e <= target)
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    bx, by = x0 + 0.04 * (x1 - x0), y0 + 0.04 * (y1 - y0)
    hgt = 0.012 * (y1 - y0)
    for i in range(2):
        ax.add_patch(Rectangle((bx + i * nice / 2, by), nice / 2, hgt, facecolor=INK if i == 0 else "white",
                               edgecolor=INK, linewidth=0.6, zorder=6))
    label = f"{nice / 1000:g} km" if nice >= 1000 else f"{nice:g} m"
    ax.text(bx + nice + 0.01 * (x1 - x0), by + hgt / 2, label, fontsize=7, va="center", color=INK, zorder=6,
            bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none", alpha=0.8))


def _fit_extent(bounds, ax_w_in: float, ax_h_in: float) -> tuple[float, float, float, float]:
    """(left, right, bottom, top) widened along one axis to the axes' aspect, so the map fills its box."""
    left, right, bottom, top = bounds
    w, h = right - left, top - bottom
    if w / h < ax_w_in / ax_h_in:
        grow = (h * ax_w_in / ax_h_in - w) / 2
        return left - grow, right + grow, bottom, top
    grow = (w * ax_h_in / ax_w_in - h) / 2
    return left, right, bottom - grow, top + grow


def _map(ax, c: Case, L: _Layers, mc, step_h: float) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(True)
        s.set_color(RULE)
    left, right, bottom, top = L.view
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    ax.set_aspect("equal", adjustable="box")
    ax.set_anchor("C")

    img = np.ones((*L.observed.shape, 4), dtype=np.float32)          # opaque white: no alpha mask in the PDF
    toa = L.toa_h.get(mc.label)
    sim = _burned(toa) if toa is not None else np.zeros_like(L.observed)
    _rgba(L.observed & ~sim, MISSED, img)
    if L.barrier is not None:
        _rgba(L.barrier & ~sim, BARRIER, img)
    _rgba(sim & ~L.observed, tint(mc.color, OVER_TINT), img)
    _rgba(sim & L.observed, mc.color, img)
    ax.imshow(img, extent=L.extent, origin="upper", interpolation="none", zorder=1)

    if toa is not None:
        _isochrones(ax, L, toa, step_h, shade(mc.color, 0.55))
    ax.add_collection(LineCollection(_outline(L.perimeter, 0.5 * L.cell), colors=[INK], linewidths=0.9,
                                     zorder=4))
    if not c.ignition.empty:
        p = c.ignition.to_crs(L.crs).geometry.iloc[0]
        ax.plot(p.x, p.y, marker="*", markersize=11, color="white", zorder=7, linestyle="none")
        ax.plot(p.x, p.y, marker="*", markersize=8, color=INK, zorder=8, linestyle="none")
    if _domain_visible(L):
        dl, dr, db, dt = L.domain
        ax.add_patch(Rectangle((dl, db), dr - dl, dt - db, fill=False, edgecolor=INK_3, linewidth=0.6,
                               linestyle=(0, (4, 3)), zorder=2))
    _scale_bar(ax, right - left)

    ax.set_title(mc.label, color=INK)
    if toa is None:
        ax.set_title("no output", loc="right", color=INK_3, fontweight="normal")
        ax.text(0.5, 0.95, f"No {mc.label} output", transform=ax.transAxes, ha="center", va="top",
                color=INK_3, fontsize=10, bbox=dict(fc="white", ec="none", alpha=0.85))
    else:
        ax.set_title(f"Jaccard {c.score(mc, 'jaccard'):.3f}", loc="right", fontweight="normal", color=INK_2)


def _domain_visible(L: _Layers) -> bool:
    (l, r, b, t), (dl, dr, db, dt) = L.view, L.domain
    return l < dl or r > dr or b < db or t > dt


def _map_legend(fig, models, step_h: float, has_barrier: bool, has_ignition: bool) -> None:
    two = lambda amount: tuple(Patch(facecolor=tint(m.color, amount), edgecolor="none") for m in models)
    handles = [two(0.0), two(OVER_TINT), Patch(facecolor=MISSED, edgecolor="none"),
               Line2D([], [], color=INK, linewidth=0.9),
               tuple(Line2D([], [], color=shade(m.color, 0.55), linewidth=0.6) for m in models)]
    step = f"{step_h:g} h" if step_h < 48 else f"{step_h / 24:g} days"
    labels = ["Simulated and observed", "Simulated only", "Observed only (missed)",
              "Observed perimeter (MTBS)", f"Arrival time, every {step}"]
    if has_ignition:
        handles.append(Line2D([], [], marker="*", markersize=8, color=INK, linestyle="none"))
        labels.append("Ignition (reported)")
    if has_barrier:
        handles.append(Patch(facecolor=BARRIER, edgecolor="none"))
        labels.append("Roads and rivers (barriers)")
    left, bottom, width, _ = MAPS_BOX
    fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(left - 0.006, bottom - 0.012),
               ncol=4, handler_map={tuple: HandlerTuple(ndivide=None, pad=0.15)}, handlelength=2.6,
               columnspacing=1.6, fontsize=7.5, borderaxespad=0)


def _maps(fig, c: Case) -> None:
    present = [mc for mc in MODELS if c.model_toas.get(mc.label)]
    left, bottom, width, height = MAPS_BOX
    gap, n = 0.018, len(MODELS)
    page_w, page_h = fig.get_size_inches()
    layout = {}

    def choose_view(bounds):
        """Side by side or stacked, whichever draws the fire larger; the view fills the map box."""
        ew, eh = bounds[1] - bounds[0], bounds[3] - bounds[2]
        side = ((width - gap * (n - 1)) / n * page_w, height * page_h)        # inches per map
        stack = (width * page_w, (height - gap * 2 * (n - 1)) / n * page_h)    # (room for titles)
        scale = lambda box: min(box[0] / ew, box[1] / eh)
        if scale(side) >= scale(stack):
            w = (width - gap * (n - 1)) / n
            layout["rects"] = [(left + i * (w + gap), bottom, w, height) for i in range(n)]
            return _fit_extent(bounds, *side)
        h = (height - gap * 2 * (n - 1)) / n
        layout["rects"] = [(left, bottom + (n - 1 - i) * (h + 2 * gap), width, h) for i in range(n)]
        return _fit_extent(bounds, *stack)

    L = _load(c, present, choose_view)
    step_h = _iso_step(L.toa_h, c.duration_h)
    for rect, mc in zip(layout["rects"], MODELS):
        _map(fig.add_axes(rect), c, L, mc, step_h)
    _map_legend(fig, MODELS, step_h, L.barrier is not None and L.barrier.any(), not c.ignition.empty)
    note = f"Map grid {L.crs.to_string() if hasattr(L.crs, 'to_string') else L.crs}, drawn at {L.cell:g} m per pixel."
    if _domain_visible(L):
        note += "  Dashed frame: edge of the simulation domain."
    fig.text(left, 0.045, note, fontsize=6.5, color=INK_3)


# ---------------------------------------------------------------------------
# Facts and scores
# ---------------------------------------------------------------------------

def _utc(ts) -> str:
    return f"{pd.Timestamp(ts):%Y-%m-%d %H:%M} UTC"


def _facts(fig, c: Case, top: float) -> float:
    rows = [("Observed area", f"{c.observed_m2 / 1e4:,.0f} ha  ({c.observed_m2 * M2_TO_ACRES:,.0f} ac)")]
    if c.duration_h:
        rows.append(("Simulated", f"{c.duration_h:,.1f} h"))
    if c.avg_wind_kph:
        rows.append(("Mean wind", f"{c.avg_wind_kph:.1f} km/h  (ERA5, 10 m)"))
    if c.sat_curve is not None:
        s = c.sat_curve
        rows += [("Satellite overpasses", f"{len(s.times)}"),
                 ("Satellite coverage", f"{s.areas_m2[-1] / 1e4:,.0f} ha")]
        if c.sat_end is not None:
            rows.append((f"{cfg.COVERAGE_FRACTION:.0%} covered at", _utc(c.sat_end)))
    fig.text(RIGHT_X, top, "Case", fontsize=9.5, fontweight="bold", va="top")
    y = top - 0.032
    for k, v in rows:
        fig.text(RIGHT_X, y, k, color=INK_2, va="top")
        fig.text(RIGHT_X + 0.112, y, v, va="top")
        y -= 0.024
    return y


def _scores(fig, c: Case, top: float) -> float:
    present = [mc for mc in MODELS if c.model_toas.get(mc.label)]
    rows = [("Simulated area (ha)", "simulated_m2",   lambda v: f"{v / 1e4:,.0f}", None),
            ("Area ratio (sim / obs)", "ratio_of_areas", lambda v: f"{v:.2f}", "ratio"),
            ("Jaccard",           "jaccard",        lambda v: f"{v:.3f}", "max"),
            ("Sørensen",          "sorensen",       lambda v: f"{v:.3f}", "max"),
            ("Cohen's κ",         "kappa",          lambda v: f"{v:.3f}", "max")]
    fig.text(RIGHT_X, top, "Scores", fontsize=9.5, fontweight="bold", va="top")
    col_x = [RIGHT_X + RIGHT_W - 0.075 * (len(present) - 1 - i) for i in range(len(present))]
    y = top - 0.034
    for x, mc in zip(col_x, present):
        fig.text(x, y, mc.label, ha="right", va="top", fontweight="bold")
    y -= 0.026
    fig.add_artist(Line2D([RIGHT_X, RIGHT_X + RIGHT_W], [y + 0.004, y + 0.004], color=RULE, linewidth=0.8))
    for x, mc in zip(col_x, present):
        fig.add_artist(Line2D([x - 0.058, x], [y + 0.004, y + 0.004], color=mc.color, linewidth=2.2,
                              solid_capstyle="butt"))
    for label, key, fmt, better in rows:
        vals = [c.score(mc, key) for mc in present]
        ok = [v for v in vals if np.isfinite(v)]
        best = None
        if better and len(ok) > 1:
            target = (lambda v: -abs(np.log(v)) if v > 0 else -np.inf) if better == "ratio" else (lambda v: v)
            best = max(ok, key=target)
        y -= 0.006
        fig.text(RIGHT_X, y, label, color=INK_2, va="top")
        for x, v in zip(col_x, vals):
            fig.text(x, y, fmt(v) if np.isfinite(v) else "–", ha="right", va="top",
                     fontweight="bold" if best is not None and v == best else "normal")
        y -= 0.024
    fig.add_artist(Line2D([RIGHT_X, RIGHT_X + RIGHT_W], [y + 0.006, y + 0.006], color=RULE, linewidth=0.8))
    if len(present) > 1:
        fig.text(RIGHT_X, y - 0.004, "Bold: better of the models (area ratio: closer to 1).", fontsize=6.5,
                 color=INK_3, va="top")
    return y - 0.02


# ---------------------------------------------------------------------------
# Area growth
# ---------------------------------------------------------------------------

def _thin(x, y, n=400):
    idx = np.unique(np.linspace(0, len(x) - 1, min(len(x), n)).round().astype(int))
    return np.asarray(x)[idx], np.asarray(y)[idx]


def _growth(fig, c: Case, rect) -> None:
    fig.text(RIGHT_X, rect[1] + rect[3] + 0.062, "Burned area over time", fontsize=9.5, fontweight="bold",
             va="top")
    ax = fig.add_axes(rect)
    obs_ha = c.observed_m2 / 1e4
    series = []
    for mc in MODELS:
        hours, area = c.model_curves.get(mc.label, ([], []))
        if len(hours):
            h, a = _thin(np.r_[0.0, hours], np.r_[0.0, area / 1e4])
            ax.plot(h, a, color=mc.color, label=mc.label, zorder=3)
            series.append(a)
    if c.sat_curve is not None and c.start is not None:
        s = c.sat_curve
        h = (s.times - c.start).total_seconds().to_numpy() / 3600.0
        a = s.areas_m2 / 1e4
        ax.step(h, a, where="post", color=SATELLITE, linewidth=1.1, zorder=4, label="Satellite detections")
        ax.plot(h, a, "o", color=SATELLITE, markersize=2.2, zorder=4)
        series.append(a)
        if c.sat_end is not None:
            he = (c.sat_end - c.start).total_seconds() / 3600.0
            ax.axvline(he, color=INK_3, linestyle=(0, (3, 2)), linewidth=0.8, zorder=2)
            ax.text(he, 0.03, f"{cfg.COVERAGE_FRACTION:.0%} covered ", transform=ax.get_xaxis_transform(),
                    fontsize=6.5, color=INK_3, va="bottom", ha="right", rotation=90)
    ax.axhline(obs_ha, color=INK, linestyle=(0, (1, 1.5)), linewidth=0.9, zorder=2)
    ax.text(0.99, obs_ha, "observed final area", transform=ax.get_yaxis_transform(), fontsize=6.5,
            color=INK_2, ha="right", va="bottom")

    peak = max([obs_ha] + [float(np.max(a)) for a in series if len(a)])
    low = min([obs_ha] + [float(np.min(a[a > 0])) for a in series if (a > 0).any()])
    if peak / obs_ha > 4 or (series and max(float(np.max(a)) for a in series) / obs_ha < 0.2):
        ax.set_yscale("log")
        ax.set_ylim(max(low * 0.5, peak * 1e-4), peak * 1.6)
        ax.yaxis.set_major_locator(LogLocator(base=10))
        ax.set_ylabel("Area (ha, log scale)")
    else:
        ax.set_ylim(0, peak * 1.08)
        ax.yaxis.set_major_locator(MaxNLocator(5))
        ax.set_ylabel("Area (ha)")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}" if v >= 1 else f"{v:g}"))
    ax.set_xlim(0, max(c.duration_h or 0, *(ax.get_xlim()[1:])))
    ax.xaxis.set_major_locator(MaxNLocator(6, integer=True))
    ax.set_xlabel("Hours since simulation start (satellite ignition)")
    ax.legend(loc="lower left", bbox_to_anchor=(-0.02, 1.0), ncol=3, handlelength=1.4, columnspacing=1.0,
              borderaxespad=0.2)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def case_page(c: Case) -> plt.Figure:
    name = c.fire_name or "Unnamed fire"
    title = f"{name} ({c.fire_year})" if c.fire_year else name
    sub = [f"Case {c.case_id}"]
    if c.start is not None:
        sub.append(f"satellite ignition {_utc(c.start)}")
    if c.duration_h:
        sub.append(f"{c.duration_h:,.0f} h simulated")
    fig = new_page(title, "   ·   ".join(sub))
    _maps(fig, c)
    y = _facts(fig, c, 0.875)
    _scores(fig, c, y - 0.018)
    _growth(fig, c, (RIGHT_X + 0.045, 0.10, RIGHT_W - 0.045, 0.25))
    return fig


def error_page(case_id: str, message: str) -> plt.Figure:
    fig = new_page(f"Case {case_id}", "Not evaluated")
    fig.text(0.035, 0.86, message, va="top", ha="left", fontsize=10, color=INK_2, wrap=True)
    return fig
