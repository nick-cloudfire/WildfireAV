"""
Step 1 of runPipelineParallel: download LANDFIRE rasters via the LFPS API.

For each case folder:
1. Read the burn perimeter (firescar.gpkg) and expand its bounding box.
2. Determine the appropriate LANDFIRE dataset year from the fire year.
3. Submit a job to the USGS LFPS API.
4. Poll until the job succeeds, then download and unpack the ZIP.

Output per case
---------------
- LANDFIRE.tif   – multi-band raster (bands match LANDFIRE_BAND_FILE_NAMES order)
- LANDFIRE.tfw   – world file (kept for reference)

The script is safe to re-run: it skips folders where LANDFIRE.tif already exists.
"""

from __future__ import annotations

import time
import zipfile
import shutil
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pipelineConfig as cfg
from case_metadata import read_case_metadata
from common import atomic_write, for_each_case, progress, require, skipped
from parallel_api import get_thread_session
from pyproj import CRS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EMAIL           = cfg.LANDFIRE_EMAIL
EXPAND          = cfg.EXPAND
FIRESCAR_NAME   = cfg.BURN_SHAPE_NAME
BASE_API        = cfg.LFPS_BASE_API
TERRAIN_PRODUCTS = list(cfg.LFPS_TERRAIN_PRODUCTS)   # always downloaded
POLL_SLEEP_S        = cfg.LFPS_POLL_SLEEP_S
POLL_HEARTBEAT_S    = cfg.LFPS_POLL_HEARTBEAT_S
MAX_RETRIES     = cfg.LFPS_MAX_ATTEMPTS

# ---------------------------------------------------------------------------
# LANDFIRE dataset version tables
# ---------------------------------------------------------------------------

# LANDFIRE fuel/canopy releases on LFPS (product names "LF<year>_FBFM40", ...).
# A fire uses the latest release not newer than its year.
DATASET_YEARS = [2016, 2022, 2023, 2024]


# ---------------------------------------------------------------------------
# Product list builder
# ---------------------------------------------------------------------------

def build_products_for_fire_year(fire_year) -> list[str]:
    """LFPS Layer_List: terrain + fuel model and canopy layers of the matching release."""
    older = [y for y in DATASET_YEARS if pd.notna(fire_year) and y <= int(fire_year)]
    release = max(older) if older else min(DATASET_YEARS)
    products = TERRAIN_PRODUCTS + [f"LF{release}_{layer}" for layer in ("FBFM40", "CC", "CH", "CBH", "CBD")]
    progress(f"LANDFIRE {release} release: {', '.join(products)}")
    return products


def _best_utm_crs(bounds) -> CRS:
    """Return a UTM CRS appropriate for the centre of the given WGS84 bounds."""
    xmin, ymin, xmax, ymax = bounds
    mid_lon = (xmin + xmax) / 2
    mid_lat = (ymin + ymax) / 2
    utm_zone = int((mid_lon + 180) / 6) + 1
    epsg = 32600 + utm_zone if mid_lat >= 0 else 32700 + utm_zone
    return CRS.from_epsg(epsg)


# ---------------------------------------------------------------------------
# LFPS API helpers
# ---------------------------------------------------------------------------

def _submit_job(products: list[str], bbox: tuple, projection: int) -> str:
    if not EMAIL:
        raise RuntimeError("LFPS_EMAIL is not set (export LFPS_EMAIL=you@example.com, "
                           "registered at https://lfps.usgs.gov)")
    params = {
        "Email":             EMAIL,
        "Layer_List":        ";".join(products),
        "Area_of_Interest":  " ".join(str(x) for x in bbox),
        "Output_Projection": str(projection),
    }
    s = get_thread_session()
    r = s.get(f"{BASE_API}/api/job/submit", params=params, timeout=60)
    r.raise_for_status()
    return r.json()["jobId"]


def _poll_job(job_id: str, log=print) -> dict:
    """Poll until the job finishes.  Network blips and 5xx keep polling the same
    job (resubmitting would lose its queue position); only LFPS_JOB_TIMEOUT_H,
    if set, gives up."""
    import requests as _requests
    t0 = time.monotonic()
    deadline = t0 + cfg.LFPS_JOB_TIMEOUT_H * 3600 if cfg.LFPS_JOB_TIMEOUT_H else None
    last_status = None
    last_queue_pos = None
    last_heartbeat = t0
    while True:
        if deadline and time.monotonic() > deadline:
            raise TimeoutError(f"LFPS job {job_id} not finished after {cfg.LFPS_JOB_TIMEOUT_H} h")
        try:
            r = get_thread_session().get(
                f"{BASE_API}/api/job/status", params={"JobId": job_id}, timeout=30
            )
            r.raise_for_status()
        except (_requests.ConnectionError, _requests.Timeout, _requests.HTTPError) as exc:
            code = getattr(exc.response, "status_code", None)
            if isinstance(exc, _requests.HTTPError) and code not in (500, 502, 503, 504):
                raise
            elapsed = int(time.monotonic() - t0)
            log(f"  [{elapsed:4d}s] transient {code or type(exc).__name__}, retrying poll…")
            time.sleep(POLL_SLEEP_S)
            continue
        js = r.json()
        status = js.get("status")
        queue_pos = js.get("queuePosition")
        elapsed = int(time.monotonic() - t0)
        now = time.monotonic()
        if status != last_status or queue_pos != last_queue_pos:
            if queue_pos is not None and status not in ("Succeeded", "Failed"):
                log(f"  [{elapsed:4d}s] {status} (queue position: {queue_pos})")
            else:
                log(f"  [{elapsed:4d}s] {status}")
            last_status = status
            last_queue_pos = queue_pos
            last_heartbeat = now
        elif POLL_HEARTBEAT_S > 0 and (now - last_heartbeat) >= POLL_HEARTBEAT_S:
            log(f"  [{elapsed:4d}s] still waiting… {status} (queue position: {queue_pos})")
            last_heartbeat = now
        if status == "Succeeded":
            return js
        if status == "Failed":
            raise RuntimeError(f"LFPS job {job_id} failed: {js}")
        time.sleep(POLL_SLEEP_S)


def _download_zip(url: str, out_path: Path) -> None:
    s = get_thread_session()
    with s.get(url, stream=True, timeout=300) as r, atomic_write(out_path) as tmp:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


# ---------------------------------------------------------------------------
# ZIP unpacking
# ---------------------------------------------------------------------------

def _unpack_and_clean(folder: Path, zip_path: Path) -> None:
    tmp = folder / "_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(tmp)

    tifs = list(tmp.rglob("*.tif"))
    tfws = list(tmp.rglob("*.tfw"))

    if tifs:
        (folder / "LANDFIRE.tif").unlink(missing_ok=True)
        tifs[0].rename(folder / "LANDFIRE.tif")
    if tfws:
        (folder / "LANDFIRE.tfw").unlink(missing_ok=True)
        tfws[0].rename(folder / "LANDFIRE.tfw")

    zip_path.unlink(missing_ok=True)
    shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------

def _process(folder: Path):
    lf_tif = folder / "LANDFIRE.tif"
    if lf_tif.exists():
        return skipped("LANDFIRE.tif already exists")
    firescar = folder / FIRESCAR_NAME
    require(firescar, hint="created by setupPipeline")
    fire_year = pd.to_datetime(read_case_metadata(folder).get("perim_ignition"), errors="coerce").year
    products = build_products_for_fire_year(fire_year)

    # Burn perimeter bounding box (WGS84), widened by EXPAND x its size on each axis
    bounds = gpd.read_file(firescar).to_crs(4326).total_bounds
    w, h = bounds[2] - bounds[0], bounds[3] - bounds[1]
    expanded = (bounds[0] - EXPAND * w / 2, bounds[1] - EXPAND * h / 2,
                bounds[2] + EXPAND * w / 2, bounds[3] + EXPAND * h / 2)
    epsg = _best_utm_crs(expanded).to_epsg()
    progress(f"Output CRS: EPSG:{epsg}")

    zip_path = folder / cfg.LANDFIRE_ZIP_NAME
    last_exc: BaseException | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            progress(f"Retry {attempt}/{MAX_RETRIES} after: {last_exc}")
            zip_path.unlink(missing_ok=True)
            shutil.rmtree(folder / "_tmp", ignore_errors=True)
        try:
            job_id = _submit_job(products, expanded, epsg)
            progress(f"LFPS job {job_id} submitted (attempt {attempt}/{MAX_RETRIES}); "
                     f"status: {BASE_API}/api/job/status?JobId={job_id}")
            url = _poll_job(job_id, log=progress).get("outputFile")
            if not url:
                raise RuntimeError("No download URL in LFPS response")
            progress("Downloading …")
            _download_zip(url, zip_path)
            _unpack_and_clean(folder, zip_path)
            return None
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"LANDFIRE download failed after {MAX_RETRIES} attempts: {last_exc}") from last_exc


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
