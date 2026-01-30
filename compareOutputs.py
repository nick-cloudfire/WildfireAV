# -*- coding: utf-8 -*-
"""
Batch comparison of many wildfire simulations + better visualization dashboard.

Each simulation folder must contain:
 - firescar.shp
 - outputs/time_of_arrival_*.tif

Metrics:
- Jaccard
- Sorensen (Dice)
- Kappa (area-based, with explicit domain)
- Ratio of Areas (sim/obs)

Visualizations (many-cases friendly):
1) Observed vs Simulated acres (log-log) with 1:1 line
2) Boxplots by observed-size bins for Jaccard / Sorensen / Kappa
3) Hexbin density plots for metric vs size
4) Top/Bottom N runs bar charts
5) Optional composite score + score vs size
"""

# =========================
# CONFIGURATION
# =========================

from pathlib import Path

# Folder containing many simulation subfolders:
ROOT_DIR = Path(r"\\wsl.localhost\Ubuntu-24.04\home\nick\elmfire_validation")  # <-- CHANGE THIS

RASTER_PATTERN = "time_of_arrival_*.tif"
BURN_THRESHOLD = 1
RASTER_BAND = 1

# Area conversion
M2_TO_ACRES = 0.000247105381  # exact conversion

# Plot behavior
EXCLUDE_ZERO_CASES_IN_PLOTS = True   # when True, removes runs with all similarity metrics == 0 from some plots
TOP_N = 15                            # for best/worst plots

# Observed acres bins (edit as desired)
SIZE_BINS_ACRES = [0, 10, 30, 100, 300, 1_000, 3_000, 10_000, 30_000, 100_000, float("inf")]


# =========================
# IMPORTS
# =========================

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape, GeometryCollection
from shapely.ops import unary_union
from pyproj import CRS
import pandas as pd
import matplotlib.pyplot as plt


# =========================
# HELPER FUNCTIONS (GEOMETRY / METRICS)
# =========================

def choose_utm_crs(geom):
    """Choose a reasonable UTM CRS based on geometry centroid."""
    lon, lat = geom.centroid.x, geom.centroid.y
    zone = int((lon + 180) // 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)

def is_no_result(df: pd.DataFrame) -> pd.Series:
    """
    Identify simulations with no usable results.
    """
    return (
        (df["simulated_area_m2"] == 0) &
        (df["jaccard"] == 0) &
        (df["sorensen"] == 0) &
        (df["kappa"] == 0)
    )

def area(geom):
    """Return area of geometry, 0.0 if empty or None."""
    if geom is None or geom.is_empty:
        return 0.0
    return geom.area


def read_shapefile_polygon(path: Path) -> gpd.GeoSeries:
    """
    Read a shapefile and return a single merged polygon GeoSeries.
    Raises if nothing is in the file.
    """
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"No geometries in {path}")
    merged = unary_union(gdf.geometry)
    return gpd.GeoSeries([merged], crs=gdf.crs)


def raster_to_burned_polygon(path: Path, band: int = 1, threshold: float = 1.0) -> gpd.GeoSeries:
    """
    Convert a raster to a single burned-area polygon (union of all burned cells).
    Returns an empty GeometryCollection if no burned pixels.
    """
    with rasterio.open(path) as src:
        arr = src.read(band)
        burned = arr >= threshold

        # No burned pixels at all
        if not burned.any():
            return gpd.GeoSeries([GeometryCollection()], crs=src.crs)

        geoms = [
            shape(g)
            for g, v in shapes(
                burned.astype(np.uint8),
                mask=burned,
                transform=src.transform
            ) if v == 1
        ]

        if not geoms:
            return gpd.GeoSeries([GeometryCollection()], crs=src.crs)

        merged = unary_union(geoms)
        return gpd.GeoSeries([merged], crs=src.crs)


def safe_divide(num, denom):
    """Return num/denom, or 0 if denom is 0."""
    return 0.0 if denom == 0 else num / denom


def jaccard(A, B):
    inter = area(A.intersection(B))
    union = area(A.union(B))
    return safe_divide(inter, union)


def sorensen(A, B):
    inter = area(A.intersection(B))
    denom = area(A) + area(B)
    return safe_divide(2 * inter, denom)


def kappa(A, B, domain):
    """
    Simple area-based Kappa, using an explicit domain polygon.
    Note: if domain has zero area, or (1 - pe) == 0, returns 0.0.
    """
    tp = area(A.intersection(B))
    fp = area(B.difference(A))
    fn = area(A.difference(B))
    tn = area(domain.difference(A.union(B)))

    total = tp + fp + fn + tn
    if total == 0:
        return 0.0

    pa = (tp + tn) / total
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (total ** 2)

    denom = 1 - pe
    if denom == 0:
        return 0.0

    return (pa - pe) / denom


def ratio_of_areas(A, B):
    """Simulated / observed area; returns 0 if observed is 0."""
    a = area(A)  # observed
    b = area(B)  # simulated
    return safe_divide(b, a)


# =========================
# MAIN PROCESSING
# =========================

def process_all_simulations(root_dir: Path) -> pd.DataFrame:
    results_list = []

    for sim_folder in sorted(root_dir.iterdir()):
        if not sim_folder.is_dir():
            continue

        firescar = sim_folder / "firescar.shp"
        outputs_dir = sim_folder / "outputs"

        # Must have a firescar file
        if not firescar.exists():
            print(f"Skipping {sim_folder.name}: no firescar.shp")
            continue

        # Look for TOA rasters (sorted by modification time)
        rasters = []
        if outputs_dir.exists():
            rasters = sorted(
                outputs_dir.glob(RASTER_PATTERN),
                key=lambda p: p.stat().st_mtime
            )

        # Load observed polygon (always, so observed area is recorded)
        obs = read_shapefile_polygon(firescar)
        obs_ll = obs.to_crs(4326)
        target_crs = choose_utm_crs(obs_ll.iloc[0])
        A = obs_ll.to_crs(target_crs).iloc[0]

        # -----------------------------
        # CASE 1: Missing TOA raster
        # -----------------------------
        if not rasters:
            print(f"No TOA raster found in {sim_folder.name} — setting simulated area + similarity indices = 0")

            metrics = {
                "simulation_folder": sim_folder.name,
                "observed_area_m2": area(A),
                "simulated_area_m2": 0.0,
                "jaccard": 0.0,
                "sorensen": 0.0,
                "kappa": 0.0,
                "ratio_of_areas": 0.0,
                "toa_raster_used": None,
            }
            results_list.append(metrics)
            continue

        # -----------------------------
        # CASE 2: Normal processing
        # -----------------------------
        raster_file = rasters[-1]  # newest raster

        # Load simulation raster -> burned polygon
        sim = raster_to_burned_polygon(raster_file, band=RASTER_BAND, threshold=BURN_THRESHOLD)
        sim_ll = sim.to_crs(4326)
        B = sim_ll.to_crs(target_crs).iloc[0]

        # Domain: A ∪ B (buffer(0) cleans geometry)
        DOMAIN = A.union(B).buffer(0)

        metrics = {
            "simulation_folder": sim_folder.name,
            "observed_area_m2": area(A),
            "simulated_area_m2": area(B),
            "jaccard": jaccard(A, B),
            "sorensen": sorensen(A, B),
            "kappa": kappa(A, B, DOMAIN),
            "ratio_of_areas": ratio_of_areas(A, B),
            "toa_raster_used": str(raster_file),
        }

        results_list.append(metrics)
        print(f"Processed {sim_folder.name}")

    df = pd.DataFrame(results_list)

    # Add acre columns
    df["observed_acres"] = df["observed_area_m2"] * M2_TO_ACRES
    df["simulated_acres"] = df["simulated_area_m2"] * M2_TO_ACRES

    return df


# =========================
# VISUALIZATION HELPERS
# =========================

def filter_zero_similarity(df: pd.DataFrame) -> pd.DataFrame:
    """Remove runs where all similarity metrics are 0. Keeps area ratio info in full df."""
    return df[
        (df["jaccard"] > 0) |
        (df["sorensen"] > 0) |
        (df["kappa"] != 0)
    ].copy()


def add_size_bins(df: pd.DataFrame, bins=None) -> pd.DataFrame:
    """Add categorical size bins based on observed acres."""
    if bins is None:
        bins = SIZE_BINS_ACRES

    labels = []
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        labels.append(f"{lo:g}-{hi:g}" if np.isfinite(hi) else f">{lo:g}")

    out = df.copy()
    out["size_bin"] = pd.cut(out["observed_acres"], bins=bins, labels=labels, include_lowest=True)
    return out


def plot_observed_vs_simulated(df: pd.DataFrame):
    """Observed vs simulated acres (log-log) with 1:1 line."""
    d = df.copy()
    d = d[(d["observed_acres"] > 0) & (d["simulated_acres"] > 0)]

    if d.empty:
        print("No positive-acre cases to plot observed vs simulated.")
        return

    x = d["observed_acres"].values
    y = d["simulated_acres"].values

    plt.figure(figsize=(7, 7))
    plt.scatter(x, y, s=40, edgecolors="black")
    plt.xscale("log")
    plt.yscale("log")

    lo = min(x.min(), y.min())
    hi = max(x.max(), y.max())
    plt.plot([lo, hi], [lo, hi])

    plt.xlabel("Observed acres (log)")
    plt.ylabel("Simulated acres (log)")
    plt.title("Observed vs Simulated Fire Size (1:1 line)")
    plt.grid(True)
    plt.tight_layout()
    plt.show()


def plot_metric_by_size_bin(df: pd.DataFrame, metric: str, exclude_zero=True, bins=None):
    """Boxplot of a metric grouped by observed acres bins."""
    d = df.copy()
    if exclude_zero:
        d = d[d[metric] > 0]

    d = d[d["observed_acres"] > 0]
    d = add_size_bins(d, bins=bins)

    groups = []
    xticks = []
    for b in d["size_bin"].cat.categories:
        vals = d.loc[d["size_bin"] == b, metric].dropna().values
        if len(vals) == 0:
            continue
        groups.append(vals)
        xticks.append(str(b))

    if not groups:
        print(f"No data to plot {metric} by size bin.")
        return

    plt.figure(figsize=(12, 5))
    plt.boxplot(groups, showfliers=False)
    plt.xticks(range(1, len(xticks) + 1), xticks, rotation=45, ha="right")
    plt.ylabel(metric)
    if metric in ("jaccard", "sorensen"):
        plt.ylim(0, 1)
    plt.title(f"{metric} distribution by observed fire size bin (acres)")
    plt.grid(True, axis="y")
    plt.tight_layout()
    plt.show()


def hexbin_metric_vs_size(df: pd.DataFrame, metric: str, exclude_zero=True):
    """Hexbin density plot for metric vs observed acres (log x-scale)."""
    d = df.copy()
    d = d[d["observed_acres"] > 0]
    if exclude_zero:
        d = d[d[metric] > 0]

    d = d.dropna(subset=[metric])
    if d.empty:
        print(f"No data to plot hexbin for {metric}.")
        return

    plt.figure(figsize=(10, 6))
    plt.hexbin(
        d["observed_acres"],
        d[metric],
        gridsize=35,
        xscale="log",
        mincnt=1
    )
    if metric in ("jaccard", "sorensen"):
        plt.ylim(0, 1)
    plt.xlabel("Observed acres (log)")
    plt.ylabel(metric)
    plt.title(f"{metric} vs observed acres (hexbin density)")
    cb = plt.colorbar()
    cb.set_label("count")
    plt.tight_layout()
    plt.show()


def plot_top_bottom(
    df: pd.DataFrame,
    metric: str,
    n: int = 15,
    exclude_no_result: bool = True,
    prefer_nonzero_for_worst: bool = True
):
    """
    Bar charts for top and bottom N runs by a metric.

    - exclude_no_result: drops simulations that were defaulted to 0 (missing raster / no burned pixels)
      Requires df to contain boolean column 'no_result'. If absent, it will just ignore this filter.
    - prefer_nonzero_for_worst: for metrics like Jaccard/Sørensen, worst is often 0.
      This prefers the worst *non-zero* set if available, otherwise falls back.
    - epsilon + annotations prevent "blank" plots when values are 0.
    """
    d = df.copy().dropna(subset=[metric])

    # Exclude no-result runs if the flag exists
    if exclude_no_result and "no_result" in d.columns:
        d = d[~d["no_result"]]

    if d.empty:
        print(f"No data to plot for {metric} after filtering.")
        return

    best = d.sort_values(metric, ascending=False).head(n)

    if prefer_nonzero_for_worst:
        d_nonzero = d[d[metric] != 0]
        worst = (d_nonzero.sort_values(metric, ascending=True).head(n)
                 if not d_nonzero.empty else
                 d.sort_values(metric, ascending=True).head(n))
    else:
        worst = d.sort_values(metric, ascending=True).head(n)

    def _plot(panel_df, title):
        if panel_df.empty:
            print(f"No rows for {title}")
            return

        vals = panel_df[metric].astype(float).values
        eps = 1e-3
        disp = np.where(vals == 0.0, eps, vals)

        plt.figure(figsize=(10, 6))
        plt.barh(panel_df["simulation_folder"], disp)
        plt.gca().invert_yaxis()

        if metric in ("jaccard", "sorensen"):
            plt.xlim(0, 1)

        # annotate true values
        x0, x1 = plt.gca().get_xlim()
        span = (x1 - x0) if (x1 - x0) != 0 else 1.0
        for y, (true_v, disp_v) in enumerate(zip(vals, disp)):
            plt.text(disp_v + 0.01 * span, y, f"{true_v:.3f}", va="center")

        plt.xlabel(metric)
        plt.title(title)
        plt.grid(True, axis="x")
        plt.tight_layout()
        plt.show()

    _plot(best, f"Top {n} simulations by {metric}")
    suffix = " (worst non-zero preferred)" if prefer_nonzero_for_worst else ""
    _plot(worst, f"Worst {n} simulations by {metric}{suffix}")


def add_composite_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    Composite score (optional) for sorting/triage.

    - Scales kappa roughly into [0,1] by mapping [-1,1] -> [0,1]
    - Penalizes area ratio away from 1 using symmetric log penalty abs(log(ratio))
    """
    d = df.copy()

    k = d["kappa"].clip(-1, 1)
    k01 = (k + 1) / 2

    eps = 1e-9
    ratio = d["ratio_of_areas"].astype(float) + eps
    area_penalty = np.abs(np.log(ratio))  # 0 if ratio=1, symmetric for over/under

    d["score"] = (
        0.45 * d["jaccard"].fillna(0) +
        0.35 * d["sorensen"].fillna(0) +
        0.20 * k01.fillna(0)
        - 0.15 * area_penalty.fillna(0)
    )
    return d


def plot_score_vs_size(df: pd.DataFrame):
    d = add_composite_score(df)
    d = d[d["observed_acres"] > 0]

    if d.empty:
        print("No data to plot score vs size.")
        return

    plt.figure(figsize=(10, 6))
    plt.scatter(d["observed_acres"], d["score"], s=50, edgecolors="black")
    plt.xscale("log")
    plt.xlabel("Observed acres (log)")
    plt.ylabel("Composite score")
    plt.title("Composite performance score vs observed fire size")
    plt.grid(True)
    plt.tight_layout()
    plt.show()


def make_dashboard(df: pd.DataFrame, exclude_zero_cases=True, top_n=15):
    """
    Generate a set of plots that work well for many cases.
    """
    print("\n=== SUMMARY TABLE (first 25 rows) ===")
    cols = ["simulation_folder", "observed_acres", "simulated_acres", "jaccard", "sorensen", "kappa", "ratio_of_areas"]
    print(df[cols].head(25).to_string(index=False))

    d_plot = df
    if exclude_zero_cases:
        d_plot = filter_zero_similarity(df)

    # 1) Observed vs simulated size
    plot_observed_vs_simulated(df)

    # 2) Metric distributions by size bin
    plot_top_bottom(df_valid, metric="jaccard", n=TOP_N, exclude_no_result=True, prefer_nonzero_for_worst=True)
    plot_top_bottom(df_valid, metric="sorensen", n=TOP_N, exclude_no_result=True, prefer_nonzero_for_worst=True)
    plot_top_bottom(df_valid, metric="kappa",   n=TOP_N, exclude_no_result=True, prefer_nonzero_for_worst=False)

    # 3) Hexbin density plots
    for metric in ["jaccard", "sorensen", "kappa"]:
        hexbin_metric_vs_size(d_plot, metric=metric, exclude_zero=False)

    # 4) Top/bottom runs
    for metric in ["jaccard", "sorensen", "kappa"]:
        plot_top_bottom(df, metric=metric, n=top_n)

    # 5) Composite score (optional)
    plot_score_vs_size(df)
    df_scored = add_composite_score(df).sort_values("score", ascending=False)

    print("\n=== TOP 15 BY COMPOSITE SCORE ===")
    print(df_scored[cols + ["score"]].head(15).to_string(index=False))

    print("\n=== BOTTOM 15 BY COMPOSITE SCORE ===")
    print(df_scored[cols + ["score"]].tail(15).to_string(index=False))


# =========================
# OPTIONAL: SINGLE-CASE POLYGON OVERLAY PLOT
# =========================

def plot_burn_scar_comparison(
    sim_folder: Path,
    raster_pattern: str = RASTER_PATTERN,
    band: int = RASTER_BAND,
    threshold: float = BURN_THRESHOLD,
):
    """
    Plot and compare predicted (simulated) and actual (observed) burn scars
    for a single simulation folder.
    """
    firescar = sim_folder / "firescar.shp"
    outputs_dir = sim_folder / "outputs"

    if not firescar.exists():
        raise FileNotFoundError(f"No firescar.shp found in {sim_folder}")

    if not outputs_dir.exists():
        raise FileNotFoundError(f"No outputs/ directory found in {sim_folder}")

    rasters = sorted(outputs_dir.glob(raster_pattern), key=lambda p: p.stat().st_mtime)
    if not rasters:
        raise FileNotFoundError(f"No rasters matching '{raster_pattern}' found in {outputs_dir}")

    raster_file = rasters[-1]

    obs = read_shapefile_polygon(firescar)
    obs_ll = obs.to_crs(4326)

    sim = raster_to_burned_polygon(raster_file, band=band, threshold=threshold)
    sim_ll = sim.to_crs(4326)

    target_crs = choose_utm_crs(obs_ll.iloc[0])
    obs_utm = obs_ll.to_crs(target_crs)
    sim_utm = sim_ll.to_crs(target_crs)

    gdf_obs = gpd.GeoDataFrame(geometry=obs_utm, crs=target_crs)
    gdf_sim = gpd.GeoDataFrame(geometry=sim_utm, crs=target_crs)

    fig, ax = plt.subplots(figsize=(8, 8))

    gdf_obs.plot(
        ax=ax,
        facecolor="none",
        edgecolor="black",
        linewidth=1.5,
        label="Observed burn scar",
    )

    gdf_sim.plot(
        ax=ax,
        facecolor="none",
        edgecolor="red",
        linewidth=1.5,
        linestyle="--",
        label="Predicted burn scar",
    )

    ax.set_aspect("equal", "box")
    ax.set_title(f"Observed vs Predicted Burn Scar\n{sim_folder.name}")
    ax.legend()
    ax.grid(True)

    plt.tight_layout()
    plt.show()
    
def add_no_result_flag(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["no_result"] = (
        (d["simulated_area_m2"] == 0) &
        (d["jaccard"] == 0) &
        (d["sorensen"] == 0) &
        (d["kappa"] == 0)
    )
    return d


# =========================
# ENTRYPOINT
# =========================

if __name__ == "__main__":
    df_results = process_all_simulations(ROOT_DIR)
    df_results = add_no_result_flag(df_results)
    
    df_valid = df_results[~df_results["no_result"]].copy()
    df_no_result = df_results[df_results["no_result"]].copy()
    
    print(f"Total: {len(df_results)} | Valid: {len(df_valid)} | No-result: {len(df_no_result)}")

    # Save results table for later analysis
    out_csv = ROOT_DIR / "batch_similarity_metrics.csv"
    df_results.to_csv(out_csv, index=False)
    print(f"\nSaved results CSV: {out_csv}")

    make_dashboard(
        df_valid,
        exclude_zero_cases=False,   # already filtered
        top_n=TOP_N
    )


    # Example single-case overlay:
    # plot_burn_scar_comparison(ROOT_DIR / "00159")
