"""
Report pages that summarise all cases:

    cover          case counts, headline scores of each model, how to read the report
    rankings       the 10 best and 10 worst cases of each model, by Jaccard
    distributions  histogram and cumulative distribution of each score, models overlaid
    model vs model each case's Jaccard and area ratio, ELMFIRE against FARSITE
    area bias      simulated / observed area of every case, sorted, log2 scale
    conditions     Jaccard and area ratio against wind, fire size and duration,
                   with binned medians (Sørensen = 2J / (1 + J), so it adds nothing here)

Only cases that ignited in at least one model are summarised
(see getValidationPDF.NOT_IGNITED_JACCARD); the worst-case list also leaves
out cases where that model did not ignite.
"""

from __future__ import annotations

from datetime import date

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Patch
from matplotlib.ticker import FixedLocator, FuncFormatter, LogLocator, NullFormatter, NullLocator

from caseMetrics import M2_TO_ACRES, MODELS, Case
from reportStyle import INK, INK_2, INK_3, PANEL, RULE, new_page, tint

SCORES = [("jaccard", "Jaccard"), ("sorensen", "Sørensen"), ("kappa", "Cohen's κ")]
RATIO_CLIP = 2.0 ** 8        # area ratios beyond 1/256x .. 256x are drawn at the edge
MIN_PER_BIN = 15             # binned medians: at least this many cases per bin


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _values(cases: list[Case], model, what: str) -> np.ndarray:
    """One value per case that has this model's output: a case attribute or a score."""
    out = []
    for c in cases:
        if not c.model_toas.get(model.label):
            continue
        if what == "observed_acres":
            out.append(c.observed_m2 * M2_TO_ACRES)
        elif what in ("duration_h", "avg_wind_kph"):
            out.append(getattr(c, what) or np.nan)
        else:
            out.append(c.score(model, what))
    return np.asarray(out, dtype=float)


def _finite(*arrays):
    ok = np.logical_and.reduce([np.isfinite(a) for a in arrays])
    return [a[ok] for a in arrays]


def _ratio_label(v: float) -> str:
    """Axis label of a power of two: 4×, 1×, 1/4×."""
    return f"{v:g}×" if v >= 1 else f"1/{1 / v:g}×"


def _ratio_value(v: float) -> str:
    """An area ratio as a number: 1.2×, 0.31×, 0.011×."""
    if not np.isfinite(v) or v <= 0:
        return "–"
    return f"{v:.2f}×" if 0.1 <= v < 10 else f"{v:.2g}×" if v < 0.1 else f"{v:,.0f}×"


# ---------------------------------------------------------------------------
# Small drawing helpers
# ---------------------------------------------------------------------------

def _no_data(ax) -> None:
    ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center", color=INK_3)
    ax.set_xticks([])
    ax.set_yticks([])


def _heading(fig, x: float, y: float, text: str, color=INK) -> None:
    fig.text(x, y, text, fontsize=10, fontweight="bold", va="baseline", color=color)


def _model_legend(fig, y: float = 0.875, x: float = 0.965) -> None:
    fig.legend([Line2D([], [], color=mc.color, linewidth=2.2) for mc in MODELS], [mc.label for mc in MODELS],
               loc="upper right", bbox_to_anchor=(x, y + 0.035), ncol=len(MODELS), fontsize=8.5,
               handlelength=1.6, columnspacing=1.4)


def _table(fig, x0: float, y0: float, widths: list[float], header: list[str], rows: list[list[str]],
           align: list[str], row_h: float = 0.026, bold_col: int | None = None) -> float:
    """Plain table drawn with figure text: header on a light panel, hairline rules. Returns its bottom."""
    width = sum(widths)
    fig.add_artist(FancyBboxPatch((x0, y0 - row_h), width, row_h, boxstyle="square,pad=0",
                                  transform=fig.transFigure, facecolor=PANEL, edgecolor="none"))
    xs = np.cumsum([0] + widths[:-1]) + x0
    pos = lambda i: xs[i] + (widths[i] - 0.006 if align[i] == "right" else 0.006)
    for i, h in enumerate(header):
        fig.text(pos(i), y0 - row_h / 2, h, ha=align[i], va="center", fontsize=7.5, fontweight="bold",
                 color=INK_2)
    y = y0 - row_h
    for r in rows:
        for i, v in enumerate(r):
            fig.text(pos(i), y - row_h / 2, v, ha=align[i], va="center", fontsize=8,
                     fontweight="bold" if i == bold_col else "normal")
        y -= row_h
        fig.add_artist(Line2D([x0, x0 + width], [y, y], color=RULE, linewidth=0.5))
    return y


def _stat_tile(fig, x: float, y: float, w: float, h: float, value: str, label: str, note: str = "") -> None:
    fig.add_artist(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.008",
                                  transform=fig.transFigure, facecolor=PANEL, edgecolor="none"))
    fig.text(x + 0.014, y + h - 0.022, label, fontsize=8, color=INK_2, va="top")
    fig.text(x + 0.014, y + 0.022, value, fontsize=22, fontweight="bold", va="baseline")
    if note:
        fig.text(x + w - 0.014, y + 0.024, note, fontsize=7.5, color=INK_3, va="baseline", ha="right")


# ---------------------------------------------------------------------------
# Cover
# ---------------------------------------------------------------------------

def _headline_rows(cases: list[Case]) -> list[list[str]]:
    rows = []
    for mc in MODELS:
        j = _finite(_values(cases, mc, "jaccard"))[0]
        s = _finite(_values(cases, mc, "sorensen"))[0]
        k = _finite(_values(cases, mc, "kappa"))[0]
        r = _values(cases, mc, "ratio_of_areas")
        r = r[np.isfinite(r) & (r > 0)]
        if not j.size:
            rows.append([mc.label, "0"] + ["–"] * 6)
            continue
        rows.append([mc.label, f"{j.size:,}", f"{np.median(j):.3f}", f"{j.mean():.3f}", f"{np.median(s):.3f}",
                     f"{np.median(k):.3f}", _ratio_value(np.median(r)) if r.size else "–",
                     f"{100 * np.mean((r >= 0.5) & (r <= 2)):.0f} %" if r.size else "–"])
    return rows


def cover_page(n_total: int, n_failed: int, ignited: list[Case], not_ignited: list[Case],
               jaccard_cutoff: float) -> plt.Figure:
    names = " and ".join(mc.label for mc in MODELS)
    fig = new_page("WildfireAV — fire spread model validation",
                   f"{names} simulations of real fires, scored against the mapped MTBS burn perimeter"
                   f"   ·   generated {date.today():%d %B %Y}")

    tiles = [(f"{n_total:,}", "Cases", ""),
             (f"{len(ignited):,}", "Scored", "ignited in at least one model"),
             (f"{len(not_ignited):,}", "Did not ignite", f"best Jaccard < {jaccard_cutoff:g}"),
             (f"{n_failed:,}", "No model output", "no arrival-time raster")]
    gap = 0.012
    w = (0.93 - 3 * gap) / 4
    for i, (v, label, note) in enumerate(tiles):
        _stat_tile(fig, 0.035 + i * (w + gap), 0.745, w, 0.12, v, label, note)

    _heading(fig, 0.035, 0.685, f"Headline scores  (the {len(ignited):,} scored cases)")
    header = ["Model", "Cases", "Median Jaccard", "Mean Jaccard", "Median Sørensen", "Median κ",
              "Median area ratio", "Area within 2×"]
    bottom = _table(fig, 0.035, 0.665, [0.12, 0.08, 0.12, 0.11, 0.13, 0.1, 0.135, 0.135], header,
                    _headline_rows(ignited), ["left"] + ["right"] * 7, row_h=0.034, bold_col=0)
    for i, mc in enumerate(MODELS):
        y = 0.665 - 0.034 * (i + 1.5)
        fig.add_artist(Line2D([0.035, 0.035], [y - 0.011, y + 0.011], color=mc.color, linewidth=3,
                              solid_capstyle="butt"))

    _heading(fig, 0.035, bottom - 0.065, "Reading the report")
    notes = [
        ("Scores", "Each model's burned cells are compared, cell by cell, with the rasterised MTBS perimeter. "
                   "Jaccard = overlap / union (0 to 1). Sørensen = 2·overlap / (simulated + observed) = 2J / (1 + J). "
                   "Cohen's κ = agreement beyond chance over the whole raster."),
        ("Area ratio", "Simulated / observed burned area: above 1× the model burned too much, below 1× too little. "
                       "\"Area within 2×\" is the share of cases between ½× and 2×."),
        ("Time window", "Every simulation starts at the satellite ignition (5 % of the fire covered by VIIRS / MODIS "
                        "hotspots) and stops when 90 % is covered, or at the reported fire-out time."),
        ("Case pages", "One page per fire: a map per model (both colours = simulated and observed, light = "
                       "simulated only, grey = observed but missed), the scores, and burned area over time "
                       "against the satellite detections."),
    ]
    y = bottom - 0.1
    for k, v in notes:
        fig.text(0.035, y, k, fontweight="bold", va="top", fontsize=8.5)
        t = fig.text(0.135, y, v, va="top", fontsize=8.5, color=INK_2, wrap=True)
        t._get_wrap_line_width = lambda: 0.82 * fig.get_size_inches()[0] * fig.dpi
        y -= 0.058
    return fig


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------

def rankings_page(cases: list[Case], jaccard_cutoff: float) -> plt.Figure:
    fig = new_page("Best and worst cases", f"Ranked by Jaccard.  Worst-case lists leave out cases where that "
                                           f"model did not ignite (Jaccard < {jaccard_cutoff:g}).")
    widths = [0.05, 0.15, 0.04, 0.055, 0.06, 0.045, 0.055]
    header = ["Case", "Fire", "Year", "Jaccard", "Sørensen", "κ", "Area ratio"]
    align = ["left", "left", "right", "right", "right", "right", "right"]
    for col, mc in enumerate(MODELS):
        scored = [c for c in cases if np.isfinite(c.score(mc, "jaccard"))]
        ranked = sorted(scored, key=lambda c: c.score(mc, "jaccard"), reverse=True)
        worst = [c for c in ranked if c.score(mc, "jaccard") >= jaccard_cutoff][::-1]
        x = 0.035 + col * 0.475
        for row, (title, subset) in enumerate(((f"{mc.label} — 10 best", ranked[:10]),
                                               (f"{mc.label} — 10 worst that ignited", worst[:10]))):
            top = 0.865 - row * 0.41
            _heading(fig, x, top, title)
            fig.add_artist(Line2D([x, x + 0.03], [top - 0.012, top - 0.012], color=mc.color, linewidth=2.5,
                                  solid_capstyle="butt"))
            rows = [[c.case_id, (c.fire_name or "")[:22], f"{c.fire_year or ''}",
                     f"{c.score(mc, 'jaccard'):.3f}", f"{c.score(mc, 'sorensen'):.3f}",
                     f"{c.score(mc, 'kappa'):.3f}", _ratio_value(c.score(mc, "ratio_of_areas"))] for c in subset]
            if rows:
                _table(fig, x, top - 0.025, widths, header, rows, align, row_h=0.028, bold_col=3)
            else:
                fig.text(x, top - 0.05, "no cases", color=INK_3)
    return fig


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------

def distribution_page(cases: list[Case]) -> plt.Figure:
    fig = new_page("Score distributions", "Top: share of cases per score bin.  Bottom: share of cases at or "
                                          "below each score; a curve further right is the better model.  "
                                          "Dashed: median.")
    _model_legend(fig)
    axes = fig.subplots(2, len(SCORES), gridspec_kw=dict(left=0.06, right=0.965, top=0.84, bottom=0.1,
                                                          hspace=0.32, wspace=0.18))
    bins = np.linspace(0, 1, 26)
    for col, (key, label) in enumerate(SCORES):
        hist, cdf = axes[0, col], axes[1, col]
        hist.set_title(label)
        top = 0.0
        for mc in MODELS:
            v = _finite(_values(cases, mc, key))[0]
            if not v.size:
                continue
            share = np.histogram(np.clip(v, 0, 1), bins)[0] / v.size
            top = max(top, share.max())
            hist.stairs(share, bins, color=mc.color, linewidth=1.4, fill=False)
            hist.stairs(share, bins, color=tint(mc.color, 0.8), fill=True, alpha=0.6)
            med = np.median(v)
            xs = np.sort(v)
            cdf.plot(np.r_[0, xs], np.r_[0, np.arange(1, xs.size + 1) / xs.size], color=mc.color,
                     drawstyle="steps-post")
            cdf.plot([med, med], [0, 0.5], color=mc.color, linewidth=0.8, linestyle=(0, (2, 2)))
            hist.axvline(med, color=mc.color, linewidth=0.8, linestyle=(0, (2, 2)))
        if top == 0:
            _no_data(hist)
            _no_data(cdf)
            continue
        hist.set_xlim(0, 1)
        hist.set_ylim(0, top * 1.12)
        hist.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{100 * v:.0f} %"))
        cdf.set_xlim(0, 1)
        cdf.set_ylim(0, 1.01)
        cdf.axhline(0.5, color=RULE, linewidth=0.8, zorder=1)
        cdf.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{100 * v:.0f} %"))
        cdf.set_xlabel(label)
        if col == 0:
            hist.set_ylabel("Share of cases")
            cdf.set_ylabel("Share of cases at or below")
    return fig


# ---------------------------------------------------------------------------
# Model against model
# ---------------------------------------------------------------------------

def model_vs_model_page(cases: list[Case]) -> plt.Figure | None:
    if len(MODELS) != 2:
        return None
    a, b = MODELS
    both = [c for c in cases if c.model_toas.get(a.label) and c.model_toas.get(b.label)]
    fig = new_page(f"{a.label} against {b.label}",
                   f"The {len(both):,} scored cases with output from both models; one dot per case.  "
                   f"Area ratio: shaded = within 2× of the observed area; beyond 1/256× – 256× drawn at the edge.")
    ax_j, ax_r = fig.subplots(1, 2, gridspec_kw=dict(left=0.07, right=0.965, top=0.84, bottom=0.1, wspace=0.22))

    ja, jb = _finite(np.array([c.score(a, "jaccard") for c in both]), np.array([c.score(b, "jaccard") for c in both]))
    ax_j.set_title("Jaccard")
    if ja.size:
        ax_j.scatter(ja, jb, s=9, color=INK, alpha=0.35, linewidths=0, zorder=3)
        ax_j.plot([0, 1], [0, 1], color=INK_3, linewidth=0.8, zorder=2)
        a_wins, b_wins = int((ja > jb).sum()), int((jb > ja).sum())
        ax_j.text(0.97, 0.04, f"{a.label} higher\n{a_wins:,} cases ({100 * a_wins / ja.size:.0f} %)",
                  transform=ax_j.transAxes, ha="right", va="bottom", color=a.color, fontweight="bold")
        ax_j.text(0.03, 0.96, f"{b.label} higher\n{b_wins:,} cases ({100 * b_wins / ja.size:.0f} %)",
                  transform=ax_j.transAxes, ha="left", va="top", color=b.color, fontweight="bold")
        ax_j.set_xlim(0, 1)
        ax_j.set_ylim(0, 1)
        ax_j.set_aspect("equal")
    else:
        _no_data(ax_j)
    ax_j.set_xlabel(f"{a.label} Jaccard")
    ax_j.set_ylabel(f"{b.label} Jaccard")

    ra, rb = _finite(np.array([c.score(a, "ratio_of_areas") for c in both]),
                     np.array([c.score(b, "ratio_of_areas") for c in both]))
    keep = (ra > 0) & (rb > 0)
    ra, rb = np.clip(ra[keep], 1 / RATIO_CLIP, RATIO_CLIP), np.clip(rb[keep], 1 / RATIO_CLIP, RATIO_CLIP)
    ax_r.set_title("Area ratio  (simulated / observed)")
    if ra.size:
        lo = min(ra.min(), rb.min(), 0.25) / 1.5
        hi = max(ra.max(), rb.max(), 4) * 1.5
        ax_r.set_xscale("log", base=2)
        ax_r.set_yscale("log", base=2)
        ax_r.axvspan(0.5, 2, color=PANEL, zorder=0)
        ax_r.axhspan(0.5, 2, color=PANEL, zorder=0)
        ax_r.axvline(1, color=INK_3, linewidth=0.8, zorder=1)
        ax_r.axhline(1, color=INK_3, linewidth=0.8, zorder=1)
        ax_r.plot([lo, hi], [lo, hi], color=RULE, linewidth=0.8, zorder=1)
        ax_r.scatter(ra, rb, s=9, color=INK, alpha=0.35, linewidths=0, zorder=3)
        ax_r.set_xlim(lo, hi)
        ax_r.set_ylim(lo, hi)
        ax_r.set_aspect("equal")
        _ratio_axis(ax_r.xaxis, lo, hi)
        _ratio_axis(ax_r.yaxis, lo, hi)
        quads = [((ra > 1) & (rb > 1), 0.97, 0.97, "both too large", "right", "top"),
                 ((ra < 1) & (rb < 1), 0.03, 0.03, "both too small", "left", "bottom")]
        for mask, x, y, text, ha, va in quads:
            ax_r.text(x, y, f"{text}: {mask.sum():,} ({100 * mask.mean():.0f} %)", transform=ax_r.transAxes,
                      ha=ha, va=va, color=INK_2, fontsize=7.5, zorder=5,
                      bbox=dict(boxstyle="square,pad=0.25", fc="white", ec="none", alpha=0.85))
    else:
        _no_data(ax_r)
    ax_r.set_xlabel(f"{a.label} area ratio")
    ax_r.set_ylabel(f"{b.label} area ratio")
    return fig


def _ratio_axis(axis, lo: float, hi: float) -> None:
    ticks = [2.0 ** e for e in range(-12, 13, 2) if lo <= 2.0 ** e <= hi]
    axis.set_major_locator(FixedLocator(ticks))
    axis.set_major_formatter(FuncFormatter(lambda v, _: _ratio_label(v)))
    axis.set_minor_locator(NullLocator())


# ---------------------------------------------------------------------------
# Area bias
# ---------------------------------------------------------------------------

def ranked_bias(ax, log2_ratio: np.ndarray, color, lo: float, hi: float, unit: str = "cases") -> None:
    """Every case as a horizontal bar of log2(simulated / observed area), sorted, largest at the top:
    too large in the model colour, too small in its light tint, within 2x shaded, stats bottom right."""
    v = np.sort(log2_ratio)
    y = np.arange(v.size)
    ax.axvspan(-1, 1, color=PANEL, zorder=0)
    over = v >= 0
    for mask, fill in ((over, color), (~over, tint(color, 0.6))):
        if mask.any():
            ax.fill_betweenx(y, 0, np.where(mask, v, 0), step="mid", color=fill, linewidth=0, zorder=2)
    ax.axvline(0, color=INK, linewidth=0.9, zorder=3)
    ax.set_xlim(lo, hi)
    ax.set_ylim(-0.5, v.size - 0.5)
    ax.set_yticks([])
    ax.grid(True, axis="x")
    ax.grid(False, axis="y")
    ticks = np.arange(np.ceil(lo / 2) * 2, hi + 0.01, 2)
    ax.set_xticks(ticks)
    ax.set_xticklabels([_ratio_label(2.0 ** t) for t in ticks])
    ax.text(0.01, 1.0, "← Too small", transform=ax.transAxes, ha="left", va="bottom", fontsize=8, color=INK_2)
    ax.text(0.99, 1.0, "Too large →", transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color=INK_2)
    stats = (f"{unit.capitalize()}  {v.size:,}\n"
             f"Median ratio  {_ratio_value(2 ** np.median(v))}\n"
             f"Too large  {100 * over.mean():.0f} %\n"
             f"Within 2×  {100 * np.mean(np.abs(v) <= 1):.0f} %")
    ax.text(0.98, 0.03, stats, transform=ax.transAxes, ha="right", va="bottom", fontsize=7.5, linespacing=1.6,
            bbox=dict(boxstyle="round,pad=0.5", fc="white", ec=RULE, lw=0.6), zorder=5)


def bias_page(cases: list[Case]) -> plt.Figure:
    fig = new_page("Burned area bias per case",
                   "Simulated / observed burned area, one bar per scored case, sorted.  Dark: too large; light: "
                   "too small; shaded: within 2×; beyond 1/256× – 256× drawn at the edge.")
    ratios = {}
    for mc in MODELS:
        r = _values(cases, mc, "ratio_of_areas")
        r = r[np.isfinite(r) & (r > 0)]
        if r.size:
            ratios[mc.label] = np.log2(np.clip(r, 1 / RATIO_CLIP, RATIO_CLIP))
    every = np.concatenate(list(ratios.values())) if ratios else np.array([0.0])
    lo, hi = min(-2.0, np.floor(every.min()) - 0.5), max(2.0, np.ceil(every.max()) + 0.5)
    axes = fig.subplots(1, len(MODELS), sharex=True,
                        gridspec_kw=dict(left=0.05, right=0.965, top=0.82, bottom=0.1, wspace=0.08))
    for ax, mc in zip(np.atleast_1d(axes), MODELS):
        ax.set_title(mc.label, pad=18)
        if mc.label in ratios:
            ranked_bias(ax, ratios[mc.label], mc.color, lo, hi)
        else:
            _no_data(ax)
        ax.set_xlabel("Simulated / observed area")
    np.atleast_1d(axes)[0].set_ylabel("Cases, ranked")
    return fig


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

def _binned_median(ax, x, y, color, log_x: bool) -> None:
    n_bins = int(min(10, x.size // MIN_PER_BIN))
    if n_bins < 2:
        return
    edges = np.unique(np.quantile(x, np.linspace(0, 1, n_bins + 1)))
    idx = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, len(edges) - 2)
    centres, med, q1, q3 = [], [], [], []
    for b in range(len(edges) - 1):
        sel = y[idx == b]
        if sel.size < 3:
            continue
        xb = x[idx == b]
        centres.append(np.exp(np.mean(np.log(xb))) if log_x else np.mean(xb))
        med.append(np.median(sel))
        q1.append(np.quantile(sel, 0.25))
        q3.append(np.quantile(sel, 0.75))
    ax.fill_between(centres, q1, q3, color=color, alpha=0.12, linewidth=0, zorder=2)
    ax.plot(centres, med, color=color, linewidth=1.8, marker="o", markersize=3, zorder=4)


def conditions_page(cases: list[Case]) -> plt.Figure:
    fig = new_page("Scores against fire conditions",
                   "Dots: cases.  Lines: median of cases binned by the x value (equal-count bins); band: "
                   "middle 50 %.  ρ: Spearman rank correlation.  Area ratio: shaded = within 2×.")
    _model_legend(fig)
    columns = [("avg_wind_kph", "Mean wind speed (km/h, ERA5 10 m)", False),
               ("observed_acres", "Observed burned area (acres)", True),
               ("duration_h", "Simulated duration (hours)", True)]
    rows = [("jaccard", "Jaccard"), ("ratio_of_areas", "Area ratio (sim / obs)")]
    axes = fig.subplots(len(rows), len(columns), gridspec_kw=dict(left=0.07, right=0.965, top=0.83, bottom=0.1,
                                                                   hspace=0.3, wspace=0.16))
    for col, (xkey, xlabel, log_x) in enumerate(columns):
        for row, (ykey, ylabel) in enumerate(rows):
            ax = axes[row, col]
            ratio = ykey == "ratio_of_areas"
            texts = []
            for mc in MODELS:
                x, y = _finite(_values(cases, mc, xkey), _values(cases, mc, ykey))
                keep = (x > 0) if log_x else np.ones_like(x, bool)
                if ratio:
                    keep &= y > 0
                x, y = x[keep], y[keep]
                if ratio:
                    y = np.clip(y, 1 / 64, 64)
                if not x.size:
                    continue
                ax.scatter(x, y, s=4, color=mc.color, alpha=0.18, linewidths=0, zorder=3)
                _binned_median(ax, x, y, mc.color, log_x)
                rho = pd.Series(x).corr(pd.Series(y), method="spearman") if x.size > 2 else np.nan
                texts.append((mc, rho))
            if log_x:
                ax.set_xscale("log")
            if ratio:
                ax.set_yscale("log", base=2)
                ax.axhspan(0.5, 2, color=PANEL, zorder=0)
                ax.axhline(1, color=INK_3, linewidth=0.8, zorder=1)
                ax.set_ylim(1 / 64, 64)
                _ratio_axis(ax.yaxis, 1 / 64, 64)
            else:
                ax.set_ylim(0, 1)
            for k, (mc, rho) in enumerate(texts):
                ax.text(0.98, 0.97 - 0.075 * k, f"ρ = {rho:+.2f}", transform=ax.transAxes, ha="right", va="top",
                        fontsize=7.5, color=mc.color, fontweight="bold",
                        bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none", alpha=0.8))
            if not texts:
                _no_data(ax)
            if col == 0:
                ax.set_ylabel(ylabel)
            if row == len(rows) - 1:
                ax.set_xlabel(xlabel)
    return fig


def summary_pages(cases: list[Case], jaccard_cutoff: float) -> list[plt.Figure]:
    pages = [rankings_page(cases, jaccard_cutoff), distribution_page(cases), model_vs_model_page(cases),
             bias_page(cases), conditions_page(cases)]
    return [p for p in pages if p is not None]
