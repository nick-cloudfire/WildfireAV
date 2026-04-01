# splitLandfireTifBands.py
"""
Step 2 of runPipelineParallel: split LANDFIRE.tif into individual band files.

The LFPS download produces a single multi-band GeoTIFF.  Band order matches
the Layer_List submitted in getLandfireProductsForFireSim.py and is reflected
in pipelineConfig.LANDFIRE_BAND_FILE_NAMES:

    Band 1 → dem.tif   (ELEV2020)
    Band 2 → slp.tif   (SLPD2020)
    Band 3 → asp.tif   (ASP2020)
    Band 4 → fbfm40.tif
    Band 5 → cc.tif
    Band 6 → ch.tif
    Band 7 → cbh.tif
    Band 8 → cbd.tif

Output: inputs/<band_name>.tif  (single-band GeoTIFF, same CRS/transform as source)
"""

from pathlib import Path

import rasterio

import pipelineConfig

FIREPAIRS_ROOT  = pipelineConfig.FIRE_ROOT
INPUTS_SUBDIR   = pipelineConfig.INPUTS_SUBDIR_NAME
BAND_FILE_NAMES = pipelineConfig.LANDFIRE_BAND_FILE_NAMES


def process_folder(folder: Path) -> None:
    tif_path = folder / "LANDFIRE.tif"
    if not tif_path.exists():
        print(f"  No LANDFIRE.tif in {folder}, skipping.")
        return

    inputs_dir = folder / INPUTS_SUBDIR
    inputs_dir.mkdir(parents=True, exist_ok=True)

    first_band = inputs_dir / f"{BAND_FILE_NAMES[0]}.tif"
    if first_band.exists():
        print(f"  Skipped — {first_band.name} already exists.")
        return

    print(f"\nSplitting {tif_path} …")
    with rasterio.open(tif_path) as src:
        band_count = src.count
        if band_count < len(BAND_FILE_NAMES):
            print(
                f"  Warning: expected ≥{len(BAND_FILE_NAMES)} bands "
                f"but raster has {band_count}. Splitting available bands only."
            )

        base_profile = src.profile.copy()
        base_profile.update(
            count=1, driver="GTiff",
            compress="lzw", tiled=True, bigtiff="IF_SAFER",
        )

        for band_idx, name in enumerate(BAND_FILE_NAMES, start=1):
            if band_idx > band_count:
                break
            out_path = inputs_dir / f"{name}.tif"
            data = src.read(band_idx)
            with rasterio.open(out_path, "w", **base_profile) as dst:
                dst.write(data, 1)
            print(f"  Band {band_idx} → {out_path.name}")


def main(case_dir=None) -> None:
    if case_dir is not None:
        process_folder(Path(case_dir))
        return

    root = Path(FIREPAIRS_ROOT)
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit()):
        process_folder(folder)


if __name__ == "__main__":
    main()
