"""
Create adj.tif and phi.tif for each FirePairs folder.

- Reads inputs/dem.tif
- Creates adj.tif and phi.tif with:
    - same shape, CRS, transform
    - dtype float32
    - all pixel values = 1.0
"""

from pathlib import Path
import numpy as np
import rasterio
import pipelineConfig

# Root folder with numbered subfolders (00001, 00002, ...)
FIRE_ROOT   = pipelineConfig.FIRE_ROOT
INPUTS      = pipelineConfig.INPUTS_SUBDIR_NAME
DEM         = pipelineConfig.LANDFIRE_BAND_FILE_NAMES[0]
ADJ         = pipelineConfig.ADJ_FILE_NAME
PHI         = pipelineConfig.PHI_FILE_NAME

def create_adj_phi_for_folder(folder: Path):
    inputs = folder / INPUTS
    dem_path = inputs / f"{DEM}.tif"
    if not dem_path.exists():
        print(f"  DEM not found at {dem_path}, skipping.")
        return

    with rasterio.open(dem_path) as src:
        profile = src.profile.copy()
        height = src.height
        width = src.width
        transform = src.transform
        crs = src.crs

    profile.update(
        dtype="float32",
        count=1,
        nodata=-9999
    )

    ones = np.ones((height, width), dtype="float32")

    adj_path = inputs / ADJ
    with rasterio.open(adj_path, "w", **profile) as dst:
        dst.write(ones, 1)
    print(f"  Wrote {adj_path}")

    phi_path = inputs / PHI
    with rasterio.open(phi_path, "w", **profile) as dst:
        dst.write(ones, 1)
    print(f"  Wrote {phi_path}")


def main():
    root = Path(FIRE_ROOT)
    for folder in root.iterdir():
        if folder.is_dir() and folder.name.isdigit():
            print(f"\nFolder {folder.name}:")
            create_adj_phi_for_folder(folder)

if __name__ == "__main__":
    main()
