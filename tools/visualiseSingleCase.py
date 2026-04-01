# -*- coding: utf-8 -*-
"""
Visualize a single wildfire simulation case: observed vs simulated burn scar comparison.

What it does:
- Reads observed polygon from firescar.shp
- Finds the newest time_of_arrival_*.tif in outputs/
- Converts raster to burned-area polygon using a threshold
- Reprojects to a local UTM CRS for accurate areas & clean plotting
- Plots:
  1) Overlay (Observed vs Simulated)
  2) Intersection / Observed-only / Sim-only (optional)
- Prints metrics + areas (m² + acres)

Requirements:
geopandas, rasterio, shapely, pyproj, numpy, matplotlib, pandas
"""

from pathlib import Path
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape, GeometryCollection
from shapely.ops import unary_union
from pyproj import CRS
import matplotlib.pyplot as plt


# =========================
# CONFIG
# =========================

ROOT_DIR = Path(r"\\wsl.localhost\Ubuntu-24.04\home\nick\elmfire_validation")  # <-- CHANGE IF NEEDED
CASE_ID = "00015"   # <-- CHANGE THIS
RASTER_PATTERN = "time_of_arrival_*.tif"
RASTER_BAND = 1
BURN_THRESHOLD = 1

M2_TO_ACRES = 0.000247105381  # exact conversion

# If True, shows an extra plot with intersection/only regions
SHOW_PARTITIONS = False


# =========================
# HELPERS
# =========================



def choose_utm_crs(geom):
    """Choose a reasonable UTM CRS based on geometry centroid (geom must be lon/lat)."""
    lon, lat = geom.centroid.x, geom.centroid.y
    zone = int((lon + 180) // 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def area(geom):
    """Return area of geometry, 0.0 if empty or None."""
    if geom is None or geom.is_empty:
        return 0.0
    return geom.area


def safe_divide(num, denom):
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
    Area-based Kappa using an explicit domain polygon.
    Note: If (1 - pe)==0 or total==0 -> 0.0
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
    return safe_divide(area(B), area(A))


def read_shapefile_polygon(path: Path) -> gpd.GeoSeries:
    """Read a shapefile and return a single merged geometry GeoSeries."""
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"No geometries in {path}")
    merged = unary_union(gdf.geometry)
    return gpd.GeoSeries([merged], crs=gdf.crs)


def raster_to_burned_polygon(path: Path, band: int = 1, threshold: float = 1.0) -> gpd.GeoSeries:
    """
    Convert raster to burned-area polygon union.
    Returns empty GeometryCollection if no burned pixels.
    """
    with rasterio.open(path) as src:
        arr = src.read(band)
        burned = arr >= threshold

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


def find_newest_raster(outputs_dir: Path, pattern: str) -> Path:
    rasters = sorted(outputs_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not rasters:
        raise FileNotFoundError(f"No rasters matching '{pattern}' found in {outputs_dir}")
    return rasters[-1]


# =========================
# MAIN VISUALIZATION
# =========================

def visualize_case(case_folder: Path):
    firescar = case_folder / "firescar.shp"
    outputs_dir = case_folder / "outputs"

    if not firescar.exists():
        raise FileNotFoundError(f"Missing firescar.shp: {firescar}")
    if not outputs_dir.exists():
        raise FileNotFoundError(f"Missing outputs/: {outputs_dir}")

    raster_file = find_newest_raster(outputs_dir, RASTER_PATTERN)

    # Observed
    obs = read_shapefile_polygon(firescar)
    obs_ll = obs.to_crs(4326)

    # Simulated (burned polygon)
    sim = raster_to_burned_polygon(raster_file, band=RASTER_BAND, threshold=BURN_THRESHOLD)
    sim_ll = sim.to_crs(4326)

    # Reproject to local UTM for areas/plotting
    target_crs = choose_utm_crs(obs_ll.iloc[0])
    A = obs_ll.to_crs(target_crs).iloc[0]  # observed
    B = sim_ll.to_crs(target_crs).iloc[0]  # simulated

    # Clean a bit
    A = A.buffer(0)
    B = B.buffer(0)

    # Domain for kappa
    DOMAIN = A.union(B).buffer(0)

    # Metrics
    obs_m2 = area(A)
    sim_m2 = area(B)
    inter_m2 = area(A.intersection(B))
    union_m2 = area(A.union(B))

    metrics = {
        "case": case_folder.name,
        "toa_raster_used": str(raster_file),
        "observed_m2": obs_m2,
        "simulated_m2": sim_m2,
        "intersection_m2": inter_m2,
        "union_m2": union_m2,
        "observed_acres": obs_m2 * M2_TO_ACRES,
        "simulated_acres": sim_m2 * M2_TO_ACRES,
        "jaccard": jaccard(A, B),
        "sorensen": sorensen(A, B),
        "kappa": kappa(A, B, DOMAIN),
        "ratio_of_areas": ratio_of_areas(A, B),
    }

    print("\n=== CASE SUMMARY ===")
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"{k:16s}: {v:.6f}")
        else:
            print(f"{k:16s}: {v}")

    # Build GeoDataFrames for plotting
    gdf_obs = gpd.GeoDataFrame({"label": ["Observed"]}, geometry=[A], crs=target_crs)
    gdf_sim = gpd.GeoDataFrame({"label": ["Simulated"]}, geometry=[B], crs=target_crs)

    # Plot 1: overlay
    fig, ax = plt.subplots(figsize=(9, 9))

    # Observed
    gdf_obs.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=2, label="Observed")

    # Simulated
    gdf_sim.plot(ax=ax, facecolor="none", edgecolor="red", linewidth=2, linestyle="--", label="Simulated")

    ax.set_aspect("equal", "box")
    ax.set_title(
        f"Observed vs Simulated Burn Scar\n{case_folder.name}\n"
        f"J={metrics['jaccard']:.3f}  S={metrics['sorensen']:.3f}  K={metrics['kappa']:.3f}  "
        f"Ratio={metrics['ratio_of_areas']:.2f}"
    )
    ax.legend()
    ax.grid(True)
    plt.tight_layout()
    plt.show()

    # Plot 2: partitions (intersection/only regions)
    if SHOW_PARTITIONS:
        inter = A.intersection(B)
        obs_only = A.difference(B)
        sim_only = B.difference(A)

        gdf_parts = gpd.GeoDataFrame(
            {"part": ["Intersection", "Observed only", "Simulated only"]},
            geometry=[inter, obs_only, sim_only],
            crs=target_crs
        )

        fig, ax = plt.subplots(figsize=(9, 9))
        # Plot outlines so all parts visible even if some are small
        gdf_parts[gdf_parts["part"] == "Intersection"].plot(
            ax=ax, facecolor="none", edgecolor="green", linewidth=2, label="Intersection"
        )
        gdf_parts[gdf_parts["part"] == "Observed only"].plot(
            ax=ax, facecolor="none", edgecolor="black", linewidth=2, linestyle="-", label="Observed only"
        )
        gdf_parts[gdf_parts["part"] == "Simulated only"].plot(
            ax=ax, facecolor="none", edgecolor="red", linewidth=2, linestyle="--", label="Simulated only"
        )

        ax.set_aspect("equal", "box")
        ax.set_title(f"Overlap Partitions\n{case_folder.name}")
        ax.legend()
        ax.grid(True)
        plt.tight_layout()
        plt.show()


# =========================
# RUN
# =========================

if __name__ == "__main__":
    case_folder = ROOT_DIR / CASE_ID
    visualize_case(case_folder)
