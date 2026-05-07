# getLandfireProductsForFireSim.py
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
from parallel_api import make_logger, get_thread_session
from pyproj import CRS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FIREPAIRS_ROOT  = cfg.FIRE_ROOT
EMAIL           = cfg.LANDFIRE_EMAIL
EXPAND          = cfg.EXPAND
FIRESCAR_NAME   = cfg.BURN_SHAPE_NAME
BASE_API        = cfg.LFPS_BASE_API
TERRAIN_PRODUCTS = list(cfg.LFPS_TERRAIN_PRODUCTS)   # always downloaded
POLL_SLEEP_S    = cfg.LFPS_POLL_SLEEP_S
POLL_MAX_TRIES  = cfg.LFPS_POLL_MAX_TRIES
MAX_RETRIES     = 3     # total attempts per case before giving up

# ---------------------------------------------------------------------------
# LANDFIRE dataset version tables
# ---------------------------------------------------------------------------

# Maps fire year → (version prefix, version suffix) for CC/CH/CBH/CBD products
LF_FUEL_VERSIONS = {
    2016: ("LF2016_"),
    2022: ("LF2022_"),
    2023: ("LF2023_"),
    2024: ("LF2024_"),
}

DATASET_YEARS = sorted(LF_FUEL_VERSIONS.keys())


# ---------------------------------------------------------------------------
# Product list builder
# ---------------------------------------------------------------------------

def build_products_for_fire_year(fire_year, log=print) -> list[str]:
    """Return the LFPS Layer_List for the best LANDFIRE dataset year."""
    if fire_year is None or pd.isna(fire_year):
        dataset_year = min(DATASET_YEARS)
    else:
        y = int(fire_year)
        candidates = [dy for dy in DATASET_YEARS if dy <= y]
        dataset_year = max(candidates) if candidates else min(DATASET_YEARS)

    prefix = LF_FUEL_VERSIONS[dataset_year]
    log(
        f"LF dataset year {dataset_year} "
        f"(prefix='{prefix}')"
    )

    fuels_canopy = [
        f"{prefix}FBFM40",
        f"{prefix}CC",
        f"{prefix}CH",
        f"{prefix}CBH",
        f"{prefix}CBD",
    ]
    products = TERRAIN_PRODUCTS + fuels_canopy
    log(f"  Products: {products}")
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
    import requests as _requests
    t0 = time.monotonic()
    last_status = None
    last_queue_pos = None
    for _ in range(POLL_MAX_TRIES):
        try:
            r = get_thread_session().get(
                f"{BASE_API}/api/job/status", params={"JobId": job_id}, timeout=30
            )
            r.raise_for_status()
        except _requests.HTTPError as exc:
            # Transient server-side errors (502, 503, 504) — keep polling
            if exc.response is not None and exc.response.status_code in (502, 503, 504):
                elapsed = int(time.monotonic() - t0)
                log(f"  [{elapsed:4d}s] transient {exc.response.status_code}, retrying poll…")
                time.sleep(POLL_SLEEP_S)
                continue
            raise
        js = r.json()
        status = js.get("status")
        queue_pos = js.get("queuePosition")
        elapsed = int(time.monotonic() - t0)
        if status != last_status or queue_pos != last_queue_pos:
            if queue_pos is not None and status not in ("Succeeded", "Failed"):
                log(f"  [{elapsed:4d}s] {status} (queue position: {queue_pos})")
            else:
                log(f"  [{elapsed:4d}s] {status}")
            last_status = status
            last_queue_pos = queue_pos
        if status == "Succeeded":
            return js
        if status == "Failed":
            raise RuntimeError(f"LFPS job {job_id} failed: {js}")
        time.sleep(POLL_SLEEP_S)
    raise TimeoutError(f"LFPS job {job_id} timed out after {POLL_MAX_TRIES} polls")


def _download_zip(url: str, out_path: Path) -> None:
    s = get_thread_session()
    with s.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
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
# Per-folder worker
# ---------------------------------------------------------------------------

def process_folder(folder: Path, summary: pd.DataFrame | None = None, log=None):
    """
    Download LANDFIRE for one case folder.

    Returns (folder_name, success, message).
    """
    if log is None:
        log = make_logger(folder.name)

    firescar = folder / FIRESCAR_NAME
    if not firescar.exists():
        return (folder.name, True, "skipped (no firescar)")
    lf_tif = folder / "LANDFIRE.tif"
    if lf_tif.exists():
        log("skipped – LANDFIRE.tif already exists")
        return (folder.name, True, "skipped (already exists)")

    log(f"Processing {folder.name}")

    # Determine fire year
    if summary is not None:
        fire_year = summary.loc[folder.name, "fire_year"]
    else:
        meta = read_case_metadata(folder)
        fire_year = pd.to_datetime(
            meta.get("perim_ignition"), errors="coerce"
        ).year

    products = build_products_for_fire_year(fire_year, log=log)

    # Build expanded bbox (WGS84)
    g = gpd.read_file(firescar)
    if g.crs is None or g.crs.to_epsg() != 4326:
        g = g.to_crs(4326)
    bounds = g.total_bounds
    w, h   = bounds[2] - bounds[0], bounds[3] - bounds[1]
    expanded = (
        bounds[0] - EXPAND * w / 2,
        bounds[1] - EXPAND * h / 2,
        bounds[2] + EXPAND * w / 2,
        bounds[3] + EXPAND * h / 2,
    )

    epsg = _best_utm_crs(expanded).to_epsg()
    log(f"  Output CRS: EPSG:{epsg}")

    zip_path = folder / "LANDFIRE.zip"
    last_exc: BaseException | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            log(f"  Retry {attempt}/{MAX_RETRIES} after: {last_exc}")
            # clean up any partial files before retrying
            zip_path.unlink(missing_ok=True)
            tmp = folder / "_tmp"
            if tmp.exists():
                shutil.rmtree(tmp)

        try:
            log(f"  Submitting LFPS job … (attempt {attempt}/{MAX_RETRIES})")
            job_id = _submit_job(products, expanded, epsg)
            log(f"  Job ID: {job_id}")
            log(f"  Status URL: {BASE_API}/api/job/status?JobId={job_id}")

            log("  Polling …")
            status_json = _poll_job(job_id, log=log)

            url = status_json.get("outputFile")
            if not url:
                raise RuntimeError("No download URL in LFPS response")

            log("  Downloading …")
            _download_zip(url, zip_path)

            log("  Unpacking …")
            _unpack_and_clean(folder, zip_path)

            log("  Done.")
            return (folder.name, True, "done")

        except Exception as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break

    raise RuntimeError(
        f"LANDFIRE download failed after {MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(case_dir=None) -> None:
    root = Path(FIREPAIRS_ROOT)

    if case_dir is not None:
        process_folder(Path(case_dir))
        return

    # Batch mode: load summary to get fire years without reading each metadata file
    summary_path = cfg.FIRE_SUMMARY_CSV_PATH   # full path under FIRE_ROOT_LOGIN_NODE
    summary_df = pd.read_csv(summary_path)
    summary_df["folder"] = summary_df["folder"].astype(str).str.zfill(5)
    summary_df["perim_ignition"] = pd.to_datetime(
        summary_df["perim_ignition"], errors="coerce", utc=True
    )
    summary_df["fire_year"] = summary_df["perim_ignition"].dt.year
    summary = summary_df.set_index("folder")

    # Collect folders that still need work
    todo = [
        p for p in sorted(root.iterdir())
        if p.is_dir() and p.name.isdigit()
        and (p / FIRESCAR_NAME).exists()
        and not (p / "LANDFIRE.tif").exists()
    ]

    print(f"Processing {len(todo)} folders …")

    results = [process_folder(f, summary) for f in todo]

    ok   = sum(1 for _, s, _ in results if s)
    fail = len(results) - ok
    print(f"\nFinished: {ok} ok, {fail} failed/skipped.")
    for name, success, msg in sorted(results):
        if not success:
            print(f"  FAIL {name}: {msg}")


if __name__ == "__main__":
    main()
