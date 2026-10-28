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

The fetched (uncropped) messages of each hour are kept in HRRR_CACHE_DIR, so
fires burning at the same time share downloads.

As a pipeline step (before WindNinja, WINDNINJA_MODE = "hrrrLocal") it
downloads the case's hours and marks them complete, so a login node with
internet can fetch everything (``runWildfireAV --start run --download-only``)
and compute nodes without internet run the rest.

WindNinja identifies the model from that name and runs every hour in the zip
(see firelab/windninja src/ninja/gcp_wx_init.cpp and cli.cpp), so the config
only needs ``forecast_filename = <zip>`` and ``time_zone``.
"""

from __future__ import annotations

import os
import threading
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
from common import fmt_duration, for_each_case, progress, read_wxs, require, skipped
from parallel_api import retry_call

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
COMPLETE_MARKER = "DOWNLOAD_COMPLETE"              # written once every hour of a case is present


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
    """GET with exponential backoff on network errors, 429 and 5xx (404 is returned)."""
    def once() -> requests.Response:
        r = session.get(url, headers=headers, timeout=TIMEOUT_S)
        if r.status_code not in (200, 206, 404):
            raise requests.HTTPError(f"HTTP {r.status_code} for {url}", response=r)
        return r
    return retry_call(once, tries=RETRIES, base_sleep_s=1, max_sleep_s=30)


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


def _part(path: Path) -> Path:
    """Temporary name unique to this process and thread (cases share the cache)."""
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")


def _cached_grib(valid: pd.Timestamp, lead: int) -> Path | None:
    """The four CONUS messages valid at *valid* from the run *lead* hours earlier (lead 0 =
    analysis), from the cache or downloaded (None: not in the archive)."""
    run = valid - pd.Timedelta(hours=lead)
    cached = Path(cfg.HRRR_CACHE_DIR) / f"hrrr.{run:%Y%m%d}.t{run:%H}z.wrfsfcf{lead:02d}.4var.grib2"
    if cached.exists():
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    url = f"{HRRR_BASE_URL}/hrrr.{run:%Y%m%d}/conus/hrrr.t{run:%H}z.wrfsfcf{lead:02d}.grib2"
    tmp = _part(cached)
    try:
        with requests.Session() as s:
            idx = _get(s, url + ".idx")
            if idx.status_code == 404:
                return None
            with open(tmp, "wb") as f:
                for start, end in parse_idx_ranges(idx.text):
                    r = _get(s, url, {"Range": f"bytes={start}-{'' if end is None else end}"})
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status_code} for {url} bytes {start}-{end}")
                    f.write(r.content)
        os.replace(tmp, cached)       # atomic: a partial file never looks complete
    finally:
        tmp.unlink(missing_ok=True)
    return cached


def _fetch_hour(ts: pd.Timestamp, out_dir: Path, dem_crs, dem_bounds) -> int | None:
    """Crop the hour to the case.  Returns the forecast lead used (0 = analysis; already
    cropped hours count as 0) or None if the archive has neither the analysis nor a
    forecast up to HRRR_FORECAST_FILL_H hours old for it."""
    final = out_dir / tif_name(ts)
    if final.exists():
        return 0
    for lead in range(cfg.HRRR_FORECAST_FILL_H + 1):
        grib = _cached_grib(ts, lead)
        if grib is not None:
            break
    else:
        return None
    tmp = _part(final)
    try:
        grib_to_cropped_tif(grib, tmp, dem_crs, dem_bounds)
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return lead


def hours_between(start, stop) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start).floor("h"), pd.Timestamp(stop).floor("h"), freq="h")


def download_hrrr(dem_path: Path, start, stop, out_dir: Path) -> dict[pd.Timestamp, int]:
    """
    Fill ``out_dir`` with cropped HRRR GeoTIFFs for every hour in [start, stop] (naive UTC).

    An hour whose analysis is missing from the archive (common in 2018) is filled
    with the forecast valid at that hour from a run up to HRRR_FORECAST_FILL_H
    hours earlier.  Existing hours are kept, so a re-run resumes.  Raises if an
    hour still has no data (WindNinja cannot interpolate across a gap).
    Returns {hour: forecast lead} of the filled hours.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dem_crs, dem_bounds = dem_footprint(Path(dem_path))
    hours = hours_between(start, stop)
    print(f"  HRRR: {len(hours)} analysis hours {hours[0]:%Y-%m-%d %H}Z -> {hours[-1]:%Y-%m-%d %H}Z "
          f"({THREADS} threads, {HRRR_BASE_URL})")

    t0 = time.monotonic()
    missing, filled, done, every = [], {}, 0, max(1, -(-len(hours) // 10))   # ~10 progress lines
    with ThreadPoolExecutor(max_workers=max(1, THREADS)) as ex:
        futs = {ex.submit(_fetch_hour, t, out_dir, dem_crs, dem_bounds): t for t in hours}
        for fut in as_completed(futs):
            lead = fut.result()
            if lead is None:
                missing.append(futs[fut])
            elif lead:
                filled[futs[fut]] = lead
            done += 1
            if done % every == 0 or done == len(hours):
                progress(f"HRRR download {done}/{len(hours)} h ({fmt_duration(time.monotonic() - t0)})")
    if filled:
        leads = sorted(set(filled.values()))
        print(f"  HRRR: {len(filled)} hour(s) missing from the archive filled with forecasts "
              f"({', '.join(f'{lead} h' for lead in leads)}) from earlier runs")
    if missing:
        missing.sort()
        raise RuntimeError(f"HRRR archive has no analysis or forecast up to {cfg.HRRR_FORECAST_FILL_H} h old "
                           f"for {len(missing)} hour(s), first {missing[0]:%Y-%m-%d %H}Z")
    return dict(sorted(filled.items()))


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


# ---------------------------------------------------------------------------
# Pipeline step
# ---------------------------------------------------------------------------

def _download_case(case_dir: Path):
    import runWindninja    # imported here: runWindninja imports this module too
    inputs = case_dir / cfg.INPUTS_SUBDIR_NAME
    hrrr_dir = inputs / cfg.WINDNINJA_SUBDIR / cfg.HRRR_LOCAL_SUBDIR
    if (inputs / cfg.WS_TIF_NAME).exists():
        return skipped(f"{cfg.WS_TIF_NAME} already exists")
    if (hrrr_dir / COMPLETE_MARKER).exists():
        return skipped("HRRR hours already downloaded")
    wxs, landscape = inputs / cfg.WXS_FILE_NAME, case_dir / runWindninja.LANDSCAPE
    require(landscape, hint="run the landfire step first")
    require(wxs, hint="run the weather step first")
    hours = runWindninja.hourly_window(case_dir, read_wxs(wxs)).index
    filled = download_hrrr(landscape, hours[0], hours[-1], hrrr_dir)
    (hrrr_dir / COMPLETE_MARKER).write_text(
        f"{hours[0]} {hours[-1]}\n"
        + "".join(f"{t:%Y-%m-%d %H}Z filled with the {lead} h forecast\n" for t, lead in filled.items()))


def main(case_dir=None):
    return for_each_case(_download_case, case_dir)


if __name__ == "__main__":
    main()
