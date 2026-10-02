#!/usr/bin/env python3
"""
Download HRRR analysis hours for one case and package them the way WindNinja's
PASTCAST-GCP-HRRR-CONUS-3-KM reader expects.

WindNinja's built-in pastcast download (``wx_model_type = PASTCAST-GCP-HRRR-...``)
needs GCS credentials and fetches serially inside every WindNinja_cli call,
which does not scale.  Instead we:

1. read each hour's ``.idx`` sidecar on the public HRRR bucket and fetch only
   the four messages WindNinja uses (2 m TMP, 10 m UGRD/VGRD, TCDC) with HTTP
   range requests;
2. crop them (native Lambert grid, buffered DEM footprint) to a GeoTIFF named
   ``hrrr.YYYYMMDDtHHz.wrfsfcf00.tif`` that keeps the GRIB band metadata
   (GRIB_ELEMENT, GRIB_SHORT_NAME, GRIB_VALID_TIME) — exactly what WindNinja's
   own GCP downloader writes;
3. zip the hours of each WindNinja run into ``PASTCAST-GCP-HRRR-CONUS-3-KM_*.zip``.

WindNinja identifies the model from that name and runs every hour in the zip
(see firelab/windninja src/ninja/gcp_wx_init.cpp and cli.cpp), so the config
only needs ``forecast_filename = <zip>`` and ``time_zone``.
"""

from __future__ import annotations

import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import rasterio
import requests
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

import pipelineConfig as cfg
from common import fmt_duration, progress

HRRR_BASE_URL   = cfg.HRRR_BASE_URL
TARGET_BANDS    = getattr(cfg, "HRRR_TARGET_BANDS", [
    ("TMP", "2 m above ground"), ("UGRD", "10 m above ground"),
    ("VGRD", "10 m above ground"), ("TCDC", "entire atmosphere"),
])
BUFFER_FRACTION = cfg.HRRR_BUFFER_FRACTION
MIN_BUFFER_M    = cfg.HRRR_MIN_BUFFER_M
THREADS         = cfg.HRRR_DOWNLOAD_THREADS
RETRIES         = cfg.HRRR_DOWNLOAD_RETRIES
TIMEOUT_S       = cfg.HRRR_DOWNLOAD_TIMEOUT_S
ZIP_PREFIX      = "PASTCAST-GCP-HRRR-CONUS-3-KM"   # WindNinja identifies the model by this substring


# ---------------------------------------------------------------------------
# .idx handling
# ---------------------------------------------------------------------------

def parse_idx_ranges(idx_text: str, targets=TARGET_BANDS) -> list[tuple[int, int | None]]:
    """
    Merged (start, end) byte ranges of the wanted (variable, level) messages.

    ``end`` is None for the file's last message (read to EOF).  Raises if any
    wanted message is missing.
    """
    entries = []
    for line in idx_text.strip().splitlines():
        p = line.split(":")
        if len(p) >= 5:
            entries.append((int(p[1]), p[3], p[4]))
    wanted = set(map(tuple, targets))
    ranges: list[tuple[int, int | None]] = []
    found = set()
    for i, (start, var, level) in enumerate(entries):
        if (var, level) not in wanted:
            continue
        found.add((var, level))
        end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
        if ranges and ranges[-1][1] is not None and ranges[-1][1] + 1 == start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    if found != wanted:
        raise ValueError(f"HRRR .idx is missing {sorted(wanted - found)}")
    return ranges


def _get(session: requests.Session, url: str, headers=None) -> requests.Response:
    """GET with exponential backoff on network errors, 429 and 5xx."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=headers, timeout=TIMEOUT_S)
            if r.status_code in (200, 206, 404):
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url} after {RETRIES} attempts: {last}")


# ---------------------------------------------------------------------------
# Crop + GeoTIFF
# ---------------------------------------------------------------------------

def dem_footprint(dem_path: Path):
    """(crs, bounds) of the DEM expanded by BUFFER_FRACTION per side (>= MIN_BUFFER_M)."""
    with rasterio.open(dem_path) as dem:
        l, b, r, t = dem.bounds
        crs = dem.crs
    min_buf = MIN_BUFFER_M if crs.is_projected else MIN_BUFFER_M / 111_000.0
    bx, by = max((r - l) * BUFFER_FRACTION, min_buf), max((t - b) * BUFFER_FRACTION, min_buf)
    return crs, (l - bx, b - by, r + bx, t + by)


def grib_to_cropped_tif(grib: Path, out_tif: Path, dem_crs, dem_bounds) -> tuple[int, int]:
    """Crop a GRIB2 file to the DEM footprint as a GeoTIFF with its GRIB band tags."""
    with rasterio.open(grib) as src:
        xmin, ymin, xmax, ymax = transform_bounds(dem_crs, src.crs, *dem_bounds, densify_pts=21)
        w = from_bounds(xmin, ymin, xmax, ymax, transform=src.transform)
        w = Window(int(w.col_off) - 1, int(w.row_off) - 1, int(w.width) + 3, int(w.height) + 3) \
            .intersection(Window(0, 0, src.width, src.height))
        profile = {**src.profile, "driver": "GTiff", "width": int(w.width), "height": int(w.height),
                   "transform": src.window_transform(w), "compress": "deflate"}
        with rasterio.open(out_tif, "w", **profile) as dst:
            dst.write(src.read(window=w))
            dst.update_tags(**src.tags())
            for i in range(1, src.count + 1):
                dst.update_tags(i, **src.tags(i))
                if src.descriptions[i - 1]:
                    dst.set_band_description(i, src.descriptions[i - 1])
    return int(w.width), int(w.height)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def tif_name(ts: pd.Timestamp) -> str:
    return f"hrrr.{ts:%Y%m%d}t{ts:%H}z.wrfsfcf00.tif"   # name WindNinja looks up inside the zip


def _fetch_hour(ts: pd.Timestamp, out_dir: Path, dem_crs, dem_bounds) -> bool:
    """Fetch + crop one analysis hour.  False if the archive has no such hour."""
    final = out_dir / tif_name(ts)
    if final.exists():
        return True
    url = f"{HRRR_BASE_URL}/hrrr.{ts:%Y%m%d}/conus/hrrr.t{ts:%H}z.wrfsfcf00.grib2"
    raw, tmp = out_dir / f"{final.stem}.grib2.part", out_dir / f"{final.name}.part"
    try:
        with requests.Session() as s:
            idx = _get(s, url + ".idx")
            if idx.status_code == 404:
                return False
            with open(raw, "wb") as f:
                for start, end in parse_idx_ranges(idx.text):
                    r = _get(s, url, {"Range": f"bytes={start}-{'' if end is None else end}"})
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status_code} for {url} bytes {start}-{end}")
                    f.write(r.content)
        grib_to_cropped_tif(raw, tmp, dem_crs, dem_bounds)
        tmp.replace(final)            # atomic: a partial file never looks complete
    finally:
        raw.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    return True


def hours_between(start, stop) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start).floor("h"), pd.Timestamp(stop).floor("h"), freq="h")


def download_hrrr(dem_path: Path, start, stop, out_dir: Path) -> Path:
    """
    Fill ``out_dir`` with cropped HRRR GeoTIFFs for every hour in [start, stop] (naive UTC).

    Existing hours are kept, so a re-run resumes.  Raises if any hour is
    missing from the archive (WindNinja cannot interpolate across a gap).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dem_crs, dem_bounds = dem_footprint(Path(dem_path))
    hours = hours_between(start, stop)
    print(f"  HRRR: {len(hours)} analysis hours {hours[0]:%Y-%m-%d %H}Z -> {hours[-1]:%Y-%m-%d %H}Z "
          f"({THREADS} threads, {HRRR_BASE_URL})")

    t0 = time.monotonic()
    missing, done, every = [], 0, max(1, -(-len(hours) // 10))   # ~10 progress lines
    with ThreadPoolExecutor(max_workers=max(1, THREADS)) as ex:
        futs = {ex.submit(_fetch_hour, t, out_dir, dem_crs, dem_bounds): t for t in hours}
        for fut in as_completed(futs):
            if not fut.result():
                missing.append(futs[fut])
            done += 1
            if done % every == 0 or done == len(hours):
                progress(f"HRRR download {done}/{len(hours)} h ({fmt_duration(time.monotonic() - t0)})")
    if missing:
        missing.sort()
        raise RuntimeError(f"HRRR archive has no analysis for {len(missing)} hour(s), "
                           f"first {missing[0]:%Y-%m-%d %H}Z")
    return out_dir


def pack_zip(tif_dir: Path, hours, zip_path: Path) -> Path:
    """Zip the GeoTIFFs for ``hours`` into a WindNinja pastcast archive."""
    if ZIP_PREFIX not in zip_path.name:
        raise ValueError(f"zip name must contain {ZIP_PREFIX!r} for WindNinja to recognise it")
    tmp = zip_path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # GeoTIFFs are already compressed
        for ts in hours:
            z.write(tif_dir / tif_name(ts), arcname=tif_name(ts))
    tmp.replace(zip_path)
    return zip_path
