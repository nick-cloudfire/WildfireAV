"""
Look of the validation report: page size, fonts, colours and the page frame
(header and footer) shared by the summary pages and the case pages.

Model colours live with the models (caseMetrics.MODELS); everything else that
is drawn in a fixed colour is named here.
"""

from __future__ import annotations

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgb

A4_LANDSCAPE = (11.69, 8.27)
PDF_DPI      = 300           # for the few rasterised layers; text and lines stay vector

INK      = "#1d1d1b"         # primary text, observed perimeter
INK_2    = "#55534e"         # secondary text, labels
INK_3    = "#8a8780"         # muted text, notes
RULE     = "#d9d7d0"         # hairlines, table rules
GRID     = "#ebe9e3"
PANEL    = "#f6f5f1"         # stat tiles, table header fill
MISSED   = "#d6d3ca"         # observed but not simulated (map)
BARRIER  = "#a3a097"         # roads / rivers (map)
SATELLITE = INK              # satellite coverage curve

matplotlib.rcParams.update({
    "pdf.fonttype":       42,              # embed TrueType: text stays selectable
    "image.composite_image": False,        # keep each map layer at its own resolution
    "font.family":        "DejaVu Sans",   # ships with matplotlib, so identical everywhere
    "font.size":          8.5,
    "axes.titlesize":     9.5,
    "axes.titleweight":   "bold",
    "axes.titlelocation": "left",
    "axes.titlepad":      6,
    "axes.labelsize":     8.5,
    "axes.labelcolor":    INK_2,
    "axes.edgecolor":     RULE,
    "axes.linewidth":     0.8,
    "axes.grid":          True,
    "axes.axisbelow":     True,
    "axes.spines.top":    False,
    "axes.spines.right":  False,
    "grid.color":         GRID,
    "grid.linewidth":     0.6,
    "xtick.color":        INK_2,
    "ytick.color":        INK_2,
    "xtick.labelsize":    7.5,
    "ytick.labelsize":    7.5,
    "xtick.major.size":   3,
    "ytick.major.size":   3,
    "xtick.major.width":  0.6,
    "ytick.major.width":  0.6,
    "legend.fontsize":    7.5,
    "legend.frameon":     False,
    "lines.linewidth":    1.6,
    "text.color":         INK,
})


def tint(rgb, amount: float) -> tuple[float, float, float]:
    """rgb mixed with white: amount 0 = rgb, 1 = white."""
    r, g, b = to_rgb(rgb)
    return (r + (1 - r) * amount, g + (1 - g) * amount, b + (1 - b) * amount)


def shade(rgb, amount: float) -> tuple[float, float, float]:
    """rgb mixed with black: amount 0 = rgb, 1 = black."""
    r, g, b = to_rgb(rgb)
    return (r * (1 - amount), g * (1 - amount), b * (1 - amount))


def new_page(title: str, subtitle: str = "") -> plt.Figure:
    """A4 landscape page with the report header (title, subtitle, rule)."""
    fig = plt.figure(figsize=A4_LANDSCAPE)
    fig.text(0.035, 0.955, title, fontsize=15, fontweight="bold", va="baseline")
    if subtitle:
        fig.text(0.035, 0.925, subtitle, fontsize=9, color=INK_2, va="baseline")
    fig.add_artist(plt.Line2D([0.035, 0.965], [0.905, 0.905], color=RULE, linewidth=0.8))
    return fig


def add_footer(fig: plt.Figure, left: str, page: int, n_pages: int) -> None:
    fig.text(0.035, 0.022, left, fontsize=7, color=INK_3)
    fig.text(0.965, 0.022, f"{page} / {n_pages}", fontsize=7, color=INK_3, ha="right")
