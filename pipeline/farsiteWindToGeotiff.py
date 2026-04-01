#!/usr/bin/env python3
"""
Derive ws.tif and wd.tif from a FARSITE farsite_WindGrids.tif.

farsite_WindGrids.tif band layout
----------------------------------
  Band 1, 3, 5, ...  → wind speed  (odd bands)
  Band 2, 4, 6, ...  → wind direction (even bands)

The file has no embedded CRS.  Its spatial reference is the same projected
coordinate system as the ELMFIRE input rasters (e.g. LANDFIRE.tif / dem.tif).

The wind-grid resolution is typically coarser than the LANDFIRE grid (set by
WINDNINJA_MESH_RESOLUTION_FACTOR in pipelineConfig).  This script reprojects
each band to exactly match the LANDFIRE.tif grid (same CRS, transform, and
shape).  Any edge cells that fall outside the wind-grid extent are filled by
repeating the nearest valid row/column, matching the behaviour of the WindNinja
pad_to_reference helper.

Outputs (written to <case_dir>/inputs/)
---------------------------------------
  ws.tif  – wind speed     (N bands, one per FARSITE time step)
  wd.tif  – wind direction (N bands, one per FARSITE time step)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling

import pipelineConfig as cfg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INPUTS              = cfg.INPUTS_SUBDIR_NAME
WIND_GRIDS_NAME     = "farsite_WindGrids.tif"
REFERENCE_NAME      = "LANDFIRE.tif"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reproject_stack(
    src_data: np.ndarray,       # (n_bands, h, w)  float32
    src_transform,
    src_crs,
    dst_transform,
    dst_crs,
    dst_height: int,
    dst_width: int,
    nodata: float,
    resampling: Resampling,
) -> np.ndarray:
    """Reproject every band in *src_data* to the destination grid."""
    n = src_data.shape[0]
    dst = np.full((n, dst_height, dst_width), nodata, dtype=np.float32)
    for i in range(n):
        reproject(
            source        = src_data[i],
            destination   = dst[i],
            src_transform = src_transform,
            src_crs       = src_crs,
            dst_transform = dst_transform,
            dst_crs       = dst_crs,
            src_nodata    = nodata,
            dst_nodata    = nodata,
            resampling    = resampling,
        )
    return dst


def _fill_edge_nodata(data: np.ndarray, nodata: float) -> np.ndarray:
    """
    Fill nodata stripes at the north (top) and east (right) edges by repeating
    the nearest valid row / column.

    This mirrors the behaviour of wn_to_geotiff.pad_to_reference for cases
    where the wind-grid extent falls just short of the LANDFIRE domain boundary
    after reprojection.
    """
    d = data.copy()
    # Identify pixels that are nodata across *all* bands simultaneously
    all_nd = np.all(d == nodata, axis=0)  # (h, w)

    # North edge: find nodata rows from the top
    top_nodata = 0
    for r in range(d.shape[1]):
        if all_nd[r, :].all():
            top_nodata += 1
        else:
            break
    if top_nodata > 0:
        d[:, :top_nodata, :] = d[:, top_nodata : top_nodata + 1, :]

    # East edge: find nodata columns from the right
    right_nodata = 0
    for c in range(d.shape[2] - 1, -1, -1):
        if all_nd[:, c].all():
            right_nodata += 1
        else:
            break
    if right_nodata > 0:
        start = d.shape[2] - right_nodata
        d[:, :, start:] = d[:, :, start - 1 : start]

    if top_nodata or right_nodata:
        print(
            f"  Filled edge nodata: {top_nodata} row(s) north, "
            f"{right_nodata} col(s) east"
        )
    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def main(case_dir: Path) -> None:
    """Extract ws.tif / wd.tif from FARSITE wind grids for *case_dir*."""
    case_dir   = Path(case_dir).absolute()
    wind_grids = case_dir / "farsite" / "outputs" / WIND_GRIDS_NAME
    reference  = case_dir / REFERENCE_NAME
    ws_out     = case_dir / INPUTS / cfg.WS_TIF_NAME
    wd_out     = case_dir / INPUTS / cfg.WD_TIF_NAME

    if ws_out.exists() and wd_out.exists():
        print("  Skipped — ws.tif and wd.tif already exist.")
        return

    if not wind_grids.exists():
        raise FileNotFoundError(
            f"Wind grids not found: {wind_grids}\n"
            "Run FARSITE (step 10) before this step."
        )

    # ---- reference grid -------------------------------------------------
    with rasterio.open(reference) as ref:
        ref_crs       = ref.crs
        ref_transform = ref.transform
        ref_height    = ref.height
        ref_width     = ref.width

    # ---- read wind grids (no CRS — same projected system as reference) --
    with rasterio.open(wind_grids) as wg:
        n_bands       = wg.count
        src_transform = wg.transform
        src_nodata    = wg.nodata if wg.nodata is not None else -9999.0
        all_data      = wg.read().astype(np.float32)   # (bands, h, w)
        # Replace any unset nodata with our sentinel so edge-fill works
        if wg.nodata is None:
            all_data[all_data == -9999.0] = src_nodata

    # ---- split interleaved bands ----------------------------------------
    # odd 1-based → index 0, 2, 4, … = speed
    # even 1-based → index 1, 3, 5, … = direction
    spd_idx  = list(range(0, n_bands, 2))
    dir_idx  = list(range(1, n_bands, 2))
    spd_data = all_data[spd_idx]    # (n_steps, h, w)
    dir_data = all_data[dir_idx]

    n_steps = len(spd_idx)
    print(
        f"  Reprojecting {n_steps} wind time steps "
        f"({wg.height}×{wg.width} @ {abs(src_transform.e):.0f} m  →  "
        f"{ref_height}×{ref_width} @ {abs(ref_transform.e):.0f} m)"
    )

    # The wind grids share the same CRS as the reference (just missing the tag)
    src_crs = ref_crs

    ws_data = _fill_edge_nodata(
        _reproject_stack(spd_data, src_transform, src_crs,
                         ref_transform, ref_crs, ref_height, ref_width,
                         src_nodata, Resampling.bilinear),
        src_nodata,
    )
    wd_data = _fill_edge_nodata(
        _reproject_stack(dir_data, src_transform, src_crs,
                         ref_transform, ref_crs, ref_height, ref_width,
                         src_nodata, Resampling.nearest),
        src_nodata,
    )

    # ---- write outputs --------------------------------------------------
    out_profile = {
        "driver":    "GTiff",
        "dtype":     "float32",
        "nodata":    src_nodata,
        "crs":       ref_crs,
        "transform": ref_transform,
        "height":    ref_height,
        "width":     ref_width,
        "count":     n_steps,
        "compress":  "lzw",
        "tiled":     True,
        "bigtiff":   "IF_SAFER",
    }

    with rasterio.open(ws_out, "w", **out_profile) as dst:
        dst.write(ws_data)
    print(f"  ws.tif  ({n_steps} bands)")

    with rasterio.open(wd_out, "w", **out_profile) as dst:
        dst.write(wd_data)
    print(f"  wd.tif  ({n_steps} bands)")

    # ws.tif / wd.tif are now ready — the source wind grids are no longer needed
    wind_grids.unlink()
    print(f"  Deleted {WIND_GRIDS_NAME}")


if __name__ == "__main__":
    from case_metadata import case_dirs
    for d in sorted(case_dirs(Path(cfg.FIRE_ROOT))):
        print(f"\n[{d.name}]")
        try:
            main(d)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
