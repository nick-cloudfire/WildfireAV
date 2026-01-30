# -*- coding: utf-8 -*-
"""
Batch wildfire validation report generator (ONE multi-page PDF).

One PDF, one page per case folder:
- Observed burn scar (firescar.gpkg)
- Predicted burn scar polygon from newest time_of_arrival_*.tif
- Ignition points (auto-detected)
- Stats + simulation duration (SIMULATION_TSTOP)
- Similarity metrics + area stats
- Optional: satellite cumulative hotspot coverage curve

Requirements:
    geopandas, rasterio, shapely, pyproj, numpy, matplotlib, pandas
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape as shp_shape, GeometryCollection, Point
from shapely.ops import unary_union
from pyproj import CRS
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import matplotlib as mpl


# -------------------------
# Matplotlib defaults
# -------------------------
mpl.rcParams["pdf.fonttype"] = 42
mpl.rcParams["ps.fonttype"] = 42
mpl.rcParams["font.family"] = "DejaVu Sans Mono"


# -------------------------
# Config
# -------------------------
M2_TO_ACRES = 0.000247105381  # exact

CSV_LON_COLS = ["lon", "longitude", "x", "X", "LONGITUDE", "LON"]
CSV_LAT_COLS = ["lat", "latitude", "y", "Y", "LATITUDE", "LAT"]
CSV_EPSG_COLS = ["epsg", "EPSG", "crs_epsg", "CRS_EPSG"]

DEFAULT_IGNITION_CANDIDATES = ["ignition_point.gpkg"]
DEFAULT_STATS_CANDIDATES = [
    "outputs/simulation_stats.csv",
    "outputs/stats.csv",
    "simulation_stats.csv",
    "stats.csv",
    "outputs/run_summary.csv",
    "run_summary.csv",
    "outputs/run.log",
    "outputs/elmfire.log",
    "run.log",
    "elmfire.log",
]
DEFAULT_DATAFILE_CANDIDATES = ["*.data"]


@dataclass(frozen=True)
class ReportConfig:
    root_dir: Path
    output_pdf: Path

    case_ids: list[str] | None = None

    raster_pattern: str = "time_of_arrival_*.tif"
    raster_band: int = 1
    burn_threshold: float = 1.0

    ignition_candidates: list[str] = None
    stats_candidates: list[str] = None
    datafile_candidates: list[str] = None

    def __post_init__(self):
        # dataclasses with mutable defaults need this pattern
        object.__setattr__(self, "ignition_candidates",
                           self.ignition_candidates or DEFAULT_IGNITION_CANDIDATES)
        object.__setattr__(self, "stats_candidates",
                           self.stats_candidates or DEFAULT_STATS_CANDIDATES)
        object.__setattr__(self, "datafile_candidates",
                           self.datafile_candidates or DEFAULT_DATAFILE_CANDIDATES)


@dataclass(frozen=True)
class SatelliteConfig:
    enabled: bool

    gpkg: Path | None = None
    layer: str | None = None
    date_col: str | None = None
    time_col: str | None = None

    master_csv: Path | None = None
    folder_col: str | None = None
    discovery_col: str | None = None

    area_crs: str = "EPSG:5070"
    max_gap: pd.Timedelta = pd.Timedelta(days=2)
    coverage_fraction: float = 0.85
    hotspot_buffer_dist: float = 500.0


# -------------------------
# Small geometry + metrics helpers
# -------------------------
def choose_utm_crs(geom_ll) -> CRS:
    lon, lat = geom_ll.centroid.x, geom_ll.centroid.y
    zone = int((lon + 180) // 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def geom_area(g) -> float:
    if g is None or g.is_empty:
        return 0.0
    return float(g.area)


def safe_divide(num: float, denom: float) -> float:
    return 0.0 if denom == 0 else num / denom


def jaccard(A, B) -> float:
    inter = geom_area(A.intersection(B))
    union = geom_area(A.union(B))
    return safe_divide(inter, union)


def sorensen(A, B) -> float:
    inter = geom_area(A.intersection(B))
    denom = geom_area(A) + geom_area(B)
    return safe_divide(2 * inter, denom)


def kappa(A, B, domain) -> float:
    tp = geom_area(A.intersection(B))
    fp = geom_area(B.difference(A))
    fn = geom_area(A.difference(B))
    tn = geom_area(domain.difference(A.union(B)))
    total = tp + fp + fn + tn
    if total == 0:
        return 0.0
    pa = (tp + tn) / total
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (total ** 2)
    return 0.0 if (1 - pe) == 0 else (pa - pe) / (1 - pe)


def ratio_of_areas(observed, simulated) -> float:
    return safe_divide(geom_area(simulated), geom_area(observed))


# -------------------------
# File discovery + readers
# -------------------------
def first_existing(case_folder: Path, relpaths: list[str]) -> Path | None:
    for rel in relpaths:
        if any(ch in rel for ch in ["*", "?", "["]):
            matches = sorted(case_folder.glob(rel))
            if matches:
                return matches[0]
        else:
            p = case_folder / rel
            if p.exists():
                return p
    return None


def read_polygon_union(path: Path) -> gpd.GeoSeries:
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"No geometries in {path}")
    merged = unary_union(gdf.geometry)
    return gpd.GeoSeries([merged], crs=gdf.crs)


def newest_raster(outputs_dir: Path, pattern: str) -> Path:
    rasters = sorted(outputs_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not rasters:
        raise FileNotFoundError(f"No rasters matching '{pattern}' in {outputs_dir}")
    return rasters[-1]


def raster_burn_polygon(path: Path, band: int, threshold: float) -> gpd.GeoSeries:
    with rasterio.open(path) as src:
        arr = src.read(band)
        burned = arr >= threshold
        if not burned.any():
            return gpd.GeoSeries([GeometryCollection()], crs=src.crs)

        geoms = [
            shp_shape(g)
            for g, v in shapes(burned.astype(np.uint8), mask=burned, transform=src.transform)
            if v == 1
        ]
        if not geoms:
            return gpd.GeoSeries([GeometryCollection()], crs=src.crs)

        return gpd.GeoSeries([unary_union(geoms)], crs=src.crs)


def read_ignitions(case_folder: Path, cfg: ReportConfig) -> gpd.GeoDataFrame:
    p = first_existing(case_folder, cfg.ignition_candidates)
    if p is None:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    suf = p.suffix.lower()
    if suf in [".shp", ".geojson", ".gpkg"]:
        gdf = gpd.read_file(p)
        if gdf.empty:
            return gpd.GeoDataFrame(geometry=[], crs=gdf.crs or "EPSG:4326")
        return gdf

    if suf == ".csv":
        df = pd.read_csv(p)
        lon_col = next((c for c in CSV_LON_COLS if c in df.columns), None)
        lat_col = next((c for c in CSV_LAT_COLS if c in df.columns), None)
        if lon_col is None or lat_col is None or df.empty:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        crs = "EPSG:4326"
        epsg_col = next((c for c in CSV_EPSG_COLS if c in df.columns), None)
        if epsg_col is not None:
            try:
                crs = CRS.from_epsg(int(df[epsg_col].iloc[0]))
            except Exception:
                crs = "EPSG:4326"

        geom = [Point(xy) for xy in zip(df[lon_col], df[lat_col])]
        return gpd.GeoDataFrame(df, geometry=geom, crs=crs)

    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


def read_sim_stats(case_folder: Path, cfg: ReportConfig) -> dict:
    p = first_existing(case_folder, cfg.stats_candidates)
    if p is None:
        return {}

    out = {"_stats_source": str(p)}
    if p.suffix.lower() == ".csv":
        try:
            df = pd.read_csv(p)
            if not df.empty:
                out.update(df.iloc[-1].to_dict())
        except Exception:
            pass
        return out

    # lightweight log parse
    try:
        text = p.read_text(errors="ignore")
    except Exception:
        return out

    kv_pairs = re.findall(r"([A-Za-z0-9_\-]+)\s*=\s*([0-9]+(?:\.[0-9]+)?)", text)
    for k, v in kv_pairs[:120]:
        if k in out:
            continue
        try:
            out[k] = float(v) if "." in v else int(v)
        except Exception:
            out[k] = v

    m = re.search(r"Runtime\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*s", text, flags=re.IGNORECASE)
    if m and "Runtime_s" not in out:
        out["Runtime_s"] = float(m.group(1))

    return out


def parse_tstop_hours(case_folder: Path, cfg: ReportConfig) -> tuple[float | None, str | None]:
    p = first_existing(case_folder, cfg.datafile_candidates)
    if p is None or not p.exists():
        return None, None

    try:
        txt = p.read_text(errors="ignore")
    except Exception:
        return None, str(p)

    m = re.search(r"SIMULATION_TSTOP\s*=\s*([0-9]+(?:\.[0-9]+)?)", txt)
    if not m:
        return None, str(p)

    seconds = float(m.group(1))
    return seconds / 3600.0, str(p)


# -------------------------
# Satellite coverage (optional)
# -------------------------
def build_satellite_datetime(df: pd.DataFrame, date_col: str, time_col: str) -> pd.Series:
    time_str = df[time_col].astype(str).str.zfill(4)
    dt_str = df[date_col].astype(str) + " " + time_str
    return pd.to_datetime(dt_str, errors="coerce")


def cumulative_coverage_chain(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    discovery_dt: pd.Timestamp,
    buffer_dist: float,
    max_gap: pd.Timedelta,
):
    pts = pts_in_burn.loc[pts_in_burn["sat_datetime"] >= discovery_dt].copy()
    if pts.empty:
        return [], [], GeometryCollection()

    pts = pts.sort_values("sat_datetime").reset_index(drop=True)
    times, areas = [], []
    cumulative_geom = GeometryCollection()

    first_time = pts.loc[0, "sat_datetime"]
    cumulative_geom = pts.loc[0, "geometry"].buffer(buffer_dist).intersection(burn_geom)
    times.append(first_time)
    areas.append(float(cumulative_geom.area))
    last_time = first_time

    for i in range(1, len(pts)):
        t = pts.loc[i, "sat_datetime"]
        if (t - last_time) > max_gap:
            break

        buf = pts.loc[i, "geometry"].buffer(buffer_dist)
        cumulative_geom = cumulative_geom.union(buf).intersection(burn_geom)
        times.append(t)
        areas.append(float(cumulative_geom.area))
        last_time = t

    return times, areas, cumulative_geom


def satellite_end_time(
    pts_in_burn: gpd.GeoDataFrame,
    burn_geom,
    discovery_dt: pd.Timestamp,
    buffer_dist: float,
    max_gap: pd.Timedelta,
    coverage_fraction: float,
):
    times, areas, _ = cumulative_coverage_chain(
        pts_in_burn, burn_geom, discovery_dt, buffer_dist, max_gap
    )
    if not times:
        return pd.NaT, [], [], 0.0

    burn_area = float(burn_geom.area)
    final_sat_area = areas[-1]
    effective_area = min(burn_area, final_sat_area)
    target_area = coverage_fraction * effective_area

    end_time = pd.NaT
    for t, a in zip(times, areas):
        if a >= target_area:
            end_time = t
            break
    if pd.isna(end_time):
        end_time = times[-1]

    return end_time, times, areas, target_area


# -------------------------
# Per-case pipeline bundle
# -------------------------
@dataclass
class CaseResult:
    case_id: str
    raster_used: Path

    crs_utm: CRS
    A_obs: object  # shapely geometry
    B_sim: object  # shapely geometry

    ign_utm: gpd.GeoDataFrame
    stats: dict
    tstop_hours: float | None
    tstop_source: str | None

    # satellite
    sat_times: list = None
    sat_areas: list = None
    sat_end_time: object = None
    sat_target_area: float | None = None

    metrics: dict = None


def process_case(
    case_folder: Path,
    cfg: ReportConfig,
    sat_cfg: SatelliteConfig,
    sat_gdf: gpd.GeoDataFrame | None,
    master_df: pd.DataFrame | None,
) -> CaseResult:
    firescar = case_folder / "firescar.gpkg"
    outputs_dir = case_folder / "outputs"
    if not firescar.exists():
        raise FileNotFoundError(f"Missing firescar.gpkg: {firescar}")
    if not outputs_dir.exists():
        raise FileNotFoundError(f"Missing outputs/: {outputs_dir}")

    raster_file = newest_raster(outputs_dir, cfg.raster_pattern)

    obs = read_polygon_union(firescar).to_crs(4326)
    sim = raster_burn_polygon(raster_file, cfg.raster_band, cfg.burn_threshold).to_crs(4326)

    crs_utm = choose_utm_crs(obs.iloc[0])
    A = obs.to_crs(crs_utm).iloc[0].buffer(0)
    B = sim.to_crs(crs_utm).iloc[0].buffer(0)
    domain = A.union(B).buffer(0)

    obs_m2 = geom_area(A)
    sim_m2 = geom_area(B)

    metrics = {
        "case": case_folder.name,
        "toa_raster_used": str(raster_file),
        "observed_m2": obs_m2,
        "simulated_m2": sim_m2,
        "intersection_m2": geom_area(A.intersection(B)),
        "union_m2": geom_area(A.union(B)),
        "observed_acres": obs_m2 * M2_TO_ACRES,
        "simulated_acres": sim_m2 * M2_TO_ACRES,
        "jaccard": jaccard(A, B),
        "sorensen": sorensen(A, B),
        "kappa": kappa(A, B, domain),
        "ratio_of_areas": ratio_of_areas(A, B),
    }

    ign = read_ignitions(case_folder, cfg)
    if ign.crs is None:
        ign = ign.set_crs("EPSG:4326")
    try:
        ign_utm = ign.to_crs(crs_utm) if not ign.empty else gpd.GeoDataFrame(geometry=[], crs=crs_utm)
    except Exception:
        ign_utm = gpd.GeoDataFrame(geometry=[], crs=crs_utm)

    stats = read_sim_stats(case_folder, cfg)
    tstop_h, tstop_src = parse_tstop_hours(case_folder, cfg)

    # Satellite (optional)
    sat_times, sat_areas, sat_end, sat_target = [], [], pd.NaT, None
    if sat_cfg.enabled and sat_gdf is not None and master_df is not None:
        try:
            folder_int = int(case_folder.name)
            row = master_df.loc[master_df[sat_cfg.folder_col].astype(int) == folder_int]
            if not row.empty:
                disc_dt = pd.to_datetime(row.iloc[0][sat_cfg.discovery_col], errors="coerce")
                if disc_dt is not None and not pd.isna(disc_dt):
                    if getattr(disc_dt, "tzinfo", None) is not None:
                        disc_dt = disc_dt.tz_convert("UTC").tz_localize(None)

                    burn_geom_5070 = gpd.GeoSeries([A], crs=crs_utm).to_crs(sat_cfg.area_crs).iloc[0]
                    sat_local = sat_gdf.to_crs(sat_cfg.area_crs)

                    pts_in_burn = sat_local.loc[sat_local.geometry.within(burn_geom_5070)].copy()
                    if not pts_in_burn.empty:
                        sat_end, sat_times, sat_areas, sat_target = satellite_end_time(
                            pts_in_burn=pts_in_burn,
                            burn_geom=burn_geom_5070,
                            discovery_dt=disc_dt,
                            buffer_dist=sat_cfg.hotspot_buffer_dist,
                            max_gap=sat_cfg.max_gap,
                            coverage_fraction=sat_cfg.coverage_fraction,
                        )
                        metrics["satellite_end_time"] = str(sat_end) if not pd.isna(sat_end) else ""
                        metrics["satellite_target_area_m2"] = float(sat_target) if sat_target is not None else np.nan
        except Exception:
            # keep satellite optional; do not fail the page
            metrics["satellite_end_time"] = ""
            metrics["satellite_target_area_m2"] = np.nan

    return CaseResult(
        case_id=case_folder.name,
        raster_used=raster_file,
        crs_utm=crs_utm,
        A_obs=A,
        B_sim=B,
        ign_utm=ign_utm,
        stats=stats,
        tstop_hours=tstop_h,
        tstop_source=tstop_src,
        sat_times=sat_times,
        sat_areas=sat_areas,
        sat_end_time=sat_end,
        sat_target_area=sat_target,
        metrics=metrics,
    )


# -------------------------
# Plotting
# -------------------------
def plot_case_page(res: CaseResult, sat_cfg: SatelliteConfig) -> plt.Figure:
    fig = plt.figure(figsize=(11.69, 8.27))  # A4 landscape-ish
    gs = fig.add_gridspec(2, 2, width_ratios=[1.45, 1.0], height_ratios=[1.0, 0.75])

    ax_map = fig.add_subplot(gs[:, 0])
    ax_txt = fig.add_subplot(gs[0, 1])
    ax_ts = fig.add_subplot(gs[1, 1])
    ax_txt.axis("off")

    gdf_obs = gpd.GeoDataFrame({"label": ["Observed"]}, geometry=[res.A_obs], crs=res.crs_utm)
    gdf_sim = gpd.GeoDataFrame({"label": ["Simulated"]}, geometry=[res.B_sim], crs=res.crs_utm)

    gdf_obs.plot(ax=ax_map, facecolor="none", edgecolor="black", linewidth=2, label="Observed")
    gdf_sim.plot(ax=ax_map, facecolor="red", edgecolor="none", alpha=0.35, label="Predicted")

    if res.ign_utm is not None and not res.ign_utm.empty:
        res.ign_utm.plot(ax=ax_map, marker="x", markersize=60, label="Ignition", zorder=5)

    ax_map.set_aspect("equal", "box")
    ax_map.grid(True)
    ax_map.legend(loc="upper right")

    m = res.metrics
    ax_map.set_title(
        f"Case {res.case_id} — Observed vs Predicted\n"
        f"J={m['jaccard']:.3f}  S={m['sorensen']:.3f}  "
        f"K={m['kappa']:.3f}  Ratio={m['ratio_of_areas']:.2f}"
    )

    # Right text panel
    lines = [
        f"CASE: {res.case_id}",
        "",
        "FILES",
        f"TOA raster: {res.raster_used.name}",
        f"Duration (TSTOP): {res.tstop_hours:.2f} hours" if res.tstop_hours is not None else "Duration (TSTOP): (not found)",
    ]
    if res.tstop_source:
        lines.append(f"TSTOP source: {Path(res.tstop_source).name}")

    lines += [
        "",
        "AREAS",
        f"Observed : {m['observed_m2']:.0f} m²  ({m['observed_acres']:.2f} acres)",
        f"Predicted: {m['simulated_m2']:.0f} m²  ({m['simulated_acres']:.2f} acres)",
        f"Intersect: {m['intersection_m2']:.0f} m²",
        f"Union    : {m['union_m2']:.0f} m²",
        "",
        "SIMILARITY",
        f"Jaccard  : {m['jaccard']:.4f}",
        f"Sorensen : {m['sorensen']:.4f}",
        f"Kappa    : {m['kappa']:.4f}",
        f"Area ratio (pred/obs): {m['ratio_of_areas']:.3f}",
    ]

    if res.stats:
        lines += ["", "SIMULATION STATS"]
        src = res.stats.get("_stats_source", "")
        if src:
            lines.append(f"source: {Path(src).name}")
        keys = [k for k in res.stats.keys() if k != "_stats_source"]
        for k in keys[:24]:
            v = res.stats[k]
            if isinstance(v, float):
                lines.append(f"{k}: {v:.6g}")
            else:
                s = str(v)
                lines.append(f"{k}: {s[:57] + '...' if len(s) > 60 else s}")

    ax_txt.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", fontsize=10, family="monospace")

    # Satellite curve
    ax_ts.grid(True)
    ax_ts.set_title("Satellite cumulative hotspot coverage (clipped)")
    if res.sat_times and res.sat_areas:
        t_arr = np.array(res.sat_times, dtype="datetime64[ns]")
        a_arr = np.array(res.sat_areas, dtype=float)
        ax_ts.plot(t_arr, a_arr, marker="o", markersize=3)
        if res.sat_target_area is not None and not np.isnan(res.sat_target_area):
            ax_ts.axhline(res.sat_target_area, linestyle="--", label=f"{sat_cfg.coverage_fraction*100:.0f}% target")
        if res.sat_end_time is not None and not pd.isna(res.sat_end_time):
            ax_ts.axvline(res.sat_end_time, linestyle="--", label="SatelliteEndTime")
        ax_ts.set_xlabel("Time")
        ax_ts.set_ylabel("Area (m²)")
        ax_ts.tick_params(axis="x", rotation=90)
        ax_ts.legend(fontsize=8)
    else:
        ax_ts.text(0.5, 0.5, "No satellite curve available for this case",
                   ha="center", va="center", transform=ax_ts.transAxes)
        ax_ts.set_xticks([])
        ax_ts.set_yticks([])

    fig.tight_layout()
    return fig


# -------------------------
# Case discovery + PDF build
# -------------------------
def list_case_folders(cfg: ReportConfig) -> list[Path]:
    if cfg.case_ids is None:
        return [d for d in sorted(cfg.root_dir.iterdir()) if d.is_dir() and (d / "firescar.gpkg").exists()]
    return [cfg.root_dir / cid for cid in cfg.case_ids]


def build_report_pdf(
    cfg: ReportConfig,
    sat_cfg: SatelliteConfig,
    sat_gdf: gpd.GeoDataFrame | None,
    master_df: pd.DataFrame | None,
) -> pd.DataFrame:
    case_folders = list_case_folders(cfg)
    if not case_folders:
        raise RuntimeError("No cases found. Check ROOT_DIR / CASE_IDS and firescar.gpkg presence.")

    cfg.output_pdf.parent.mkdir(parents=True, exist_ok=True)

    metrics_list = []
    with PdfPages(cfg.output_pdf) as pdf:
        for case_folder in case_folders:
            try:
                res = process_case(case_folder, cfg, sat_cfg, sat_gdf, master_df)
                fig = plot_case_page(res, sat_cfg)
                pdf.savefig(fig)
                plt.close(fig)
                metrics_list.append(res.metrics)
                print(f"Added page: {case_folder.name}")
            except Exception as e:
                print(f"SKIP {case_folder.name}: {e}")

    return pd.DataFrame(metrics_list)


# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    ROOT_DIR = Path(r"/home/nick/elmfire_validation/FirePairs")
    cfg = ReportConfig(
        root_dir=ROOT_DIR,
        output_pdf=ROOT_DIR / "elmfire_validation_report.pdf",
        case_ids=None,
    )

    # Optional: load from your pipelineConfig if present
    try:
        import pipelineConfig

        sat_cfg = SatelliteConfig(
            enabled=True,
            gpkg=Path(pipelineConfig.SATELLITE_GPKG),
            layer=pipelineConfig.SATELLITE_LAYER_NAME,
            date_col=pipelineConfig.SAT_DATE_COL,
            time_col=pipelineConfig.SAT_TIME_COL,
            master_csv=Path(pipelineConfig.FIRE_SUMMARY_CSV),
            folder_col=pipelineConfig.COL_FOLDER,
            discovery_col=pipelineConfig.COL_POINT_DISCOVERY,
            area_crs="EPSG:5070",
            max_gap=pd.Timedelta(days=pipelineConfig.SAT_CHAIN_MAX_GAP_DAYS),
            coverage_fraction=0.85,
            hotspot_buffer_dist=getattr(pipelineConfig, "SAT_HOTSPOT_BUFFER_DIST", 500.0),
        )
    except Exception:
        sat_cfg = SatelliteConfig(enabled=False)

    sat_gdf, master_df = None, None
    if sat_cfg.enabled:
        print("Loading satellite points...")
        sat_gdf = gpd.read_file(sat_cfg.gpkg, layer=sat_cfg.layer)
        sat_gdf = sat_gdf.set_crs(sat_cfg.area_crs, inplace=False, allow_override=True)
        sat_gdf["sat_datetime"] = build_satellite_datetime(sat_gdf, sat_cfg.date_col, sat_cfg.time_col)
        sat_gdf = sat_gdf.dropna(subset=["sat_datetime"])

        print("Loading master CSV...")
        master_df = pd.read_csv(sat_cfg.master_csv)

    df_metrics = build_report_pdf(cfg, sat_cfg, sat_gdf, master_df)
    print("Done.")