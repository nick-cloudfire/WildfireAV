#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 9 of runPipelineParallel: prepare FARSITE inputs for one case.

For each case:
- Converts LANDFIRE.tif → landscape.lcp + landscape.prj  (gdal_translate / gdalsrsinfo)
- Reprojects ignition_point.gpkg → ignition.shp          (ogr2ogr)
- Merges pre-clipped barrier layers → barrier.shp         (ogr2ogr, optional)
- Crops conditioning days from weather.wxs
- Computes per-fuel-class mean moisture → output.fms
- Writes farsite.txt  (single line consumed by FARSITE batch runner)
- Writes farsite.input (all FARSITE run parameters + inline weather/moisture)

Standalone usage (process all cases under FIRE_ROOT):
    python prepareFarsite.py

Or from the pipeline via main(case_dir).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import fiona
import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.transform import xy as transform_xy
from rasterio.warp import reproject, Resampling as RioResampling
from rasterio.windows import Window

import pipelineConfig as cfg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIRE_ROOT        = Path(cfg.FIRE_ROOT)
PREIGNITION_DAYS = cfg.CONDITIONING_DAYS
M1_FILENAME      = cfg.FMC_FILE_NAMES[0]
M10_FILENAME     = cfg.FMC_FILE_NAMES[1]
M100_FILENAME    = cfg.FMC_FILE_NAMES[2]
FBFM_FILENAME    = cfg.LANDFIRE_BAND_FILE_NAMES[3]
INPUTS           = cfg.INPUTS_SUBDIR_NAME
MESH_RES_FACTOR  = cfg.WINDNINJA_MESH_RESOLUTION_FACTOR
WS_TIF_NAME      = cfg.WS_TIF_NAME
WD_TIF_NAME      = cfg.WD_TIF_NAME

USE_BARRIER = True   # set False to omit barrier.shp from farsite.input

LH_CONST    = 60
LW_CONST    = 90
DECIMALS    = 0

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_band(path: Path):
    with rasterio.open(path) as ds:
        arr    = ds.read(1)
        nodata = ds.nodata
        profile = ds.profile
    return arr, nodata, profile


def _means_by_class(classes, values, valid_mask, max_class):
    c = classes[valid_mask].astype(np.int64)
    v = values[valid_mask].astype(np.float64)
    sums  = np.bincount(c, weights=v, minlength=max_class + 1)
    cnts  = np.bincount(c,            minlength=max_class + 1)
    means = np.full(max_class + 1, np.nan, dtype=np.float64)
    ok    = cnts > 0
    means[ok] = sums[ok] / cnts[ok]
    return means, cnts


def _parse_wxs_first_last_datetimes(wxs_lines):
    data_lines = [
        ln.strip() for ln in wxs_lines
        if ln.strip() and len(ln.strip()) >= 4 and ln.strip()[:4].isdigit()
    ]
    if not data_lines:
        raise ValueError("No data lines found in WXS file.")

    def parse_dt(line):
        parts = line.split()
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        hhmm = parts[3].zfill(4)
        return datetime(y, m, d, int(hhmm[:2]), int(hhmm[2:]))

    def _int_precip(line):
        parts = line.split()
        # field 6 (0-based) is HrlyPcp; FARSITE requires integer precipitation
        parts[6] = str(int(round(float(parts[6]))))
        return " ".join(parts)

    data_lines = [_int_precip(ln) for ln in data_lines]
    return parse_dt(data_lines[0]), parse_dt(data_lines[-1]), data_lines


def _burn_period_lines(start_dt, end_dt):
    def hhmm(dt):
        return f"{dt.hour:02d}{dt.minute:02d}"

    start_date, end_date = start_dt.date(), end_dt.date()
    lines, d = [], start_date
    while d <= end_date:
        if start_date == end_date:
            s, e = hhmm(start_dt), hhmm(end_dt)
        elif d == start_date:
            s, e = hhmm(start_dt), "2359"
        elif d == end_date:
            s, e = "0000", hhmm(end_dt)
        else:
            s, e = "0000", "2359"
        lines.append(f"{d.month:02d} {d.day:02d} {s} {e}")
        d = d + timedelta(days=1)
    return lines


def _is_valid_fuel(val, nodata, valid_min: float = 101.0) -> bool:
    if val is None:
        return False
    if nodata is not None and val == nodata:
        return False
    try:
        if np.isnan(val):
            return False
    except Exception:
        pass
    return val >= valid_min


def _snap_to_valid_fuel(
    fuels_tif: Path,
    x: float,
    y: float,
    valid_min: float = 101.0,
    max_radius_cells: int = 2000,
) -> tuple[float, float, bool]:
    """Snap (x, y) to the centre of the nearest pixel with fuel code >= valid_min."""
    with rasterio.open(fuels_tif) as ds:
        row0, col0 = ds.index(x, y)
        row0 = min(max(row0, 0), ds.height - 1)
        col0 = min(max(col0, 0), ds.width - 1)

        v0 = ds.read(1, window=Window(col0, row0, 1, 1), masked=False)[0, 0]
        if _is_valid_fuel(v0, ds.nodata, valid_min):
            xc, yc = transform_xy(ds.transform, row0, col0, offset="center")
            return float(xc), float(yc), False

        for r in range(1, max_radius_cells + 1):
            r0 = max(row0 - r, 0)
            r1 = min(row0 + r, ds.height - 1)
            c0 = max(col0 - r, 0)
            c1 = min(col0 + r, ds.width - 1)
            arr = ds.read(1, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1), masked=False)
            valid = arr >= valid_min
            if ds.nodata is not None:
                valid &= arr != ds.nodata
            if np.issubdtype(arr.dtype, np.floating):
                valid &= ~np.isnan(arr)
            if not np.any(valid):
                continue
            vrows, vcols = np.where(valid)
            rows, cols = vrows + r0, vcols + c0
            d2 = (rows - row0) ** 2 + (cols - col0) ** 2
            k = int(np.argmin(d2))
            xc, yc = transform_xy(ds.transform, int(rows[k]), int(cols[k]), offset="center")
            return float(xc), float(yc), True

    raise RuntimeError(
        f"No valid fuel (>= {valid_min}) within {max_radius_cells} cells of ignition"
    )


def _snap_ignition_shp(ignition_shp: Path, fuels_tif: Path) -> bool:
    """Snap the ignition shapefile point to the nearest valid fuel cell in-place.

    Returns True if the point was moved.
    """
    with fiona.open(str(ignition_shp)) as src:
        schema   = src.schema.copy()
        crs      = src.crs
        features = list(src)

    if not features:
        return False

    feat   = dict(features[0])
    coords = feat["geometry"]["coordinates"]
    x, y   = float(coords[0]), float(coords[1])

    x_snap, y_snap, moved = _snap_to_valid_fuel(fuels_tif, x, y)
    if not moved:
        return False

    new_coords = (x_snap, y_snap) if len(coords) == 2 else (x_snap, y_snap, float(coords[2]))
    feat["geometry"] = {"type": "Point", "coordinates": new_coords}

    with fiona.open(str(ignition_shp), "w", driver="ESRI Shapefile",
                    schema=schema, crs=crs) as dst:
        dst.write(feat)
    return True


def _write_asc(data: np.ndarray, cellsize: float, xllcorner: float, yllcorner: float,
               out_path: Path) -> None:
    """Write *data* as an ESRI ASCII raster compatible with FARSITE's ReadAsciiGrid."""
    nrows, ncols = data.shape
    # FARSITE rejects any negative value (treats them as nodata and aborts)
    safe = np.clip(data, 0.0, None)
    header = (
        f"ncols {ncols}\n"
        f"nrows {nrows}\n"
        f"xllcorner {xllcorner:.6f}\n"
        f"yllcorner {yllcorner:.6f}\n"
        f"cellsize {cellsize:.6f}\n"
        f"NODATA_value -9999"
    )
    np.savetxt(str(out_path), safe, header=header, comments="", fmt="%.4f")


def _build_wind_grids(
    ws_tif: Path, wd_tif: Path,
    winds_dir: Path,
    start_dt: datetime, end_dt: datetime,
) -> list:
    """Resample ws.tif/wd.tif bands to coarser ASC grids and return ATM entries.

    Output resolution = source cellsize × MESH_RES_FACTOR (matches WindNinja mesh).
    Returns list of (month, day, hhmm, ws_rel_str, wd_rel_str) tuples where
    paths are relative to winds_dir.parent (i.e. farsite_dir).
    """
    winds_dir.mkdir(exist_ok=True)
    farsite_dir = winds_dir.parent
    entries = []

    with rasterio.open(ws_tif) as ws_ds, rasterio.open(wd_tif) as wd_ds:
        src_transform = ws_ds.transform
        src_crs       = ws_ds.crs
        cellsize_src  = abs(float(src_transform.a))
        xllcorner     = float(src_transform.c)
        ymax          = float(src_transform.f)

        out_cellsize  = cellsize_src * MESH_RES_FACTOR
        # Use ceil so output extent is >= source extent (CheckCoverage requirement)
        out_width     = math.ceil(ws_ds.width  / MESH_RES_FACTOR)
        out_height    = math.ceil(ws_ds.height / MESH_RES_FACTOR)
        out_transform = Affine(out_cellsize, 0.0, xllcorner,
                               0.0, -out_cellsize, ymax)
        yllcorner     = ymax - out_height * out_cellsize

        n_bands = ws_ds.count
        print(f"  Wind grids: {n_bands} bands → {out_width}×{out_height} @ {out_cellsize:.0f}m")

        for i in range(n_bands):
            ts = start_dt + timedelta(hours=i)
            if ts > end_dt:
                break

            ws_arr = np.zeros((out_height, out_width), dtype=np.float32)
            reproject(
                source=rasterio.band(ws_ds, i + 1), destination=ws_arr,
                src_transform=src_transform, src_crs=src_crs,
                dst_transform=out_transform,      dst_crs=src_crs,
                resampling=RioResampling.bilinear,
            )
            wd_arr = np.zeros((out_height, out_width), dtype=np.float32)
            reproject(
                source=rasterio.band(wd_ds, i + 1), destination=wd_arr,
                src_transform=src_transform, src_crs=src_crs,
                dst_transform=out_transform,      dst_crs=src_crs,
                resampling=RioResampling.nearest,
            )

            ws_asc = winds_dir / f"ws_{i:03d}.asc"
            wd_asc = winds_dir / f"wd_{i:03d}.asc"
            _write_asc(ws_arr, out_cellsize, xllcorner, yllcorner, ws_asc)
            _write_asc(wd_arr, out_cellsize, xllcorner, yllcorner, wd_asc)

            hhmm = ts.hour * 100 + ts.minute
            entries.append((ts.month, ts.day, hhmm,
                             str(ws_asc.relative_to(farsite_dir)),
                             str(wd_asc.relative_to(farsite_dir))))

    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def main(case_dir: Path) -> None:
    """Prepare all FARSITE inputs for *case_dir*.  Skips if already done."""
    case_dir      = Path(case_dir)
    farsite_dir   = case_dir / "farsite"
    farsite_input = farsite_dir / "farsite.input"

    if farsite_input.exists():
        print(f"[{case_dir.name}] Skipped — farsite.input already exists")
        return

    # ---- required inputs check ----------------------------------------
    required = [
        case_dir / "LANDFIRE.tif",
        case_dir / "ignition_point.gpkg",
        case_dir / INPUTS / cfg.WXS_FILE_NAME,
        case_dir / INPUTS / WS_TIF_NAME,
        case_dir / INPUTS / WD_TIF_NAME,
        case_dir / INPUTS / (FBFM_FILENAME  + ".tif"),
        case_dir / INPUTS / (M1_FILENAME    + ".tif"),
        case_dir / INPUTS / (M10_FILENAME   + ".tif"),
        case_dir / INPUTS / (M100_FILENAME  + ".tif"),
        *(([
            case_dir / INPUTS / cfg.BARRIER_ROADS_CLIP_NAME,
            case_dir / INPUTS / cfg.BARRIER_WATER_CLIP_NAME,
            case_dir / INPUTS / cfg.BARRIER_BACKUP_CLIP_NAME,
        ]) if USE_BARRIER else []),
    ]
    missing = [p for p in required if not p.exists()]
    if missing:
        print(f"[{case_dir.name}] Skipped — missing: {', '.join(p.name for p in missing)}")
        return

    print(f"[{case_dir.name}] Preparing FARSITE inputs")

    landfire    = case_dir / "LANDFIRE.tif"
    lcp_out     = farsite_dir / "landscape.lcp"
    prj_out     = farsite_dir / "landscape.prj"
    wxs_path    = case_dir / INPUTS / cfg.WXS_FILE_NAME
    new_wxs     = farsite_dir / "weather.wxs"
    fms_out     = farsite_dir / "output.fms"

    # ---- directory setup -----------------------------------------------
    shutil.rmtree(farsite_dir, ignore_errors=True)
    farsite_dir.mkdir()
    (farsite_dir / "outputs").mkdir()

    # ---- landscape.lcp + .prj ------------------------------------------
    subprocess.run(
        [
            "gdal_translate", "-of", "LCP",
            str(landfire), str(lcp_out),
            "-co", "LINEAR_UNIT=SET_FROM_SRS",
            "-co", "ELEVATION_UNIT=METERS",
            "-co", "SLOPE_UNIT=DEGREES",
            "-co", "ASPECT_UNIT=AZIMUTH_DEGREES",
            "-co", "CANOPY_COV_UNIT=PERCENT",
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    prj_text = subprocess.check_output(
        ["gdalsrsinfo", "-o", "wkt_esri", str(landfire)],
        text=True, stderr=subprocess.DEVNULL,
    )
    prj_out.write_text(prj_text)
    print("  landscape.lcp + .prj")

    # ---- ignition.shp --------------------------------------------------
    subprocess.run(
        [
            "ogr2ogr", "-f", "ESRI Shapefile",
            str(farsite_dir / "ignition.shp"),
            str(case_dir / "ignition_point.gpkg"),
            "ignition_point",
            "-t_srs", str(prj_out),
            "-overwrite",
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    fuels_tif = (case_dir / INPUTS / FBFM_FILENAME).with_suffix(".tif")
    snapped   = _snap_ignition_shp(farsite_dir / "ignition.shp", fuels_tif)
    print("  ignition.shp" + ("  (snapped to valid fuel)" if snapped else ""))

    # ---- barrier.shp (optional) ----------------------------------------
    if USE_BARRIER:
        barrier_shp = farsite_dir / "barrier.shp"
        for i, src in enumerate([
            case_dir / INPUTS / cfg.BARRIER_ROADS_CLIP_NAME,
            case_dir / INPUTS / cfg.BARRIER_WATER_CLIP_NAME,
            case_dir / INPUTS / cfg.BARRIER_BACKUP_CLIP_NAME,
        ]):
            cmd = ["ogr2ogr", "-f", "ESRI Shapefile", "-t_srs", str(prj_out)]
            if i > 0:
                cmd += ["-update", "-append"]
            cmd += [str(barrier_shp), str(src)]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("  barrier.shp")

    # ---- weather.wxs (trim conditioning days) --------------------------
    raw_lines        = wxs_path.read_text().splitlines()
    header_lines     = 4
    rows_to_remove   = PREIGNITION_DAYS * 24
    trimmed          = raw_lines[:header_lines] + raw_lines[header_lines + rows_to_remove:]
    new_wxs.write_text("\n".join(trimmed) + "\n")
    n_wx = len(trimmed) - header_lines
    print(f"  weather.wxs  ({n_wx} records, {PREIGNITION_DAYS} conditioning days removed)")

    # ---- output.fms (per-fuel mean moisture) ---------------------------
    fbfm,  fbfm_nodata,  _ = _read_band((case_dir / INPUTS / FBFM_FILENAME).with_suffix(".tif"))
    m1,    m1_nodata,    _ = _read_band((case_dir / INPUTS / M1_FILENAME).with_suffix(".tif"))
    m10,   m10_nodata,   _ = _read_band((case_dir / INPUTS / M10_FILENAME).with_suffix(".tif"))
    m100,  m100_nodata,  _ = _read_band((case_dir / INPUTS / M100_FILENAME).with_suffix(".tif"))

    valid = np.ones(fbfm.shape, dtype=bool)
    for arr, nd in [(fbfm, fbfm_nodata), (m1, m1_nodata), (m10, m10_nodata), (m100, m100_nodata)]:
        if nd is not None:
            valid &= (arr != nd)
    if not np.any(valid):
        raise ValueError(f"[{case_dir.name}] No valid pixels for FMS computation.")

    fbfm_int  = fbfm.astype(np.int64, copy=False)
    max_class = int(np.nanmax(fbfm_int[valid]))
    m1_mn,  counts = _means_by_class(fbfm_int, m1,   valid, max_class)
    m10_mn, _      = _means_by_class(fbfm_int, m10,  valid, max_class)
    m100_mn, _     = _means_by_class(fbfm_int, m100, valid, max_class)

    present   = np.where(counts > 0)[0]
    fms_lines = [
        f"{fuel} {m1_mn[fuel]:.{DECIMALS}f} {m10_mn[fuel]:.{DECIMALS}f} "
        f"{m100_mn[fuel]:.{DECIMALS}f} {LH_CONST} {LW_CONST}"
        for fuel in present
    ]
    # prepend fuel-0 default (copy of first real class with code 0)
    default_parts    = fms_lines[0].split()
    default_parts[0] = "0"
    fms_lines_full   = [" ".join(default_parts)] + fms_lines

    with fms_out.open("w", newline="\n") as f:
        f.write("\n".join(fms_lines_full) + "\n")
    print(f"  output.fms   ({len(present)} fuel classes)")

    # ---- farsite.txt ---------------------------------------------------
    _rel         = farsite_dir.relative_to(FIRE_ROOT)
    barrier_arg  = f"{_rel}/barrier.shp" if USE_BARRIER else "0"
    farsite_line = (
        f"{_rel}/landscape.lcp {_rel}/farsite.input "
        f"{_rel}/ignition.shp {barrier_arg} {_rel}/outputs/farsite 2"
    )
    (farsite_dir / "farsite.txt").write_text(farsite_line + "\n")

    # ---- farsite.input -------------------------------------------------
    wxs_file_lines                      = new_wxs.read_text().splitlines()
    start_dt, end_dt, wxs_data_lines    = _parse_wxs_first_last_datetimes(wxs_file_lines)
    burn_lines                          = _burn_period_lines(start_dt, end_dt)

    # ---- wind grids (ATM file) -----------------------------------------
    winds_dir   = farsite_dir / "winds"
    atm_entries = _build_wind_grids(
        case_dir / INPUTS / WS_TIF_NAME,
        case_dir / INPUTS / WD_TIF_NAME,
        winds_dir, start_dt, end_dt,
    )
    atm_path = farsite_dir / "winds.atm"
    with atm_path.open("w", newline="\n") as f:
        f.write("ENGLISH\n")
        for month, day, hhmm, ws_rel, wd_rel in atm_entries:
            f.write(f"{month} {day} {hhmm} {ws_rel} {wd_rel}\n")
    print(f"  winds.atm    ({len(atm_entries)} entries)")

    raws_elev       = next((ln for ln in wxs_file_lines if ln.startswith("RAWS_ELEVATION:")), "RAWS_ELEVATION: ")
    raws_units_raw  = next((ln for ln in wxs_file_lines if ln.startswith("RAWS_UNITS:")),     "RAWS_UNITS: ")
    pfx, _, val     = raws_units_raw.partition(":")
    val_lc          = val.strip().lower()
    raws_units      = f"{pfx}: {'Metric' if val_lc == 'metric' else 'English' if val_lc == 'english' else val.strip().title()}"

    content = [
        "FARSITE INPUTS FILE VERSION 1.0",
        f"FARSITE_START_TIME: {start_dt.month:02d} {start_dt.day:02d} {start_dt.hour:02d}{start_dt.minute:02d}",
        f"FARSITE_END_TIME: {end_dt.month:02d} {end_dt.day:02d} {end_dt.hour:02d}{end_dt.minute:02d}",
        "FARSITE_TIMESTEP: 60",
        "FARSITE_DISTANCE_RES: 60.0",
        "FARSITE_PERIMETER_RES: 60.0",
        "FARSITE_MIN_IGNITION_VERTEX_DISTANCE: 15.0",
        "FARSITE_SPOT_GRID_RESOLUTION: 30.0",
        "FARSITE_SPOT_PROBABILITY: 0.00",
        "FARSITE_SPOT_IGNITION_DELAY: 0",
        "FARSITE_MINIMUM_SPOT_DISTANCE: 60",
        "FARSITE_ACCELERATION_ON: 1",
        f"FARSITE_BURN_PERIODS: {len(burn_lines)}",
        *burn_lines,
        "",
        f"FUEL_MOISTURES_DATA: {len(fms_lines_full)}",
        *fms_lines_full,
        "",
        raws_elev,
        raws_units,
        "",
        f"RAWS: {len(wxs_data_lines)}",
        *wxs_data_lines,
        "",
        "FOLIAR_MOISTURE_CONTENT: 100",
        "CROWN_FIRE_METHOD: Finney",
        "FARSITE_ATM_FILE: winds.atm",
        "",
    ]
    farsite_input.write_text("\n".join(content) + "\n")
    print(f"  farsite.input  ({start_dt:%Y-%m-%d %H:%M} -> {end_dt:%Y-%m-%d %H:%M})")


def _write_run_all(fire_root: Path) -> None:
    """(Re)generate runAllFarsite.txt from all farsite.txt files under *fire_root*."""
    lines = []
    for d in sorted(fire_root.iterdir()):
        if d.is_dir() and d.name.isdigit():
            p = d / "farsite" / "farsite.txt"
            if p.exists():
                lines.append(p.read_text().strip())
    out = fire_root / "runAllFarsite.txt"
    out.write_text("\n".join(lines) + "\n")
    print(f"\nWrote {len(lines)} entries to {out}")


if __name__ == "__main__":
    for case_dir in sorted(FIRE_ROOT.iterdir()):
        if case_dir.is_dir() and case_dir.name.isdigit():
            main(case_dir)
    _write_run_all(FIRE_ROOT)
