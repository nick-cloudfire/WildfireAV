#!/usr/bin/env python3
"""
Step 6 of runPipelineParallel: compute dead-fuel moisture with the Nelson model.

For each case:
1. Convert input GeoTIFFs to BSQ format (GDAL/ENVI requirement for the C# model).
2. Run the Nelson C# executable to produce m1/m10/m100 BSQ files.
3. Convert BSQ outputs back to compressed GeoTIFFs.
4. Remove all intermediate BSQ/HDR/XML files.

Inputs (from inputs/ folder)
-----------------------------
- weather.wxs, dem.tif, slp.tif, asp.tif, cc.tif

Outputs (in inputs/ folder)
----------------------------
- m1.tif, m10.tif, m100.tif  (1-hr, 10-hr, 100-hr dead fuel moisture)
"""

from pathlib import Path

import pipelineConfig as cfg
from parallel_api import run_subprocess

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIRE_ROOT       = Path(cfg.FIRE_ROOT)
INPUTS          = cfg.INPUTS_SUBDIR_NAME
RAWS_FILENAME   = cfg.WXS_FILE_NAME
PREIGNITION_DAYS = cfg.CONDITIONING_DAYS
NELSON_EXE      = Path(cfg.NELSON_EXE)

# Input band filenames (without extension)
CC_FILENAME     = cfg.LANDFIRE_BAND_FILE_NAMES[4]   # cc
DEM_FILENAME    = cfg.LANDFIRE_BAND_FILE_NAMES[0]   # dem
SLOPE_FILENAME  = cfg.LANDFIRE_BAND_FILE_NAMES[1]   # slp
ASPECT_FILENAME = cfg.LANDFIRE_BAND_FILE_NAMES[2]   # asp

# Output filenames (without extension)
M1_FILENAME     = cfg.FMC_FILE_NAMES[0]
M10_FILENAME    = cfg.FMC_FILE_NAMES[1]
M100_FILENAME   = cfg.FMC_FILE_NAMES[2]


# ---------------------------------------------------------------------------
# Format-conversion helpers
# ---------------------------------------------------------------------------

def _to_bsq(input_tif: Path, output_bsq: Path) -> None:
    run_subprocess(
        [
            "gdal_translate",
            "--config", "GDAL_CACHEMAX", "512",
            "-of", "ENVI",
            "-co", "INTERLEAVE=BSQ",
            str(input_tif),
            str(output_bsq),
        ],
    )


def _to_geotiff(input_bsq: Path, output_tif: Path) -> None:
    run_subprocess(
        [
            "gdal_translate",
            "--config", "GDAL_CACHEMAX", "512",
            "-of", "GTiff",
            "-co", "COMPRESS=ZSTD",
            "-co", "BIGTIFF=YES",
            "-co", "NUM_THREADS=8",
            str(input_bsq),
            str(output_tif),
        ],
    )


def _delete_bsq_hdr_xml(folder: Path) -> None:
    for ext in ("*.bsq", "*.hdr", "*.xml"):
        for f in folder.glob(ext):
            f.unlink()


# ---------------------------------------------------------------------------
# Per-case processor
# ---------------------------------------------------------------------------

def process_case(case_dir: Path) -> None:
    case_dir   = Path(case_dir)
    inputs_dir = case_dir / INPUTS
    raws_file  = inputs_dir / RAWS_FILENAME

    if (inputs_dir / f"{M1_FILENAME}.tif").exists():
        print(f"  Skipped — {M1_FILENAME}.tif already exists.")
        return

    cc_file     = inputs_dir / CC_FILENAME
    dem_file    = inputs_dir / DEM_FILENAME
    slope_file  = inputs_dir / SLOPE_FILENAME
    aspect_file = inputs_dir / ASPECT_FILENAME
    m1_file     = inputs_dir / M1_FILENAME
    m10_file    = inputs_dir / M10_FILENAME
    m100_file   = inputs_dir / M100_FILENAME

    # Clean up any leftover intermediates from a previous run
    _delete_bsq_hdr_xml(inputs_dir)

    # Convert tiffs to BSQ
    for base in (cc_file, dem_file, slope_file, aspect_file):
        _to_bsq(base.with_suffix(".tif"), base.with_suffix(".bsq"))

    # Run Nelson model
    cmd = [
        str(NELSON_EXE),
        str(raws_file),
        str(dem_file.with_suffix(".bsq")),
        str(slope_file.with_suffix(".bsq")),
        str(aspect_file.with_suffix(".bsq")),
        str(cc_file.with_suffix(".bsq")),
        str(PREIGNITION_DAYS),
    ]
    run_subprocess(cmd, cwd=str(NELSON_EXE.parent))

    # Convert outputs to GeoTIFF
    for base in (m1_file, m10_file, m100_file):
        _to_geotiff(base.with_suffix(".bsq"), base.with_suffix(".tif"))

    # Clean up intermediates
    _delete_bsq_hdr_xml(inputs_dir)


def main(case_dir=None) -> None:
    if case_dir is not None:
        process_case(Path(case_dir))
        return

    for folder in sorted(FIRE_ROOT.iterdir()):
        if folder.is_dir() and folder.name.isdigit():
            print(f"\nFolder {folder.name}:")
            process_case(folder)


if __name__ == "__main__":
    main()
