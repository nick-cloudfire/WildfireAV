#!/usr/bin/env python3
"""
Stack WindNinja's hourly ASCII grids into inputs/ws.tif (speed, mph) and
inputs/wd.tif (direction, degrees), one band per hour in time order.

WindNinja names its outputs ``*_MM-DD-YYYY_HHMM_<res>_vel.asc`` / ``..._ang.asc``
(time in WINDNINJA_TIME_ZONE, UTC).  The stacks are padded or cropped to the
LANDFIRE grid so ELMFIRE can read them alongside the fuels.  Hours before
``first_hour`` (the case's ignition hour) are left out, so band 1 is the
ignition hour like every other hourly input; each band's time is recorded
(common.write_band_times).
"""

import re
import tempfile
from pathlib import Path
import shutil

import numpy as np
import rasterio
from rasterio.transform import Affine

import pandas as pd

from common import atomic_write, write_band_times
import pipelineConfig as cfg
from parallel_api import hours, run_subprocess

DT_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})_(\d{4})")  # MM-DD-YYYY_HHMM


def time_key(name: str) -> str | None:
    m = DT_RE.search(name)
    if not m:
        return None
    mm, dd, yyyy, hhmm = m.groups()
    return f"{int(yyyy):04d}{int(mm):02d}{int(dd):02d}{hhmm}"  # YYYYMMDDHHMM


def ordered_files(in_dir: Path, suffix: str, first_hour=None) -> list[tuple[pd.Timestamp, Path]]:
    """(valid time, file) of WindNinja's *_<suffix>.asc grids from first_hour on, in time order."""
    # Non-recursive glob: staged .asc files live directly in in_dir.
    # Using rglob would also find the originals inside chunk_NNN/ subdirs,
    # causing every timestep to appear twice in the stack.
    files = []
    for p in in_dir.glob(f"*_{suffix}.asc"):
        k = time_key(p.name)
        if not p.is_file() or k is None:
            continue
        t = pd.Timestamp(f"{k[:8]} {k[8:]}")
        if first_hour is None or t >= first_hour:
            files.append((t, p))
    return sorted(files)


def _asc_extent(asc_path: Path) -> tuple[float, float, float, float]:
    """
    Read xllcorner/yllcorner/ncols/nrows/cellsize from an ESRI ASCII header
    and return (xmin, ymin, xmax, ymax).

    WindNinja writes slightly different float values across chunks, causing
    gdalbuildvrt to produce outputs that differ by 1 pixel. Pinning all bands
    to the first file's extent via -te prevents this.
    """
    header: dict[str, float] = {}
    with asc_path.open() as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2 and parts[0].lower() in (
                "ncols", "nrows", "xllcorner", "yllcorner", "cellsize"
            ):
                header[parts[0].lower()] = float(parts[1])
            elif header:
                break  # past the header block
    xmin = header["xllcorner"]
    ymin = header["yllcorner"]
    xmax = xmin + header["ncols"] * header["cellsize"]
    ymax = ymin + header["nrows"] * header["cellsize"]
    return xmin, ymin, xmax, ymax


def make_stack(in_dir: Path, suffix: str, out_tif: Path, first_hour=None) -> pd.DatetimeIndex:
    """Stack the grids into out_tif; returns the band times."""
    timed = ordered_files(in_dir, suffix, first_hour)
    if not timed:
        raise RuntimeError(f"WindNinja produced no *_{suffix}.asc files (MM-DD-YYYY_HHMM) "
                           f"from {first_hour or 'the start'} on in {in_dir}")
    times, files = pd.DatetimeIndex([t for t, _ in timed]), [p for _, p in timed]
    if first_hour is not None and times[0] != first_hour:
        raise RuntimeError(f"WindNinja output starts at {times[0]}, after the ignition hour {first_hour}")
    if (times.to_series().diff().dropna() != pd.Timedelta(hours=1)).any():
        raise RuntimeError(f"WindNinja *_{suffix}.asc grids are not consecutive hours")

    xmin, ymin, xmax, ymax = _asc_extent(files[0])

    with tempfile.NamedTemporaryFile(prefix=f"{suffix}_", suffix=".vrt", delete=False) as tf:
        vrt = Path(tf.name)

    try:
        run_subprocess([
            "gdalbuildvrt", "-q", "-separate",
            "-resolution", "highest",
            "-te", str(xmin), str(ymin), str(xmax), str(ymax),
            str(vrt),
        ] + [str(p) for p in files], timeout=hours(cfg.GDAL_TIMEOUT_H))
        run_subprocess([
            "gdal_translate", "-q", str(vrt), str(out_tif),
            "-of", "GTiff",
            "-co", "COMPRESS=lzw",
            "-co", "TILED=YES",
            "-co", "BIGTIFF=IF_SAFER",
        ], timeout=hours(cfg.GDAL_TIMEOUT_H))
    finally:
        vrt.unlink(missing_ok=True)
    print(f"  {out_tif.name.lstrip('.').replace('.partial', '')}: {len(files)} hourly bands, "
          f"{times[0]:%Y-%m-%d %H:%M} -> {times[-1]:%Y-%m-%d %H:%M} UTC")
    return times

def pad_to_reference(stack_tif: Path, reference_tif: Path) -> None:
    """
    Resize stack_tif to exactly match reference_tif's (height, width), then
    rewrite the file in place.

    The lower-left corner is always aligned (same origin), so any size
    difference is at the north (top) and east (right) edges:
      - stack smaller → pad by repeating the edge row/column
      - stack larger  → crop the excess rows/columns
    """
    with rasterio.open(reference_tif) as ref:
        target_h, target_w = ref.height, ref.width

    with rasterio.open(stack_tif) as src:
        if src.height == target_h and src.width == target_w:
            return  # already the right size

        delta_rows = target_h - src.height   # positive → need to pad, negative → need to crop
        delta_cols = target_w - src.width

        data      = src.read()          # (bands, height, width)
        profile   = src.profile.copy()
        transform = src.transform
        cellsize  = abs(transform.e)

    # ---- east edge (columns) --------------------------------------------
    if delta_cols > 0:
        data = np.concatenate(
            [data, np.repeat(data[:, :, -1:], delta_cols, axis=2)],
            axis=2,
        )
    elif delta_cols < 0:
        data = data[:, :, :target_w]

    # ---- north edge (rows) ----------------------------------------------
    if delta_rows > 0:
        data = np.concatenate(
            [np.repeat(data[:, 0:1, :], delta_rows, axis=1), data],
            axis=1,
        )
        transform = Affine(
            transform.a, transform.b, transform.c,
            transform.d, transform.e,
            transform.f + delta_rows * cellsize,   # shift ymax north
        )
    elif delta_rows < 0:
        data = data[:, -target_h:, :]   # keep the bottom target_h rows (south-aligned)
        transform = Affine(
            transform.a, transform.b, transform.c,
            transform.d, transform.e,
            transform.f + (-delta_rows) * transform.e,  # shift ymax south
        )

    action = "Padded" if (delta_rows >= 0 and delta_cols >= 0) else \
             "Cropped" if (delta_rows <= 0 and delta_cols <= 0) else "Resized"

    profile.update(
        height=target_h, width=target_w, transform=transform,
        compress="lzw", tiled=True, bigtiff="IF_SAFER",
    )
    with rasterio.open(stack_tif, "w", **profile) as dst:
        dst.write(data)

    print(
        f"  {action} {stack_tif.name.lstrip('.').replace('.partial', '')}: "
        f"rows {delta_rows:+d} (north), cols {delta_cols:+d} (east)"
    )


def clean_windninja_outputs(in_dir: Path):
    """Delete all per-step subdirectories and staged ASCII outputs.

    WindNinja writes one ``step_NNN/`` / ``chunk_NNN/`` directory per run and stages a
    copy of each output back to *in_dir*.  After ws.tif / wd.tif have been
    built we no longer need any of this — delete everything except the two
    GeoTIFFs that live in the parent ``inputs/`` folder (not inside in_dir).
    """
    in_dir = Path(in_dir)
    n_dirs = n_files = 0

    for child in list(in_dir.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
            n_dirs += 1
        elif child.is_file():
            child.unlink()
            n_files += 1

    print(f"  Cleaned windninja/: removed {n_dirs} subdirectories, {n_files} files")

def main(in_dir, out_dir, reference_tif=None, clean=False, first_hour=None):
    in_dir  = Path(in_dir)
    out_dir = Path(out_dir)
    # wd first: ws.tif is the step's "done" marker, so it must appear last.
    for suffix, name in (("ang", "wd.tif"), ("vel", "ws.tif")):
        with atomic_write(out_dir / name) as tmp:
            times = make_stack(in_dir, suffix, tmp, first_hour)
            if reference_tif is not None:
                pad_to_reference(tmp, Path(reference_tif))
            write_band_times(tmp, times)

    if clean:
        clean_windninja_outputs(in_dir)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Stack WindNinja *_vel/_ang.asc grids into ws.tif / wd.tif")
    ap.add_argument("in_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--reference", help="raster whose shape the stacks are padded/cropped to")
    ap.add_argument("--clean", action="store_true", help="delete the WindNinja workspace afterwards")
    a = ap.parse_args()
    main(a.in_dir, a.out_dir, a.reference, a.clean)