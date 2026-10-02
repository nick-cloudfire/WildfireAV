"""
Step "adj_phi": write the static ELMFIRE inputs adj.tif and phi.tif.

Both are filled with 1.0 on the DEM grid (same shape, CRS, transform).
"""

from pathlib import Path

import numpy as np
import rasterio

import pipelineConfig as cfg
from common import atomic_write, for_each_case, require, skipped

DEM_NAME = cfg.LANDFIRE_BAND_FILE_NAMES[0] + ".tif"


def _process(folder: Path):
    inputs = folder / cfg.INPUTS_SUBDIR_NAME
    dem_path = inputs / DEM_NAME
    require(dem_path, hint="run the split_bands step first")
    outs = [inputs / cfg.ADJ_FILE_NAME, inputs / cfg.PHI_FILE_NAME]
    if all(p.exists() for p in outs):
        return skipped("adj.tif and phi.tif already exist")

    with rasterio.open(dem_path) as src:
        profile = {k: v for k, v in src.profile.items() if k not in ("blockxsize", "blockysize")}
        profile.update(dtype=cfg.RASTER_DTYPE, count=1, nodata=cfg.RASTER_NODATA,
                       compress="lzw", tiled=True, bigtiff="IF_SAFER")
    ones = np.ones((profile["height"], profile["width"]), dtype=cfg.RASTER_DTYPE)
    for out_path in outs:
        with atomic_write(out_path) as tmp, rasterio.open(tmp, "w", **profile) as dst:
            dst.write(ones, 1)
    print(f"  Wrote {', '.join(p.name for p in outs)}")


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
