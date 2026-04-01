"""
Step 3 of runPipelineParallel: create adjacency (adj.tif) and phi (phi.tif) rasters.

Both rasters are filled with 1.0 and match the DEM in shape, CRS, and transform.
They are required by Elmfire as static inputs.
"""

from pathlib import Path

import numpy as np
import rasterio

import pipelineConfig as cfg

FIRE_ROOT = cfg.FIRE_ROOT
INPUTS    = cfg.INPUTS_SUBDIR_NAME
DEM_NAME  = cfg.LANDFIRE_BAND_FILE_NAMES[0] + ".tif"
ADJ_NAME  = cfg.ADJ_FILE_NAME
PHI_NAME  = cfg.PHI_FILE_NAME
DTYPE     = cfg.RASTER_DTYPE
NODATA    = cfg.RASTER_NODATA


def _create_for_folder(folder: Path) -> None:
    inputs  = folder / INPUTS
    dem_path = inputs / DEM_NAME
    if not dem_path.exists():
        print(f"  DEM not found at {dem_path}, skipping.")
        return

    if (inputs / ADJ_NAME).exists() and (inputs / PHI_NAME).exists():
        print(f"  Skipped — adj.tif and phi.tif already exist.")
        return

    with rasterio.open(dem_path) as src:
        profile = src.profile.copy()

    profile.update(
        dtype=DTYPE, count=1, nodata=NODATA,
        compress="lzw", tiled=True, bigtiff="IF_SAFER",
    )
    ones = np.ones((profile["height"], profile["width"]), dtype=DTYPE)

    for name in (ADJ_NAME, PHI_NAME):
        out_path = inputs / name
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(ones, 1)
        print(f"  Wrote {out_path}")


def main(case_dir=None) -> None:
    if case_dir is not None:
        folder = Path(case_dir)
        print(f"\nFolder {folder.name}:")
        _create_for_folder(folder)
        return

    root = Path(FIRE_ROOT)
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and folder.name.isdigit():
            print(f"\nFolder {folder.name}:")
            _create_for_folder(folder)


if __name__ == "__main__":
    main()
