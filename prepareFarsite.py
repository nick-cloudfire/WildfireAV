#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Feb 17 16:19:00 2026

@author: nick
"""

import pipelineConfig as cfg
from pathlib import Path
import subprocess
import shutil
import numpy as np
import rasterio
from datetime import datetime, timedelta

FIRE_ROOT = Path(cfg.FIRE_ROOT)

PREIGNITION_DAYS = cfg.CONDITIONING_DAYS
M1_FILENAME = cfg.FMC_FILE_NAMES[0]
M10_FILENAME = cfg.FMC_FILE_NAMES[1]
M100_FILENAME = cfg.FMC_FILE_NAMES[2]
FBFM_FILENAME = cfg.LANDFIRE_BAND_FILE_NAMES[3]

INPUTS = cfg.INPUTS_SUBDIR_NAME

farsiteFolder = Path("farsite")
landfire = Path("LANDFIRE.tif")
lcp_out = farsiteFolder / Path("landscape.lcp")
prj_out = farsiteFolder / Path("landscape.prj")
wxs_path = INPUTS / Path(cfg.WXS_FILE_NAME)
new_wxs_path = farsiteFolder / Path("weather.wxs")
OUT_FMS = farsiteFolder / Path("output.fms")

LH_CONST = 60
LW_CONST = 90
M1000_CONST = 16

DECIMALS = 0

def _read_band(path: Path):
    with rasterio.open(path) as ds:
        arr = ds.read(1)
        nodata = ds.nodata
        profile = ds.profile
    return arr, nodata, profile


def _means_by_class(classes, values, valid_mask, max_class):
    """
    Compute mean(values) grouped by integer class codes using bincount.
    """
    c = classes[valid_mask].astype(np.int64)
    v = values[valid_mask].astype(np.float64)

    sums = np.bincount(c, weights=v, minlength=max_class + 1)
    cnts = np.bincount(c, minlength=max_class + 1)

    means = np.full(max_class + 1, np.nan, dtype=np.float64)
    ok = cnts > 0
    means[ok] = sums[ok] / cnts[ok]
    return means, cnts



def _parse_wxs_first_last_datetimes(wxs_lines):
    """
    Expects wxs format like your sample:
      line0: RAWS_ELEVATION: ...
      line1: RAWS_UNITS: ...
      line2: RAWS_WINDS: ... (optional)
      line3: header: Year Mth Day Time ...
      line4+: data: YYYY MM DD HHMM ...
    Returns (start_dt, end_dt) as datetime objects.
    """
    # Find first data line by skipping non-data lines until a line starts with a year
    data_lines = []
    for ln in wxs_lines:
        s = ln.strip()
        if not s:
            continue
        # crude but effective: data lines begin with 4-digit year
        if len(s) >= 4 and s[:4].isdigit():
            data_lines.append(s)

    if not data_lines:
        raise ValueError("No data lines found in WXS file.")

    def parse_dt(data_line):
        parts = data_line.split()
        # YYYY MM DD HHMM
        y = int(parts[0]); m = int(parts[1]); d = int(parts[2])
        hhmm = parts[3].zfill(4)
        hh = int(hhmm[:2]); mm = int(hhmm[2:])
        return datetime(y, m, d, hh, mm)

    start_dt = parse_dt(data_lines[0])
    end_dt = parse_dt(data_lines[-1])
    return start_dt, end_dt, data_lines


def _burn_period_lines(start_dt, end_dt):
    """
    Create daily burn period lines from start_dt to end_dt.
    Rules:
      - If spans multiple days:
          first day: start_time -> 2359
          middle days: 0000 -> 2359
          last day: 0000 -> end_time
      - If within one day:
          that day: start_time -> end_time
    Output lines are strings: "MM DD HHMM HHMM"
    """
    def hhmm(dt):
        return f"{dt.hour:02d}{dt.minute:02d}"

    start_date = start_dt.date()
    end_date = end_dt.date()

    lines = []
    d = start_date
    while d <= end_date:
        if start_date == end_date:
            # single-day run
            s = hhmm(start_dt)
            e = hhmm(end_dt)
        else:
            if d == start_date:
                s = hhmm(start_dt)
                e = "2359"
            elif d == end_date:
                s = "0000"
                e = hhmm(end_dt)
            else:
                s = "0000"
                e = "2359"

        lines.append(f"{d.month:02d} {d.day:02d} {s} {e}")
        d = d + timedelta(days=1)

    return lines

for caseId in sorted(FIRE_ROOT.iterdir()):
    if caseId.is_dir() and caseId.name.isdigit():
        print(f'================ Folder {caseId} ================')
        farsiteFolder = caseId / Path("farsite")
        landfire = caseId / Path("LANDFIRE.tif")
        lcp_out = farsiteFolder / Path("landscape.lcp")
        prj_out = farsiteFolder / Path("landscape.prj")
        wxs_path = caseId / INPUTS / Path(cfg.WXS_FILE_NAME)
        new_wxs_path = farsiteFolder / Path("weather.wxs")
        OUT_FMS = farsiteFolder / Path("output.fms")
                        
        # make farsite folder
        shutil.rmtree(farsiteFolder, ignore_errors=True)
        farsiteFolder.mkdir()
        farsiteOutput = farsiteFolder / Path("outputs")
        shutil.rmtree(farsiteOutput, ignore_errors=True)
        farsiteOutput.mkdir()
        print("---- Made FARSITE directory")
        
        # translate LANDFIRE geotiff to LCP
        cmd_translate = [
            "gdal_translate",
            "-of", "LCP",
            str(landfire),
            str(lcp_out),
            "-co", "LINEAR_UNIT=SET_FROM_SRS",
            "-co", "ELEVATION_UNIT=METERS",
            "-co", "SLOPE_UNIT=DEGREES",
            "-co", "ASPECT_UNIT=AZIMUTH_DEGREES",
            "-co", "CANOPY_COV_UNIT=PERCENT",
        ]
        subprocess.run(cmd_translate, check=True)
        print("---- Translated LANDFIRE GeoTIFF")
        
        # create LCP projection file
        cmd_srsinfo = ["gdalsrsinfo", "-o", "wkt_esri", str(landfire)]
        prj_text = subprocess.check_output(cmd_srsinfo, text=True)
        prj_out.write_text(prj_text)
        print("---- Created Landscape Projection File")
        
        # reproject ignition point
        cmd = [
            "ogr2ogr",
            "-f", "ESRI Shapefile",
            str(farsiteFolder / Path("ignition.shp")),
            str(caseId / Path("ignition_point.gpkg")),
            "ignition_point",
            "-t_srs", str(farsiteFolder / Path("landscape.prj")),
            "-overwrite",
        ]
        subprocess.run(cmd, check=True)
        print("---- Reprojected ignition point")
        
        # crop wxs file
        lines = wxs_path.read_text().splitlines()
        header_lines = 4  # lines 0–3
        records_per_day = 24
        rows_to_remove = PREIGNITION_DAYS * records_per_day
        new_lines = (
            lines[:header_lines] +
            lines[header_lines + rows_to_remove:]
        )
        new_wxs_path.write_text("\n".join(new_lines) + "\n")
        print("---- Cropped & Copied WXS File")
        
        # create fms file
        fbfm, fbfm_nodata, ref_prof = _read_band(Path(caseId / INPUTS / FBFM_FILENAME).with_suffix(".tif"))
        m1, m1_nodata, _ = _read_band(Path(caseId / INPUTS / M1_FILENAME).with_suffix(".tif"))
        m10, m10_nodata, _ = _read_band(Path(caseId / INPUTS / M10_FILENAME).with_suffix(".tif"))
        m100, m100_nodata, _ = _read_band(Path(caseId / INPUTS / M100_FILENAME).with_suffix(".tif"))

        valid = np.ones(fbfm.shape, dtype=bool)
    
        if fbfm_nodata is not None:
            valid &= (fbfm != fbfm_nodata)
        if m1_nodata is not None:
            valid &= (m1 != m1_nodata)
        if m10_nodata is not None:
            valid &= (m10 != m10_nodata)
        if m100_nodata is not None:
            valid &= (m100 != m100_nodata)
    
        if not np.any(valid):
            raise ValueError("No valid pixels found after applying nodata / filters.")

        fbfm_int = fbfm.astype(np.int64, copy=False)
        max_class = int(np.nanmax(fbfm_int[valid]))
        m1_mean, counts = _means_by_class(fbfm_int, m1, valid, max_class)
        m10_mean, _     = _means_by_class(fbfm_int, m10, valid, max_class)
        m100_mean, _    = _means_by_class(fbfm_int, m100, valid, max_class)

        present = np.where(counts > 0)[0]
        
        lines = []
        
        for fuel in present:
            lines.append(
                f"{fuel} "
                f"{m1_mean[fuel]:.{DECIMALS}f} "
                f"{m10_mean[fuel]:.{DECIMALS}f} "
                f"{m100_mean[fuel]:.{DECIMALS}f} "
                f"{LH_CONST} {LW_CONST} {M1000_CONST}"
            )
        
        first_real = lines[0].split()
        first_real[0] = "0"
        default_line = " ".join(first_real)
    
        with OUT_FMS.open("w", newline="\n") as f:
            f.write(default_line + "\n")
            for ln in lines:
                f.write(ln + "\n")
        print("---- Created FMS File")
        
        # create farsite.txt
        farsite_txt_path = farsiteFolder / "farsite.txt"
        
        farsite_line = f"//wsl.localhost/Ubuntu-24.04/{farsiteFolder}/landscape.lcp //wsl.localhost/Ubuntu-24.04/{farsiteFolder}/farsite.input //wsl.localhost/Ubuntu-24.04/{farsiteFolder}/ignition.shp 0 //wsl.localhost/Ubuntu-24.04/{farsiteFolder}/outputs/farsite 2"
        farsite_txt_path.write_text(farsite_line + "\n")
        print("---- Created farsite.txt")
        
        # create farsite.input
        wxs_lines = new_wxs_path.read_text().splitlines()
        start_dt, end_dt, wxs_data_lines = _parse_wxs_first_last_datetimes(wxs_lines)
        burn_lines = _burn_period_lines(start_dt, end_dt)
    
        # Pull RAWS_ELEVATION / RAWS_UNITS from wxs header if present
        # Default to empty if missing
        raws_elev = next((ln for ln in wxs_lines if ln.startswith("RAWS_ELEVATION:")), "RAWS_ELEVATION: ")
        raws_units_line = next((ln for ln in wxs_lines if ln.startswith("RAWS_UNITS:")), "RAWS_UNITS: ")

        # Normalize to "Metric" / "English" (title-case) while keeping the prefix
        prefix, _, value = raws_units_line.partition(":")
        value_norm = value.strip().lower()
        
        if value_norm == "metric":
            raws_units = f"{prefix}: Metric"
        elif value_norm == "english":
            raws_units = f"{prefix}: English"
        else:
            # fallback: title-case whatever it is
            raws_units = f"{prefix}: {value.strip().title()}"
        # Read FMS lines (you already include fuel 0 default line)
        fms_lines = [ln.strip() for ln in OUT_FMS.read_text().splitlines() if ln.strip()]
    
        out_path = farsiteFolder / "farsite.input"
    
        # Build farsite.input content
        # Keep your constants; only dynamic: start/end + burn periods + inline fms/wxs
        content_lines = [
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
            "",  # blank line
    
            f"FUEL_MOISTURES_DATA: {len(fms_lines)}",
            *fms_lines,   # inline ALL fms records
            "",  # blank line
    
            raws_elev,
            raws_units,
            "",
    
            f"RAWS: {len(wxs_data_lines)}",
            *wxs_data_lines,  # inline weather data rows only (no column header)
            "",
    
            "FOLIAR_MOISTURE_CONTENT: 100",
            "CROWN_FIRE_METHOD: Finney",
            "",
        ]
    
        out_path.write_text("\n".join(content_lines) + "\n")
        print(f"---- Created farsite.input ({out_path})")