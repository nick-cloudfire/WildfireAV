"""
One report page per case (A4 landscape):

    left          map: simulated burn masks + arrival-time isochrones of every
                  model, observed perimeter (black), ignition (x), barriers (grey)
    top right     scores table
    bottom right  area growth: each model's simulated area against simulation
                  time, and the satellite-detected area against date

Rasters are read here, one case at a time, and released when the page is done.
"""

from __future__ import annotations

import logging

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from rasterio.plot import plotting_extent
from rasterio.warp import Resampling, reproject

import pipelineConfig as cfg
from caseMetrics import BURN_THRESHOLD, M2_TO_ACRES, MODELS, Case, to_crs_robust, read_toa, toa_crs

log = logging.getLogger("validation")

A4_LANDSCAPE = (11.69, 8.27)
N_ISOCHRONES = 6       # arrival-time contour lines per model on the map
CURVE_POINTS = 300     # points per plotted curve


def _on_grid(src: rasterio.DatasetReader, src_crs, base: rasterio.DatasetReader) -> np.ndarray:
    """TOA of src resampled (nearest) onto the base raster's grid."""
    if src.name == base.name:
        return read_toa(src)
    out = np.full((base.height, base.width), np.nan, dtype=np.float32)
    reproject(read_toa(src), out, src_transform=src.transform, src_crs=src_crs,
              dst_transform=base.transform, dst_crs=base.crs, resampling=Resampling.nearest,
              src_nodata=np.nan, dst_nodata=np.nan)
    return out


def _draw_mask(ax, extent, mask: np.ndarray, rgb, alpha: float, zorder: int = 1) -> None:
    if not mask.any():
        return
    img = np.zeros((*mask.shape, 4), dtype=np.float32)
    img[mask] = (*rgb, alpha)
    ax.imshow(img, extent=extent, origin="upper", interpolation="nearest", zorder=zorder)


def _draw_isochrones(ax, extent, toa: np.ndarray, color) -> None:
    burned = np.isfinite(toa) & (toa >= BURN_THRESHOLD)
    if burned.sum() < 4 or toa[burned].max() <= toa[burned].min():
        return
    levels = np.linspace(toa[burned].min(), toa[burned].max(), N_ISOCHRONES + 2)[1:-1]
    h, w = toa.shape
    x = np.linspace(extent[0], extent[1], w)
    y = np.linspace(extent[3], extent[2], h)          # rows run top -> bottom
    ax.contour(x, y, np.ma.masked_where(~burned, toa), levels=levels,
               colors=[color], linewidths=0.7, alpha=0.85)


def _draw_barriers(ax, extent, base: rasterio.DatasetReader, case_dir) -> None:
    path = case_dir / cfg.INPUTS_SUBDIR_NAME / cfg.BARRIER_FILE_NAME
    if not path.exists():
        return
    with rasterio.open(path) as src:
        width = np.zeros((base.height, base.width), dtype=np.float32)
        reproject(src.read(1).astype(np.float32), width, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=base.transform, dst_crs=base.crs, resampling=Resampling.nearest)
    _draw_mask(ax, extent, width > 0, (0.2, 0.2, 0.2), 0.15, zorder=0)


def _map(ax, c: Case) -> None:
    ax.grid(True, alpha=0.2, linewidth=0.6)
    ax.set_aspect("equal", "box")
    ax.ticklabel_format(useOffset=False, style="plain")
    ax.tick_params(labelsize=7)
    present = [mc for mc in MODELS if c.model_toas.get(mc.label)]
    drawn = []
    with rasterio.open(c.model_toas[present[0].label]) as base:
        base_crs = toa_crs(base, None)
        extent = plotting_extent(base)
        _draw_barriers(ax, extent, base, c.case_dir)
        for mc in present:
            try:
                with rasterio.open(c.model_toas[mc.label]) as src:
                    toa = _on_grid(src, toa_crs(src, base_crs), base)
                _draw_mask(ax, extent, np.isfinite(toa) & (toa >= BURN_THRESHOLD), mc.color, mc.alpha)
                _draw_isochrones(ax, extent, toa, mc.color)
                drawn.append(mc)
            except Exception as e:
                log.warning("Case %s: %s not drawn on the map: %s", c.case_id, mc.label, e)
        observed = to_crs_robust(c.observed, base_crs)
        observed.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=2)
        if not c.ignition.empty:
            c.ignition.to_crs(base_crs).plot(ax=ax, marker="x", markersize=60, zorder=5)

    minx, miny, maxx, maxy = observed.total_bounds
    pad = 0.1 * max(maxx - minx, maxy - miny)
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_xlabel(f"Easting (m, {base_crs.to_string()})", fontsize=8)
    ax.set_ylabel("Northing (m)", fontsize=8)

    scores = [f"J({mc.label})={c.score(mc, 'jaccard'):.3f}" for mc in present]
    ax.set_title("Burn masks  |  " + "  ".join(scores))
    handles = [Line2D([0], [0], color="black", linewidth=2, label="Observed")]
    for mc in MODELS:
        if mc in drawn:
            handles += [Patch(facecolor=mc.color, alpha=mc.alpha, label=f"{mc.label} burned"),
                        Line2D([0], [0], color=mc.color, linewidth=0.7,
                               label=f"{mc.label} isochrones ({N_ISOCHRONES})")]
        else:
            handles.append(Patch(facecolor="none", edgecolor="gray", linestyle=":",
                                 label=f"{mc.label} (no output)"))
    if not c.ignition.empty:
        handles.append(Line2D([0], [0], marker="x", linestyle="None", markersize=10, label="Ignition"))
    ax.legend(handles=handles, loc="upper right", fontsize=8)


def _table(c: Case) -> str:
    present = [mc for mc in MODELS if c.model_toas.get(mc.label)]
    w = max(10, *(len(mc.label) for mc in present))
    observed = f"{c.observed_m2 / 1e4:,.0f} ha ({c.observed_m2 * M2_TO_ACRES:,.0f} ac)"
    lines = [
        f"Observed area : {observed}",
        f"Simulated     : {c.duration_h:.1f} h" if c.duration_h else "Simulated     : (unknown)",
        f"Mean wind     : {c.avg_wind_kph:.1f} km/h (ERA5, 10 m)" if c.avg_wind_kph else "",
        "",
        f"{'SCORES':<14}" + "".join(f"  {mc.label:>{w}}" for mc in present),
        "─" * (14 + len(present) * (w + 2)),
    ]
    rows = [("Burned ha",  "simulated_m2",   lambda v: f"{v / 1e4:,.0f}"),
            ("Area ratio", "ratio_of_areas", lambda v: f"{v:.2f}"),
            ("Jaccard",    "jaccard",        lambda v: f"{v:.3f}"),
            ("Sorensen",   "sorensen",       lambda v: f"{v:.3f}"),
            ("Kappa",      "kappa",          lambda v: f"{v:.3f}")]
    for label, key, fmt in rows:
        cells = [c.score(mc, key) for mc in present]
        lines.append(f"{label:<14}" + "".join(f"  {fmt(v) if np.isfinite(v) else '':>{w}}" for v in cells))
    if c.sat_curve is not None:
        lines += ["", "SATELLITE",
                  f"  First detection : {c.sat_curve.times[0]:%Y-%m-%d %H:%M} UTC",
                  f"  Overpasses      : {len(c.sat_curve.times)}",
                  f"  Covered area    : {c.sat_curve.areas_m2[-1] / 1e4:,.0f} ha"]
        if c.sat_end is not None:
            lines.append(f"  End ({cfg.COVERAGE_FRACTION:.0%} cover) : {c.sat_end:%Y-%m-%d %H:%M} UTC")
    return "\n".join(lines)


def _thin(x, y):
    idx = np.unique(np.linspace(0, len(x) - 1, min(len(x), CURVE_POINTS)).round().astype(int))
    return x[idx], y[idx]


def _growth(ax, c: Case) -> None:
    ax.grid(True, alpha=0.3, linewidth=0.6)
    ax.set_title("Area growth (relative to final area)")
    ax.set_ylabel("Area / final area")
    ax.set_xlabel("Simulation time (hours)")
    ax.set_ylim(0.0, 1.05)
    for mc in MODELS:
        hours, area = c.model_curves.get(mc.label, ([], []))
        if len(hours):
            ax.plot(*_thin(hours, area / area[-1]), color=mc.color, linewidth=1.5, label=f"{mc.label}")
    if c.duration_h:
        ax.set_xlim(0.0, c.duration_h)

    handles, labels = ax.get_legend_handles_labels()
    if c.sat_curve is not None:
        sat = ax.twiny()                                   # date axis on top
        curve = c.sat_curve
        sat.plot(*_thin(curve.times.to_numpy(), curve.areas_m2 / curve.areas_m2[-1]),
                 color="black", linewidth=1.2, label="Satellite")
        sat.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))
        sat.xaxis.set_major_formatter(mdates.ConciseDateFormatter(sat.xaxis.get_major_locator()))
        sat.set_xlabel("Date (UTC)")
        if c.sat_end is not None:
            sat.axvline(c.sat_end, color="black", linestyle="--", linewidth=1.0)
            ax.axhline(cfg.COVERAGE_FRACTION * curve.effective_m2 / curve.areas_m2[-1],
                       color="black", linestyle="--", linewidth=1.0)
        h2, l2 = sat.get_legend_handles_labels()
        handles, labels = handles + h2, labels + l2
    if handles:
        ax.legend(handles, labels, fontsize=8, loc="lower right")


def case_page(c: Case) -> plt.Figure:
    title = " ".join(p for p in (c.fire_name, f"({c.fire_year})" if c.fire_year else None) if p)
    fig = plt.figure(figsize=A4_LANDSCAPE)
    fig.suptitle(f"{title}  —  Case {c.case_id}" if title else f"Case {c.case_id}",
                 fontsize=12, fontweight="bold", y=0.99)
    grid = fig.add_gridspec(2, 2, width_ratios=[1.45, 1.0], height_ratios=[1.0, 0.75])
    _map(fig.add_subplot(grid[:, 0]), c)
    ax_txt = fig.add_subplot(grid[0, 1])
    ax_txt.axis("off")
    ax_txt.text(0.0, 1.0, _table(c), va="top", ha="left", fontsize=8.5, family="monospace",
                transform=ax_txt.transAxes)
    _growth(fig.add_subplot(grid[1, 1]), c)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def error_page(case_id: str, message: str) -> plt.Figure:
    fig = plt.figure(figsize=A4_LANDSCAPE)
    fig.text(0.02, 0.98, f"Case {case_id}\n\nNot evaluated:\n{message}",
             va="top", ha="left", family="monospace", fontsize=12)
    return fig
