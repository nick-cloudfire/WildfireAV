# -*- coding: utf-8 -*-
"""
Split LANDFIRE.tif into individual band files for each fire folder.

For each folder under FIREPAIRS_ROOT:
    - Look for LANDFIRE.tif
    - Create an "inputs" subfolder
    - Save each band as:
        dem.tif, slp.tif, asp.tif, fbfm40.tif,
        cc.tif, ch.tif, cbh.tif, cbd.tif

Assumes the LFPS Layer_List order used in the downloader is:
    [ELEV2020,
     SLPD2020,
     ASP2020,
     <version>FBFM40<suffix>,
     <version>CC<suffix>,
     <version>CH<suffix>,
     <version>CBH<suffix>,
     <version>CBD<suffix>]

So band indices map to:
    1 -> dem
    2 -> slp
    3 -> asp
    4 -> fbfm40
    5 -> cc
    6 -> ch
    7 -> cbh
    8 -> cbd
"""

from pathlib import Path
import pipelineConfig
import rasterio

FIREPAIRS_ROOT      = pipelineConfig.FIRE_ROOT
INPUTS_SUBFOLDER    = pipelineConfig.INPUTS_SUBDIR_NAME
BAND_FILE_NAMES     = pipelineConfig.LANDFIRE_BAND_FILE_NAMES

def process_folder(folder: Path):
    """
    For a single fire folder:
        - open LANDFIRE.tif
        - log band descriptions (if present)
        - create /inputs
        - write each band as a separate single-band GeoTIFF
    """
    tif_path = folder / "LANDFIRE.tif"
    if not tif_path.exists():
        print(f"No LANDFIRE.tif in {folder}, skipping.")
        return

    inputs_dir = folder / INPUTS_SUBFOLDER
    inputs_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Processing {tif_path} ===")

    with rasterio.open(tif_path) as src:
        band_count = src.count
        if band_count < len(BAND_FILE_NAMES):
            print(
                f"  Warning: expected at least {len(BAND_FILE_NAMES)} bands "
                f"(terrain + fuels), but raster has {band_count}. "
                f"Only splitting first {band_count}."
            )

        # Base profile for single-band outputs
        base_profile = src.profile.copy()
        base_profile.update(count=1)

        # Loop over bands and output names
        for band_index, out_name in enumerate(BAND_FILE_NAMES, start=1):
            if band_index > band_count:
                break
            out_path = inputs_dir / f"{out_name}.tif"
            data = src.read(band_index)
            profile = base_profile.copy()
            profile["driver"] = "GTiff"
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(data, 1)
            print(f"  Wrote band {band_index} -> {out_path}")

def main():
    root = Path(FIREPAIRS_ROOT)
    folders = sorted(p for p in root.iterdir() if p.is_dir())
    for folder in folders:
        process_folder(folder)

if __name__ == "__main__":
    main()
