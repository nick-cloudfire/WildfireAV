#!/usr/bin/env python3
import re
import tempfile
from pathlib import Path
import shutil

import numpy as np
import rasterio
from rasterio.transform import Affine

from parallel_api import run_subprocess

DT_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})_(\d{4})")  # MM-DD-YYYY_HHMM


def time_key(name: str) -> str | None:
    m = DT_RE.search(name)
    if not m:
        return None
    mm, dd, yyyy, hhmm = m.groups()
    return f"{int(yyyy):04d}{int(mm):02d}{int(dd):02d}{hhmm}"  # YYYYMMDDHHMM


def ordered_files(in_dir: Path, suffix: str) -> list[Path]:
    # Non-recursive glob: staged .asc files live directly in in_dir.
    # Using rglob would also find the originals inside chunk_NNN/ subdirs,
    # causing every timestep to appear twice in the stack.
    files = []
    for p in in_dir.glob(f"*_{suffix}.asc"):
        if not p.is_file():
            continue
        k = time_key(p.name)
        if k is None:
            continue
        files.append((k, p))
    files.sort(key=lambda x: x[0])
    return [p for _, p in files]


def run(cmd: list[str]) -> None:
    run_subprocess(cmd)


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


def make_stack(in_dir: Path, suffix: str, out_tif: Path) -> None:
    files = ordered_files(in_dir, suffix)
    if not files:
        raise SystemExit(f"No *_{suffix}.asc files with a MM-DD-YYYY_HHMM token found in {in_dir}")

    xmin, ymin, xmax, ymax = _asc_extent(files[0])

    with tempfile.NamedTemporaryFile(prefix=f"{suffix}_", suffix=".vrt", delete=False) as tf:
        vrt = Path(tf.name)

    try:
        run([
            "gdalbuildvrt", "-separate",
            "-resolution", "highest",
            "-te", str(xmin), str(ymin), str(xmax), str(ymax),
            str(vrt),
        ] + [str(p) for p in files])
        run([
            "gdal_translate", str(vrt), str(out_tif),
            "-of", "GTiff",
            "-co", "COMPRESS=lzw",
            "-co", "TILED=YES",
            "-co", "BIGTIFF=IF_SAFER",
        ])
    finally:
        vrt.unlink(missing_ok=True)

def pad_to_reference(stack_tif: Path, reference_tif: Path) -> None:
    """
    Extend stack_tif to match reference_tif's (height, width) by repeating
    edge pixels, then rewrite the file in place.

    WindNinja coarsens its computation grid to a multiple of
    WINDNINJA_MESH_RESOLUTION_FACTOR, so its ASCII output is typically 1-N
    pixels shorter/narrower than the input landscape.  The lower-left corner
    is always aligned (same origin), so any missing pixels are at the:
      - top  (north) – prepend copies of the existing top row
      - right (east) – append copies of the existing right column
    """
    with rasterio.open(reference_tif) as ref:
        target_h, target_w = ref.height, ref.width

    with rasterio.open(stack_tif) as src:
        if src.height == target_h and src.width == target_w:
            return  # already the right size

        missing_rows = target_h - src.height
        missing_cols = target_w - src.width

        if missing_rows < 0 or missing_cols < 0:
            raise ValueError(
                f"{stack_tif.name}: wind stack ({src.height}×{src.width}) is "
                f"larger than reference ({target_h}×{target_w}); cannot pad."
            )

        data      = src.read()          # (bands, height, width)
        profile   = src.profile.copy()
        transform = src.transform
        cellsize  = abs(transform.e)

    # Pad right edge (east): repeat last column
    if missing_cols > 0:
        data = np.concatenate(
            [data, np.repeat(data[:, :, -1:], missing_cols, axis=2)],
            axis=2,
        )

    # Pad top edge (north): repeat first row and shift the transform origin up
    if missing_rows > 0:
        data = np.concatenate(
            [np.repeat(data[:, 0:1, :], missing_rows, axis=1), data],
            axis=1,
        )
        transform = Affine(
            transform.a, transform.b, transform.c,
            transform.d, transform.e,
            transform.f + missing_rows * cellsize,   # shift ymax north
        )

    profile.update(height=target_h, width=target_w, transform=transform)
    with rasterio.open(stack_tif, "w", **profile) as dst:
        dst.write(data)

    print(
        f"  Padded {stack_tif.name}: "
        f"+{missing_rows} row(s) north, +{missing_cols} col(s) east"
    )


def clean_windninja_outputs(in_dir: Path):
    count = 0
    wxfolder = [p for p in in_dir.rglob("WXSTATIONS*") if p.is_dir()]
    for folder in wxfolder:
        shutil.rmtree(folder)
    for ext in ("*.asc", "*.prj"):
        for p in in_dir.rglob(ext):
            if p.is_file():
                p.unlink()
                count += 1
    print(f"Deleted {count} WindNinja output files (*.asc, *.prj)")

def main(in_dir, out_dir, reference_tif=None, clean=False):
    in_dir  = Path(in_dir)
    out_dir = Path(out_dir)
    ws_tif  = out_dir / "ws.tif"
    wd_tif  = out_dir / "wd.tif"

    make_stack(in_dir, "vel", ws_tif)
    make_stack(in_dir, "ang", wd_tif)

    if reference_tif is not None:
        ref = Path(reference_tif)
        pad_to_reference(ws_tif, ref)
        pad_to_reference(wd_tif, ref)

    if clean:
        clean_windninja_outputs(in_dir)
    print("Done: ws.tif, wd.tif")


if __name__ == "__main__":
    main()