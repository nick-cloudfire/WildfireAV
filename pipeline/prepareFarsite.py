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

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import rasterio

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

USE_BARRIER = True   # set False to omit barrier.shp from farsite.input

LH_CONST    = 60
LW_CONST    = 90
M1000_CONST = 16
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

    with rasterio.open(landfire) as ds:
        cellsize = ds.res[0]
    gridded_winds_res = int(cellsize * cfg.WINDNINJA_MESH_RESOLUTION_FACTOR)

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
    print("  ignition.shp")

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
        f"{m100_mn[fuel]:.{DECIMALS}f} {LH_CONST} {LW_CONST} {M1000_CONST}"
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
        "GRIDDED_WINDS_GENERATE: Yes",
        f"GRIDDED_WINDS_RESOLUTION: {gridded_winds_res}",
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
