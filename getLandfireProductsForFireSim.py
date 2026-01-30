# -*- coding: utf-8 -*-
"""
Parallel LANDFIRE downloader (threaded).
Parallelises the slow I/O: submit -> poll -> download.
"""

import time
import zipfile
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import geopandas as gpd
import pandas as pd
import requests
from pyproj import CRS
import pipelineConfig
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

FIREPAIRS_ROOT      = pipelineConfig.FIRE_ROOT
SUMMARY_CSV         = pipelineConfig.FIRE_SUMMARY_CSV
EMAIL               = pipelineConfig.LANDFIRE_EMAIL
EXPAND              = pipelineConfig.EXPAND
TERRAIN_PRODUCTS    = ["ELEV2020", "SLPD2020", "ASP2020"]
BASE_API            = "https://lfps.usgs.gov"
FIRESCAR_NAME       = pipelineConfig.BURN_SHAPE_NAME

# API Download knobs
MAX_WORKERS         = pipelineConfig.MAX_WORKERS          # overall folder concurrency
MAX_DOWNLOADS       = 3        # throttle downloads separately (optional)
POLL_SLEEP_S        = 10
POLL_MAX_TRIES      = 300
_download_sem       = threading.Semaphore(MAX_WORKERS)
_PRINT_LOCK         = threading.Lock()

FBFM40_CODES = {
    2019: "200F40_19",
    2020: "200F40_20",
    2023: "230FBFM40",
    2024: "240FBFM40",
    2025: "250FBFM40",
}
LF_FUEL_VERSIONS = {
    2019: ("200", "_19"),
    2020: ("200", "_20"),
    2023: ("230", ""),
    2024: ("240", ""),
    2025: ("250", ""),
}
DATASET_YEARS = sorted(LF_FUEL_VERSIONS.keys())


def make_logger(prefix: str):
    pfx = f"[{prefix}] "
    def log(msg=""):
        ts = datetime.now().strftime("%H:%M:%S")
        text = "" if msg is None else str(msg)
        lines = text.splitlines() or [""]
        with _PRINT_LOCK:
            for line in lines:
                print(f"{ts} {pfx}{line}", flush=True)
    return log

def build_products_for_fire_year(fire_year, log=print):
    
    if fire_year is None or pd.isna(fire_year):
        return min(DATASET_YEARS)
    y = int(fire_year)
    candidates = [dy for dy in DATASET_YEARS if dy <= y]
    dataset_year = max(candidates) if candidates else min(DATASET_YEARS)
    prefix, suff = LF_FUEL_VERSIONS[dataset_year]

    fbfm40_code = FBFM40_CODES[dataset_year]

    log(f"Using LF dataset year {dataset_year}, (prefix '{prefix}', suffix '{suff}', FBFM40='{fbfm40_code}')"
    )

    fuels_canopy = [
        fbfm40_code,
        f"{prefix}CC{suff}",
        f"{prefix}CH{suff}",
        f"{prefix}CBH{suff}",
        f"{prefix}CBD{suff}",
    ]

    products = TERRAIN_PRODUCTS + fuels_canopy
    log(f"  Products: {products}")
    return products

def best_projection_for_bbox(bounds):
    xmin, ymin, xmax, ymax = bounds
    mid_lon = (xmin + xmax) / 2
    mid_lat = (ymin + ymax) / 2
    utm_zone = int((mid_lon + 180) / 6) + 1
    epsg = 32600 + utm_zone if mid_lat >= 0 else 32700 + utm_zone
    return CRS.from_epsg(epsg)

# ---------------------------------------------------------------------------
# LFPS COMMUNICATION
# ---------------------------------------------------------------------------

# Optional: a Session improves throughput by reusing connections per thread
_thread_local = threading.local()

def _get_session():
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        _thread_local.session = s
    return _thread_local.session

def submit_job(products, bbox, projection):
    params = {
        "Email": EMAIL,
        "Layer_List": ";".join(products),
        "Area_of_Interest": " ".join(str(x) for x in bbox),
        "Output_Projection": str(projection),
    }
    s = _get_session()
    r = s.get(f"{BASE_API}/api/job/submit", params=params, timeout=60)
    r.raise_for_status()
    js = r.json()
    return js["jobId"]

def poll_job(job_id, log=print):
    for _ in range(300):
        r = requests.get(f"{BASE_API}/api/job/status", params={"JobId": job_id})
        js = r.json()
        status = js.get("status")
        log(status)

        if status == "Succeeded":
            return js
        if status == "Failed":
            raise RuntimeError(js)

        time.sleep(10)

    raise TimeoutError("LFPS job timeout")


def extract_url(status_json):
    url = status_json.get("outputFile")
    if not url:
        raise RuntimeError("No download URL in LFPS response")
    return url

def download_zip(url, out_path):
    s = _get_session()
    with s.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)

# ---------------------------------------------------------------------------
# ZIP HANDLING
# ---------------------------------------------------------------------------

def unpack_and_clean(folder, zip_path):
    folder = Path(folder)
    temp = folder / "_tmp"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir()

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(temp)

    tifs = list(temp.rglob("*.tif"))
    tfws = list(temp.rglob("*.tfw"))

    if tifs:
        (folder / "LANDFIRE.tif").unlink(missing_ok=True)
        tifs[0].rename(folder / "LANDFIRE.tif")

    if tfws:
        (folder / "LANDFIRE.tfw").unlink(missing_ok=True)
        tfws[0].rename(folder / "LANDFIRE.tfw")

    zip_path.unlink(missing_ok=True)
    shutil.rmtree(temp)

# ---------------------------------------------------------------------------
# WORKER
# ---------------------------------------------------------------------------

def process_folder(folder: Path, summary: pd.DataFrame):
    prefix = f"[{folder.name}]"

    def log(msg):
        # keeps multi-thread prints from interleaving mid-line
        with _PRINT_LOCK:
            print(f"{prefix}{msg}")

    firescar = folder / FIRESCAR_NAME
    if not firescar.exists():
        return (folder.name, True, "skipped (no firescar file)") #Name, Success, Message
    lf_tif = folder / "LANDFIRE.tif"
    if lf_tif.exists():
        log("=== skipped - already exists ===")
        return (folder.name, True, "skipped (LANDFIRE.tif exists)")

    log(f"\n=== Processing folder {folder.name} ===")

    fire_year = summary.loc[folder.name, "fire_year"]
    products = build_products_for_fire_year(fire_year, log=log)

    g = gpd.read_file(firescar)
    if g.crs is None or g.crs.to_epsg() != 4326:
        g = g.to_crs(4326)

    bounds = g.total_bounds
    w = bounds[2] - bounds[0]
    h = bounds[3] - bounds[1]
    expanded = (
        bounds[0] - EXPAND * w / 2,
        bounds[1] - EXPAND * h / 2,
        bounds[2] + EXPAND * w / 2,
        bounds[3] + EXPAND * h / 2,
    )

    crs = best_projection_for_bbox(expanded)
    epsg = crs.to_epsg()
    log(f"  CRS: EPSG:{epsg}")

    log("  Sending job...")
    job_id = submit_job(products, expanded, epsg)

    log("  Waiting...")
    status_json = poll_job(job_id, log=log)

    url = extract_url(status_json)
    zip_path = folder / "LANDFIRE.zip"

    log("  Downloading...")
    with _download_sem:
        download_zip(url, zip_path)

    log("  Unpacking...")
    unpack_and_clean(folder, zip_path)

    log("  Done.")
    return (folder.name, True, "done")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    root = Path(FIREPAIRS_ROOT)
    summary_df = pd.read_csv(SUMMARY_CSV)
    summary_df["folder"] = summary_df["folder"].astype(str).str.zfill(5)
    summary_df["perim_ignition"] = pd.to_datetime(summary_df["perim_ignition"], errors="coerce", utc=True)
    summary_df["fire_year"] = summary_df["perim_ignition"].dt.year
    summary = summary_df.set_index("folder")

    folders = sorted([p for p in root.iterdir() if p.is_dir()])

    # Optional: queue only folders that need work
    todo = []
    for folder in folders:
        firescar = folder / FIRESCAR_NAME
        if not firescar.exists():
            continue
        lf_tif = folder / "LANDFIRE.tif"
        if lf_tif.exists():
            continue
        todo.append(folder)

    print(f"Parallel processing {len(todo)} folders with MAX_WORKERS={MAX_WORKERS}...")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(process_folder, f, summary) for f in todo]
        for fut in as_completed(futures):
            results.append(fut.result())

    # Summary
    ok = sum(1 for _, success, _ in results if success)
    fail = len(results) - ok
    print(f"\nFinished: {ok} ok, {fail} failed/skipped.")
    for name, success, msg in sorted(results):
        if not success:
            print(f"  FAIL {name}: {msg}")

if __name__ == "__main__":
    main()
