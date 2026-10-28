#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 9 of runPipelineParallel: prepare FARSITE inputs for one case.

For each case:
- Converts LANDFIRE.tif → landscape.lcp + landscape.prj  (gdal_translate / gdalsrsinfo)
- Reprojects ignition_point.gpkg → ignition.shp          (ogr2ogr)
- Merges pre-clipped barrier layers → barrier.shp         (ogr2ogr, optional)
- Computes per-fuel-class mean dead fuel moisture at the ignition hour → output.fms
- Resamples the hourly WindNinja grids to winds/*.asc + winds.atm
- Writes farsite.txt  (the one-line command file FARSITE is started with)
- Writes farsite.input (run parameters, weather from the ignition hour on, moisture)

FARSITE simulates exactly ELMFIRE's window, SatelliteIgnitionTime ->
EventEndTime, so both models burn for the same time.  Input format:
docs/farsite_input_reference.md.

Standalone usage (process all cases under FIRE_ROOT):
    python prepareFarsite.py

Or from the pipeline via main(case_dir).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import fiona
import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import reproject, Resampling as RioResampling

import pipelineConfig as cfg
from case_metadata import read_case_metadata
from common import (atomic_write, barrier_cells, case_window, for_each_case, ignition_hour, read_wxs, require,
                    require_starts_at, skipped, snap_to_valid_fuel)
from parallel_api import hours
from liveFuelMoisture import live_moisture

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

M1_FILENAME      = cfg.FMC_FILE_NAMES[0]
M10_FILENAME     = cfg.FMC_FILE_NAMES[1]
M100_FILENAME    = cfg.FMC_FILE_NAMES[2]
FBFM_FILENAME    = cfg.LANDFIRE_BAND_FILE_NAMES[3]
INPUTS           = cfg.INPUTS_SUBDIR_NAME
MESH_RES_FACTOR  = cfg.WINDNINJA_MESH_RESOLUTION_FACTOR
WS_TIF_NAME      = cfg.WS_TIF_NAME
WD_TIF_NAME      = cfg.WD_TIF_NAME

USE_BARRIER = True   # set False to omit barrier.shp from farsite.input

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    """Run a GDAL/OGR command; on failure raise with its stderr (not swallowed)."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=hours(cfg.GDAL_TIMEOUT_H))
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (exit {r.returncode}): {r.stderr.strip()[-500:]}")
    return r.stdout


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


def _raws_lines(wxs, first) -> list[str]:
    """Hourly weather rows from *first* on, in the RAWS format FARSITE reads (integer precipitation)."""
    df = read_wxs(wxs)
    df = df[df.index >= first]
    return [f"{t.year} {t.month} {t.day} {t.hour * 100 + t.minute:04d} {r.temp_C:.0f} {r.rh:.0f} "
            f"{round(r.pcp)} {r.wind_kph:.0f} {r.wind_dir_deg:.0f} {r.cloud_pct:.0f}"
            for t, r in df.iterrows()]


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


def _snap_ignition_shp(ignition_shp: Path, fuels_tif: Path, blocked: np.ndarray) -> bool:
    """Snap the ignition shapefile point in place to the nearest burnable cell off a barrier.

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

    x_snap, y_snap, moved = snap_to_valid_fuel(fuels_tif, x, y, blocked=blocked)
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


def _band_interleaved(tif: Path, workdir: Path) -> Path:
    """tif, or a band-interleaved copy of it in workdir if it is pixel-interleaved.

    Reading one band of a pixel-interleaved stack decompresses every band, so
    going through a long fire hour by hour took hours (ws/wd.tif made before
    wn_to_geotiff wrote them band-interleaved).
    """
    with rasterio.open(tif) as ds:
        if ds.count == 1 or ds.profile.get("interleave", "band") == "band":
            return tif
    copy = workdir / f"_{tif.name}"
    _run(["gdal_translate", "-q", "-co", "INTERLEAVE=BAND", "-co", "COMPRESS=LZW", "-co", "TILED=YES",
          "-co", "BIGTIFF=IF_SAFER", str(tif), str(copy)])
    return copy


def _build_wind_grids(ws_tif: Path, wd_tif: Path, winds_dir: Path, band_times, end_dt) -> list:
    """Resample ws.tif/wd.tif bands valid up to end_dt to coarser ASC grids; return ATM entries.

    Output resolution = source cellsize × MESH_RES_FACTOR (matches WindNinja mesh).
    Returns list of (month, day, hhmm, ws_rel_str, wd_rel_str) tuples where
    paths are relative to winds_dir.parent (i.e. farsite_dir).  FARSITE uses
    each grid from its time until the next one.
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

        used = [ts for ts in band_times if ts <= end_dt]
        print(f"  Wind grids: {len(used)} hourly bands → {out_width}×{out_height} @ {out_cellsize:.0f}m")

        for i, ts in enumerate(used):

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

def _process(case_dir: Path):
    """Prepare all FARSITE inputs for *case_dir*.  Skips if already done."""
    farsite_dir   = case_dir / "farsite"
    farsite_input = farsite_dir / "farsite.input"

    if farsite_input.exists():
        return skipped("farsite.input already exists")

    # ---- required inputs check ----------------------------------------
    meta = read_case_metadata(case_dir)
    start_dt, end_dt = case_window(meta, cfg.COL_SATELLITE_IGNITION, cfg.EVENT_END_COL)   # = ELMFIRE's
    first = ignition_hour(meta)
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
    require(*required, hint="produced by earlier steps")
    wind_times = require_starts_at(case_dir / INPUTS / WS_TIF_NAME, first, "windninja")
    require_starts_at((case_dir / INPUTS / M1_FILENAME).with_suffix(".tif"), first, "nelson")

    landfire    = case_dir / "LANDFIRE.tif"
    lcp_out     = farsite_dir / "landscape.lcp"
    prj_out     = farsite_dir / "landscape.prj"
    wxs_path    = case_dir / INPUTS / cfg.WXS_FILE_NAME
    fms_out     = farsite_dir / "output.fms"

    # ---- directory setup -----------------------------------------------
    shutil.rmtree(farsite_dir, ignore_errors=True)
    farsite_dir.mkdir()
    (farsite_dir / "outputs").mkdir()

    # ---- landscape.lcp + .prj ------------------------------------------
    _run(
        [
            "gdal_translate", "-q", "-of", "LCP",
            str(landfire), str(lcp_out),
            "-co", "LINEAR_UNIT=SET_FROM_SRS",
            "-co", "ELEVATION_UNIT=METERS",
            "-co", "SLOPE_UNIT=DEGREES",
            "-co", "ASPECT_UNIT=AZIMUTH_DEGREES",
            "-co", "CANOPY_COV_UNIT=PERCENT",
        ],
    )
    prj_text = _run(["gdalsrsinfo", "-o", "wkt_esri", str(landfire)])
    prj_out.write_text(prj_text)
    print("  landscape.lcp + .prj")

    # ---- ignition.shp --------------------------------------------------
    _run(
        [
            "ogr2ogr", "-f", "ESRI Shapefile",
            str(farsite_dir / "ignition.shp"),
            str(case_dir / "ignition_point.gpkg"),
            "ignition_point",
            "-t_srs", str(prj_out),
            "-overwrite",
        ],
    )
    fuels_tif = (case_dir / INPUTS / FBFM_FILENAME).with_suffix(".tif")
    snapped   = _snap_ignition_shp(farsite_dir / "ignition.shp", fuels_tif, barrier_cells(case_dir, fuels_tif))
    print("  ignition.shp" + ("  (snapped to burnable fuel clear of barriers)" if snapped else ""))

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
            _run(cmd)
        print("  barrier.shp")

    # ---- output.fms (per-fuel mean dead moisture, band 1 = ignition hour) ----
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
    herb_mc, woody_mc = live_moisture(case_dir)   # same values as ELMFIRE
    fms_lines = [
        f"{fuel} {m1_mn[fuel]:.0f} {m10_mn[fuel]:.0f} {m100_mn[fuel]:.0f} {herb_mc:.0f} {woody_mc:.0f}"
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
    # Paths are relative to farsite_dir since wine runs with cwd=farsite_dir.
    barrier_arg  = "barrier.shp" if USE_BARRIER else "0"
    farsite_line = (
        f"landscape.lcp farsite.input "
        f"ignition.shp {barrier_arg} outputs/farsite 2"
    )
    (farsite_dir / "farsite.txt").write_text(farsite_line + "\n")

    # ---- farsite.input -------------------------------------------------
    wxs_file_lines = wxs_path.read_text().splitlines()
    wxs_data_lines = _raws_lines(wxs_path, first)
    burn_lines     = _burn_period_lines(start_dt, end_dt)

    # ---- wind grids (ATM file) -----------------------------------------
    winds_dir   = farsite_dir / "winds"
    ws_tif = _band_interleaved(case_dir / INPUTS / WS_TIF_NAME, farsite_dir)
    wd_tif = _band_interleaved(case_dir / INPUTS / WD_TIF_NAME, farsite_dir)
    atm_entries = _build_wind_grids(ws_tif, wd_tif, winds_dir, wind_times, end_dt)
    for copy in {ws_tif, wd_tif} - {case_dir / INPUTS / WS_TIF_NAME, case_dir / INPUTS / WD_TIF_NAME}:
        copy.unlink()
    atm_path = farsite_dir / "winds.atm"
    with atm_path.open("w", newline="\n") as f:
        f.write("ENGLISH\n")
        for month, day, hhmm, ws_rel, wd_rel in atm_entries:
            f.write(f"{month} {day} {hhmm} {ws_rel} {wd_rel}\n")
    print(f"  winds.atm    ({len(atm_entries)} entries)")

    raws_elev  = next(ln for ln in wxs_file_lines if ln.startswith("RAWS_ELEVATION:"))
    raws_units = "RAWS_UNITS: Metric"      # weather.wxs is written in metric (downloadWeatherData)

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
    with atomic_write(farsite_input) as tmp:   # farsite.input is the step's done-marker
        tmp.write_text("\n".join(content) + "\n")
    print(f"  farsite.input  ({start_dt:%Y-%m-%d %H:%M} -> {end_dt:%Y-%m-%d %H:%M})")


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
