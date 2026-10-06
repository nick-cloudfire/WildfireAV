#!/usr/bin/env python3
"""
Environment checks run before a batch starts.

Catches, in seconds, the problems that would otherwise make every case fail
hours into a SLURM job: a missing executable or module, an unset LFPS_EMAIL,
a missing barrier dataset, no outbound internet on the compute node, too
little disk, or more worker threads than allocated CPUs.

Only what the selected cases still need is checked (e.g. LFPS is only
required if some case has no LANDFIRE.tif yet).  Before setup (folders=None)
there are no cases yet, so everything is assumed needed and the raw setup
inputs are checked too.

    python preflight.py              # check every case under FIRE_ROOT
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE.parent), str(_HERE)]

import pipelineConfig as cfg
from case_metadata import METADATA_FILENAME, case_dirs

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _which(name: str) -> str | None:
    return shutil.which(str(name))


def _reachable(url: str) -> tuple[bool, str]:
    import requests
    try:
        r = requests.head(url, timeout=10, allow_redirects=True)
        return r.status_code < 500, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, type(e).__name__


def _windninja() -> tuple[str, str]:
    if not cfg.WINDNINJA_CONDA_ENV:
        path = _which("WindNinja_cli")
        return (OK, path) if path else (FAIL, "not on PATH (activate its env or set WINDNINJA_CONDA_ENV)")
    conda = os.environ.get("CONDA_EXE", "conda")
    if not _which(conda):
        return FAIL, f"conda ('{conda}') not found to look in env '{cfg.WINDNINJA_CONDA_ENV}'"
    try:
        out = subprocess.run([conda, "run", "-n", cfg.WINDNINJA_CONDA_ENV, "which", "WindNinja_cli"],
                             capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return WARN, "conda run timed out; could not verify"
    if out.returncode == 0 and out.stdout.strip():
        return OK, f"{out.stdout.strip()} (conda env {cfg.WINDNINJA_CONDA_ENV})"
    return FAIL, f"not found in conda env '{cfg.WINDNINJA_CONDA_ENV}' (WINDNINJA_CONDA_ENV)"


def _existing(path: Path) -> Path:
    """path, or its nearest existing parent (FIRE_ROOT may not exist before setup)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def checks(folders: list[Path] | None, workers: int, fresh: bool = False) -> list[tuple[str, str, str]]:
    """fresh=True: outputs will be cleaned first, so every case needs every download.
    folders=None: checked before setup — no cases yet, so every download is needed."""
    inputs = cfg.INPUTS_SUBDIR_NAME
    install = cfg.WINDNINJA_SOURCE == "install"
    res: list[tuple[str, str, str]] = []

    def add(level, name, detail=""):
        res.append((level, name, detail))

    # ---- config ---------------------------------------------------------------
    if cfg.UNKNOWN_LOCAL_KEYS:
        add(WARN, "local config", "pipelineConfig_local.py sets unknown key(s) (stale copy?): "
            + " ".join(cfg.UNKNOWN_LOCAL_KEYS))
    overrides = sorted(cfg.LOCAL_OVERRIDES) + [f"WAV_{k}" for k in cfg.ENV_OVERRIDES]
    add(OK, "config overrides", " ".join(overrides) if overrides else "none (defaults)")

    # ---- cases --------------------------------------------------------------
    root = Path(cfg.FIRE_ROOT)
    add(OK if os.access(_existing(root), os.W_OK) else FAIL, "FIRE_ROOT writable", str(root))
    if folders is None:
        for label, path in (("MTBS perimeters", cfg.MTBS_PERIMS_RAW),
                            ("ignition points", cfg.USFS_POINTS_RAW),
                            ("satellite points", cfg.SATELLITE_GPKG)):
            add(OK if Path(path).exists() else FAIL, f"data {label}", str(path))
        need_lfps = need_wx = ["all"]
        need_wn = ["all"] if install else []
    else:
        no_meta = [f.name for f in folders if not (f / METADATA_FILENAME).exists()]
        add(FAIL if no_meta else OK, "case metadata",
            f"{len(no_meta)} case(s) lack {METADATA_FILENAME}: {' '.join(no_meta[:8])}" if no_meta
            else f"{len(folders)} case(s)")

        need_lfps = [f for f in folders if not (f / "LANDFIRE.tif").exists()]
        gsi = cfg.LIVE_FUEL_MOISTURE_SOURCE == "gsi"
        need_wx = [f for f in folders if fresh or not (f / inputs / cfg.WXS_FILE_NAME).exists()
                   or (gsi and not (f / inputs / "live_fuel_moisture.json").exists())]
        need_wn = [f for f in folders if fresh or not (f / inputs / cfg.WS_TIF_NAME).exists()] if install else []

    # ---- credentials ----------------------------------------------------------
    if need_lfps:
        add(OK if cfg.LANDFIRE_EMAIL else FAIL, "LFPS_EMAIL",
            cfg.LANDFIRE_EMAIL or f"unset, but {len(need_lfps)} case(s) need a LANDFIRE download "
                                  "(export LFPS_EMAIL=you@example.com)")

    # ---- executables ------------------------------------------------------------
    tools = ["gdal_translate", "ogr2ogr", "gdalsrsinfo", cfg.ELMFIRE_EXE, "wine64"]
    if need_wn:
        tools.append("gdalbuildvrt")
    for t in tools:
        path = _which(t)
        add(OK if path else FAIL, f"exe {Path(str(t)).name}", path or "not found on PATH")
    farsite = Path(cfg.FARSITE_FB_DIR) / cfg.FARSITE_EXE_NAME
    add(OK if farsite.exists() else FAIL, "FARSITE binary", str(farsite))
    nelson = Path(cfg.NELSON_EXE)
    add(OK if nelson.exists() else FAIL, "Nelson model", str(nelson))
    if need_wn:
        level, detail = _windninja()
        add(level, "WindNinja_cli", detail)

    # ---- datasets -----------------------------------------------------------------
    for label, path, required in (("roads", cfg.ROADS_GPKG, True),
                                  ("waterways", cfg.WATER_GPKG, True),
                                  ("backup rivers", cfg.BACKUP_WATER_GPKG, False)):
        exists = Path(path).exists()
        add(OK if exists else (FAIL if required else WARN), f"data {label}", str(path))

    # ---- network (compute nodes often have none) --------------------------------------
    urls = []
    if need_lfps:
        urls.append(("LFPS API", cfg.LFPS_BASE_API, need_lfps))
    if need_wx:
        urls.append(("OpenMeteo", cfg.OPENMETEO_URL, need_wx))
    if need_wn and cfg.WINDNINJA_MODE == "hrrrLocal":
        urls.append(("HRRR archive", cfg.HRRR_BASE_URL + "/", need_wn))
    with ThreadPoolExecutor(max_workers=4) as ex:
        for (label, url, need), (ok, why) in zip(urls, ex.map(lambda u: _reachable(u[1]), urls)):
            n = "all cases" if folders is None else f"{len(need)} case(s)"
            add(OK if ok else FAIL, f"net {label}", f"{url} ({why}; needed by {n})")

    # ---- resources --------------------------------------------------------------------
    cpus = cfg.AVAILABLE_CPUS
    threads = workers * (cfg.WINDNINJA_NUM_THREADS if need_wn else 1)
    slurm = os.environ.get("SLURM_CPUS_PER_TASK")
    add(WARN if threads > cpus else OK, "CPUs",
        f"{cpus} available{f' (SLURM_CPUS_PER_TASK={slurm})' if slurm else ''}, "
        f"{workers} worker(s) x {cfg.WINDNINJA_NUM_THREADS if need_wn else 1} thread(s)")
    free_gb = shutil.disk_usage(_existing(root)).free / 1e9
    add(WARN if free_gb < cfg.PREFLIGHT_MIN_FREE_GB else OK, "disk free", f"{free_gb:,.0f} GB at {root}")
    return res


def run(folders: list[Path] | None, workers: int, fresh: bool = False) -> bool:
    """Print the checks; return False if any check failed."""
    res = checks(folders, workers, fresh)
    print("PREFLIGHT")
    for level, name, detail in res:
        print(f"  [{level:>4}] {name:<22} {detail}")
    fails = [r for r in res if r[0] == FAIL]
    if fails:
        print(f"\n{len(fails)} preflight check(s) failed — fix them or pass --no-preflight to run anyway.\n")
        return False
    print()
    return True


if __name__ == "__main__":
    sys.exit(0 if run(case_dirs(Path(cfg.FIRE_ROOT)), cfg.MAX_PARALLEL_CASES) else 1)
