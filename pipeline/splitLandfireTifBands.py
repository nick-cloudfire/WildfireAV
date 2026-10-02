"""
Step "split_bands": split LANDFIRE.tif into one GeoTIFF per band.

Band order matches the LFPS Layer_List (getLandfireProductsForFireSim.py) and
``pipelineConfig.LANDFIRE_BAND_FILE_NAMES``:
dem, slp, asp, fbfm40, cc, ch, cbh, cbd  ->  inputs/<name>.tif
"""

from pathlib import Path

import rasterio

import pipelineConfig as cfg
from common import atomic_write, for_each_case, require, skipped

BAND_FILE_NAMES = cfg.LANDFIRE_BAND_FILE_NAMES


def _process(folder: Path):
    tif_path = folder / "LANDFIRE.tif"
    require(tif_path, hint="run the landfire step first")
    inputs_dir = folder / cfg.INPUTS_SUBDIR_NAME
    inputs_dir.mkdir(parents=True, exist_ok=True)
    outs = [inputs_dir / f"{name}.tif" for name in BAND_FILE_NAMES]
    if all(p.exists() for p in outs):
        return skipped("band files already exist")

    with rasterio.open(tif_path) as src:
        if src.count < len(BAND_FILE_NAMES):
            raise ValueError(f"{tif_path.name} has {src.count} bands, expected {len(BAND_FILE_NAMES)} "
                             f"({', '.join(BAND_FILE_NAMES)}) — delete it to re-download")
        profile = {k: v for k, v in src.profile.items() if k not in ("blockxsize", "blockysize")}
        profile.update(count=1, driver="GTiff", compress="lzw", tiled=True, bigtiff="IF_SAFER")
        for band_idx, out_path in enumerate(outs, start=1):
            with atomic_write(out_path) as tmp, rasterio.open(tmp, "w", **profile) as dst:
                dst.write(src.read(band_idx), 1)
        print(f"  {src.width}x{src.height} @ {src.res[0]:.0f} m -> {', '.join(p.name for p in outs)}")


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
