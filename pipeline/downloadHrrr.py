#!/usr/bin/env python3
"""
Download HRRR analysis hours for one case and clip them to the case DEM.

WindNinja's built-in PASTCAST-GCP-HRRR downloader does not scale (it fetches
whole GRIB2 files, serially, inside every WindNinja_cli invocation).  Instead
we pull only the four bands WindNinja needs (2 m TMP, 10 m UGRD/VGRD, TCDC)
straight from the public HRRR archive on Google Cloud Storage using HTTP range
requests driven by each file's ``.idx`` sidecar, then crop to a buffered DEM
footprint.  The result is a pastcast-style directory tree::

    <out_dir>/YYYYMMDDTHH00/hrrr.tHHz.wrfsfcf00.grib2

which is handed to WindNinja via ``forecast_filename``.

Clipping is done in the native HRRR (Lambert Conformal) grid with
``gdal_translate -srcwin`` rather than by reprojecting to the DEM CRS: the GRIB
driver can only *write* a handful of projections (not Albers) and a native
crop keeps the GRIB band metadata WindNinja uses to identify the fields.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import rasterio
import requests
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

import pipelineConfig as cfg

HRRR_BASE_URL = getattr(
    cfg, "HRRR_BASE_URL", "https://storage.googleapis.com/high-resolution-rapid-refresh"
)
TARGET_BANDS = getattr(cfg, "HRRR_TARGET_BANDS", [
    {"var": "TMP",  "level": "2 m above ground"},
    {"var": "UGRD", "level": "10 m above ground"},
    {"var": "VGRD", "level": "10 m above ground"},
    {"var": "TCDC", "level": "entire atmosphere"},
])
BUFFER_FRACTION = getattr(cfg, "HRRR_BUFFER_FRACTION", 0.20)
MIN_BUFFER_M    = getattr(cfg, "HRRR_MIN_BUFFER_M", 6000.0)   # >= 2 HRRR cells each side
THREADS         = getattr(cfg, "HRRR_DOWNLOAD_THREADS", 4)
RETRIES         = getattr(cfg, "HRRR_DOWNLOAD_RETRIES", 4)
TIMEOUT_S       = getattr(cfg, "HRRR_DOWNLOAD_TIMEOUT_S", 120)
HOUR_DIR_FMT    = "%Y%m%dT%H00"


# ---------------------------------------------------------------------------
# .idx handling
# ---------------------------------------------------------------------------

def parse_idx_ranges(idx_text: str, target_specs=TARGET_BANDS) -> list[tuple[int, int | None]]:
    """
    Return merged (start, end) byte ranges for the requested variable/level pairs.

    ``end`` is None for the last message in the file (read to EOF).  Ranges
    that are adjacent in the file are merged to cut the request count.
    """
    entries = []
    for line in idx_text.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 5:
            entries.append((int(parts[1]), parts[3], parts[4]))

    wanted = {(s["var"], s["level"]) for s in target_specs}
    ranges: list[tuple[int, int | None]] = []
    for i, (start, var, level) in enumerate(entries):
        if (var, level) not in wanted:
            continue
        end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
        if ranges and ranges[-1][1] is not None and ranges[-1][1] + 1 == start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))

    found = sum(1 for _, v, l in entries if (v, l) in wanted)
    if found != len(wanted):
        raise ValueError(
            f"Expected {len(wanted)} HRRR messages, found {found} in idx "
            f"(wanted {sorted(wanted)})"
        )
    return ranges


def _get(session: requests.Session, url: str, headers=None) -> requests.Response:
    """GET with exponential backoff on network errors and 5xx/429."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=headers, timeout=TIMEOUT_S)
            if r.status_code in (200, 206, 404):
                return r
            last = RuntimeError(f"HTTP {r.status_code} for {url}")
        except requests.RequestException as e:
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url} after {RETRIES} attempts: {last}")


# ---------------------------------------------------------------------------
# DEM footprint -> HRRR pixel window
# ---------------------------------------------------------------------------

def dem_bounds_buffered(dem_path: Path) -> tuple[rasterio.crs.CRS, tuple[float, float, float, float]]:
    """DEM bounds expanded by BUFFER_FRACTION per side (at least MIN_BUFFER_M)."""
    with rasterio.open(dem_path) as dem:
        l, b, r, t = dem.bounds
        crs = dem.crs
    # Buffer in metres when the DEM is projected; fall back to degrees-equivalent.
    min_buf = MIN_BUFFER_M if crs.is_projected else MIN_BUFFER_M / 111_000.0
    bx = max((r - l) * BUFFER_FRACTION, min_buf)
    by = max((t - b) * BUFFER_FRACTION, min_buf)
    return crs, (l - bx, b - by, r + bx, t + by)


def _src_window(grib_path: Path, dem_crs, dem_bounds) -> Window:
    """Pixel window in the GRIB grid covering the buffered DEM footprint."""
    with rasterio.open(grib_path) as src:
        # densify so the Albers->LCC edge curvature is captured
        xmin, ymin, xmax, ymax = transform_bounds(dem_crs, src.crs, *dem_bounds, densify_pts=21)
        win = from_bounds(xmin, ymin, xmax, ymax, transform=src.transform)
        full = Window(0, 0, src.width, src.height)
    win = Window(
        int(win.col_off) - 1, int(win.row_off) - 1,
        int(win.width) + 3, int(win.height) + 3,
    ).intersection(full)
    return win


def _clip(raw: Path, out: Path, dem_crs, dem_bounds) -> None:
    gdal_translate = shutil.which("gdal_translate")
    if gdal_translate is None:
        # Unclipped still works with WindNinja, just larger on disk.
        print("    WARNING: gdal_translate not found; keeping unclipped HRRR file")
        shutil.move(raw, out)
        return
    w = _src_window(raw, dem_crs, dem_bounds)
    cmd = [
        gdal_translate, "-q", "-of", "GRIB",
        "-srcwin", str(int(w.col_off)), str(int(w.row_off)),
        str(int(w.width)), str(int(w.height)),
        str(raw), str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    raw.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def hrrr_filename(ts: dt.datetime) -> str:
    return f"hrrr.t{ts:%H}z.wrfsfcf00.grib2"


def _fetch_hour(ts: dt.datetime, out_dir: Path, dem_crs, dem_bounds) -> bool:
    """Fetch + clip one analysis hour.  Returns False if HRRR has no such file."""
    name = hrrr_filename(ts)
    hour_dir = out_dir / f"{ts:{HOUR_DIR_FMT}}"
    final = hour_dir / name
    if final.exists():
        return True
    hour_dir.mkdir(parents=True, exist_ok=True)

    url = f"{HRRR_BASE_URL}/hrrr.{ts:%Y%m%d}/conus/{name}"
    raw = hour_dir / f"raw_{name}"
    tmp = hour_dir / f"{name}.part"
    with requests.Session() as s:
        idx = _get(s, url + ".idx")
        if idx.status_code == 404:
            return False
        ranges = parse_idx_ranges(idx.text)
        with open(raw, "wb") as f:
            for start, end in ranges:
                r = _get(s, url, {"Range": f"bytes={start}-{end if end is not None else ''}"})
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code} fetching {url} bytes {start}-{end}")
                f.write(r.content)
    try:
        _clip(raw, tmp, dem_crs, dem_bounds)
        tmp.replace(final)       # atomic: a partial file never looks complete
    finally:
        raw.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    return True


def hours_between(start: dt.datetime, stop: dt.datetime) -> list[dt.datetime]:
    """Every whole UTC hour from floor(start) to floor(stop) inclusive."""
    cur = start.replace(minute=0, second=0, microsecond=0)
    last = stop.replace(minute=0, second=0, microsecond=0)
    out = []
    while cur <= last:
        out.append(cur)
        cur += dt.timedelta(hours=1)
    return out


def download_hrrr(dem_path: Path, start: dt.datetime, stop: dt.datetime, out_dir: Path) -> Path:
    """
    Populate ``out_dir`` with clipped HRRR hours covering [start, stop] (naive UTC).

    Hours already present are skipped, so a re-run resumes.  Raises if any hour
    is unavailable, since WindNinja cannot interpolate across a missing file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dem_crs, dem_bounds = dem_bounds_buffered(Path(dem_path))
    hours = hours_between(start, stop)
    print(f"  Downloading {len(hours)} HRRR hours -> {out_dir}")

    with ThreadPoolExecutor(max_workers=max(1, THREADS)) as ex:
        ok = list(ex.map(lambda t: _fetch_hour(t, out_dir, dem_crs, dem_bounds), hours))

    missing = [t for t, good in zip(hours, ok) if not good]
    if missing:
        raise RuntimeError(
            f"HRRR unavailable for {len(missing)} hour(s), first: {missing[0]:%Y-%m-%d %H:%M} UTC"
        )
    return out_dir
