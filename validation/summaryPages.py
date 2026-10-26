"""
Report pages that summarise all cases:

    cover       case counts and the 10 best / worst cases of each model
    wind        scores against mean wind speed, one column per model
    size        scores against burned area and simulated duration (log axes)
    bias        simulated / observed area of every case, log2 scale
    histograms  distribution of each score, one row per model

Only cases that ignited in at least one model are summarised
(see getValidationPDF.NOT_IGNITED_JACCARD).
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

from caseMetrics import M2_TO_ACRES, MODELS, Case
from casePage import A4_LANDSCAPE

SCORES = [("jaccard", "Jaccard"), ("sorensen", "Sørensen"), ("kappa", "Cohen's κ")]


def _figure(rows: int, cols: int, title: str):
    fig, axes = plt.subplots(rows, cols, figsize=A4_LANDSCAPE, squeeze=False)
    fig.suptitle(title, fontsize=13, fontweight="bold", y=0.99)
    return fig, axes


def _no_data(ax) -> None:
    ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center", color="gray")
    ax.set_xticks([])
    ax.set_yticks([])


def _values(cases: list[Case], model, attr_or_score: str) -> np.ndarray:
    """One value per case that has this model's output: a Case attribute or a score."""
    out = []
    for c in cases:
        if not c.model_toas.get(model.label):
            continue
        if attr_or_score == "observed_acres":
            out.append(c.observed_m2 * M2_TO_ACRES)
        elif attr_or_score in ("duration_h", "avg_wind_kph"):
            out.append(getattr(c, attr_or_score) or np.nan)
        else:
            out.append(c.score(model, attr_or_score))
    return np.asarray(out, dtype=float)


def _fit_line(ax, x, y, color, log_x: bool = False, y_pos: float = 0.04) -> None:
    """Least-squares line (in log10 x if log_x) with its R² and equation."""
    if x.size < 3:
        return
    xf = np.log10(x) if log_x else x
    slope, intercept = np.polyfit(xf, y, 1)
    r2 = np.corrcoef(xf, y)[0, 1] ** 2
    xs = np.sort(x)
    ax.plot(xs, slope * (np.log10(xs) if log_x else xs) + intercept,
            color=color, linewidth=1.2, linestyle="--", alpha=0.85, zorder=4)
    eq = f"y={slope:+.3f}·log₁₀(x){intercept:+.3f}" if log_x else f"y={slope:+.4f}·x{intercept:+.3f}"
    ax.text(0.02, y_pos, f"R²={r2:.3f}  {eq}", transform=ax.transAxes, fontsize=6.5, color=color,
            va="bottom", bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color, alpha=0.7, lw=0.6))


def cover_page(n_total: int, n_failed: int, ignited: list[Case], not_ignited: list[Case],
               jaccard_cutoff: float) -> plt.Figure:
    fig = plt.figure(figsize=A4_LANDSCAPE)
    fig.suptitle("Validation Report — Summary", fontsize=14, fontweight="bold", y=0.99)
    grid = fig.add_gridspec(3, len(MODELS), height_ratios=[0.10, 0.45, 0.45], hspace=0.45,
                            wspace=0.12, left=0.04, right=0.98, top=0.93, bottom=0.02)
    ax = fig.add_subplot(grid[0, :])
    ax.axis("off")
    ax.text(0.5, 0.5,
            f"Cases: {n_total}     Evaluated: {len(ignited)}     "
            f"Did not ignite (Jaccard < {jaccard_cutoff:.2f}): {len(not_ignited)}     "
            f"No model output: {n_failed}",
            ha="center", va="center", fontsize=10, family="monospace", transform=ax.transAxes,
            bbox=dict(boxstyle="round,pad=0.4", facecolor="#e8e8e8", linewidth=0))

    header = ["Case", "Fire", "Jaccard", "Sørensen", "κ"]
    for col, mc in enumerate(MODELS):
        ranked = sorted((c for c in ignited if np.isfinite(c.score(mc, "jaccard"))),
                        key=lambda c: c.score(mc, "jaccard"), reverse=True)
        for row, (title, subset) in enumerate((("Top 10 by Jaccard", ranked[:10]),
                                               ("Bottom 10 by Jaccard", ranked[::-1][:10])), start=1):
            ax = fig.add_subplot(grid[row, col])
            ax.axis("off")
            ax.set_title(f"{mc.label} — {title}", fontsize=9, fontweight="bold", color=mc.color, pad=3)
            if not subset:
                _no_data(ax)
                continue
            cells = [[c.case_id, (c.fire_name or "")[:16]] + [f"{c.score(mc, k):.3f}" for k, _ in SCORES]
                     for c in subset]
            table = ax.table(cellText=cells, colLabels=header, loc="center", cellLoc="center",
                             bbox=[0, 0, 1, 1])
            table.auto_set_font_size(False)
            table.set_fontsize(7.5)
            for (r, _), cell in table.get_celld().items():
                cell.set_facecolor("#b0b0b0" if r == 0 else "#f0f0f0" if r % 2 == 0 else "white")
                if r == 0:
                    cell.set_text_props(fontweight="bold")
    return fig


def wind_page(cases: list[Case]) -> plt.Figure:
    fig, axes = _figure(len(SCORES), len(MODELS), "Scores vs. Mean Wind Speed (ERA5 10 m, km/h)")
    for col, mc in enumerate(MODELS):
        x = _values(cases, mc, "avg_wind_kph")
        for row, (key, label) in enumerate(SCORES):
            ax = axes[row, col]
            ax.set_ylim(-0.05, 1.05)
            ax.grid(True, alpha=0.3, linewidth=0.5)
            ax.set_ylabel(label if col == 0 else "")
            ax.set_title(mc.label if row == 0 else "")
            if row == len(SCORES) - 1:
                ax.set_xlabel("Mean wind speed (km/h)")
            y = _values(cases, mc, key)
            ok = np.isfinite(x) & np.isfinite(y)
            if not ok.any():
                _no_data(ax)
                continue
            ax.scatter(x[ok], y[ok], color=mc.color, alpha=0.65, s=18, zorder=3)
            _fit_line(ax, x[ok], y[ok], mc.color)
            ax.text(0.98, 0.95, f"n={ok.sum()}", transform=ax.transAxes, ha="right", va="top", fontsize=8)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def size_page(cases: list[Case]) -> plt.Figure:
    columns = [("observed_acres", "Observed burned area (acres)"), ("duration_h", "Simulated duration (hours)")]
    fig, axes = _figure(len(SCORES), len(columns), "Scores vs. Fire Size and Duration")
    for col, (x_key, x_label) in enumerate(columns):
        for row, (key, label) in enumerate(SCORES):
            ax = axes[row, col]
            ax.set_ylim(-0.05, 1.05)
            ax.set_xscale("log")
            ax.grid(True, alpha=0.3, linewidth=0.5)
            ax.set_ylabel(label if col == 0 else "")
            ax.set_title(x_label.split(" (")[0] if row == 0 else "")
            if row == len(SCORES) - 1:
                ax.set_xlabel(x_label)
            y_pos = 0.04                     # stack the fit labels of the models
            for mc in MODELS:
                x, y = _values(cases, mc, x_key), _values(cases, mc, key)
                ok = np.isfinite(x) & np.isfinite(y) & (x > 0)
                if ok.any():
                    ax.scatter(x[ok], y[ok], color=mc.color, alpha=0.65, s=18, zorder=3,
                               label=f"{mc.label} (n={ok.sum()})")
                    _fit_line(ax, x[ok], y[ok], mc.color, log_x=True, y_pos=y_pos)
                    y_pos += 0.12
            if row == 0 and col == 0:
                ax.legend(fontsize=7, loc="lower right")
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def bias_page(cases: list[Case]) -> plt.Figure:
    """Horizontal bar per case of log2(simulated / observed area), sorted; shared x axis."""
    log2 = {}
    for mc in MODELS:
        ratio = _values(cases, mc, "ratio_of_areas")
        ratio = ratio[np.isfinite(ratio) & (ratio > 0)]
        if ratio.size:
            log2[mc.label] = np.sort(np.log2(ratio))
    every = np.concatenate(list(log2.values())) if log2 else np.array([0.0])
    lo = min(-2.0, 1.2 * every.min())        # always show at least 1/4x .. 4x
    hi = max(2.0, 1.2 * every.max())
    ticks = np.arange(np.ceil(lo), np.floor(hi) + 1).astype(int)

    fig, axes = _figure(1, len(MODELS), "Burned Area Bias per Case   (simulated / observed, log₂ scale)")
    for ax, mc in zip(axes[0], MODELS):
        ax.set_title(mc.label)
        ax.set_xlim(lo, hi)
        if mc.label not in log2:
            _no_data(ax)
            continue
        v = log2[mc.label]
        ax.barh(np.arange(v.size), v, height=0.85,
                color=[(*mc.color, 0.75) if x >= 0 else (0.5, 0.5, 0.5, 0.65) for x in v])
        ax.axvline(0, color="black", linewidth=1.0, zorder=5)
        for t in ticks[ticks != 0]:
            ax.axvline(t, color="gray", linestyle=":", linewidth=0.7, alpha=0.55, zorder=3)
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{2 ** t:.0f}×" if t >= 0 else f"1/{2 ** -t:.0f}×" for t in ticks], fontsize=9)
        ax.set_xlabel("simulated / observed area")
        ax.set_yticks([])
        ax.grid(True, axis="x", alpha=0.25, linewidth=0.5)
        over = int((v > 0).sum())
        ax.text(0.98, 0.01, f"n = {v.size}\nmedian ratio = {np.median(2 ** v):.2f}×\n"
                            f"overestimated: {over}/{v.size} ({100 * over / v.size:.0f}%)",
                transform=ax.transAxes, fontsize=7, ha="right", va="bottom",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.8, linewidth=0.6))
        ax.legend(handles=[Patch(color=mc.color, alpha=0.8, label="Overestimate (ratio > 1)"),
                           Patch(color=(0.5, 0.5, 0.5), alpha=0.75, label="Underestimate (ratio < 1)")],
                  fontsize=7, loc="upper left")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return fig


def histogram_page(cases: list[Case]) -> plt.Figure:
    fig, axes = _figure(len(MODELS), len(SCORES), "Score Distributions")
    for row, mc in enumerate(MODELS):
        for col, (key, label) in enumerate(SCORES):
            ax = axes[row, col]
            ax.set_xlim(-0.05, 1.05)
            ax.grid(True, alpha=0.3, axis="y")
            ax.set_title(label if row == 0 else "")
            ax.set_ylabel(f"{mc.label}\nCases" if col == 0 else "")
            if row == len(MODELS) - 1:
                ax.set_xlabel("Score")
            v = _values(cases, mc, key)
            v = v[np.isfinite(v)]
            if not v.size:
                _no_data(ax)
                continue
            ax.hist(v, bins=min(25, max(5, v.size // 3)), range=(0, 1),
                    color=mc.color, alpha=0.75, edgecolor="white", linewidth=0.5)
            ax.axvline(v.mean(), color="black", linestyle="--", linewidth=1.2, label=f"mean={v.mean():.3f}")
            ax.axvline(np.median(v), color="dimgray", linestyle=":", label=f"median={np.median(v):.3f}")
            ax.legend(fontsize=7, loc="upper left")
            ax.text(0.98, 0.95, f"n={v.size}", transform=ax.transAxes, ha="right", va="top", fontsize=8)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    return fig


def summary_pages(cases: list[Case]) -> list[plt.Figure]:
    return [wind_page(cases), size_page(cases), bias_page(cases), histogram_page(cases)]
