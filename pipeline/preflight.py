#!/usr/bin/env python3
"""
Environment checks run before a batch starts.

Catches, in seconds, the problems that would otherwise make every case fail
hours into a SLURM job: a missing executable, one that can't start (missing shared
libraries, no .NET runtime for Nelson, FARSITE not launching under Wine), an
unreadable PROJ database for WindNinja, an unset LFPS_EMAIL,
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
from downloadHrrr import COMPLETE_MARKER

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


def _missing_libs(exe: str | Path) -> list[str]:
    """Shared libraries ldd can't resolve for exe ([] if fine or ldd unavailable)."""
    try:
        out = subprocess.run(["ldd", str(exe)], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.split()[0] for line in out.splitlines() if "not found" in line]


def _launch(cmd: list[str], timeout: int, **kw) -> tuple[int | None, str]:
    """(returncode, combined output); returncode None on timeout or if it can't start."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return None, f"no exit within {timeout} s"
    except OSError as e:
        return None, str(e)


def _nelson(exe: Path) -> tuple[str, str]:
    """Without arguments Nelson prints its usage — proves the .NET runtime loads."""
    rc, out = _launch([str(exe)], 60, cwd=str(exe.parent))
    if "Usage" in out:
        return OK, str(exe)
    first = next((l for l in out.splitlines() if l.strip()), f"exit {rc}")
    return FAIL, f"{exe} does not start: {first.strip()[:120]} (.NET 8 runtime installed? DOTNET_ROOT?)"


_WINE_ERRORS = ("could not load", "cannot find", "bad exe format", "not a valid win32", "wine client error")


def _farsite(exe: Path) -> tuple[str, str]:
    """Start the FARSITE exe under Wine with no arguments (prints usage and exits)."""
    rc, out = _launch(["wine64", str(exe)], 120, cwd=str(exe.parent),
                      env={**os.environ, "WINEDEBUG": "-all", "PWD": str(exe.parent)})   # see runFarsiteCase
    low = out.lower()
    if any(e in low for e in _WINE_ERRORS):
        first = next(l for l in out.splitlines() if any(e in l.lower() for e in _WINE_ERRORS))
        hint = ""
        real = exe.resolve()
        if "bad exe format" in low and any(p.lower().endswith((".com", ".exe", ".bat")) for p in real.parts[:-1]):
            hint = (f" - its real path {real.parent} has a folder ending in .com/.exe/.bat, which Wine "
                    "mistakes for a program; copy the FARSITE folder somewhere whose real path has none "
                    "(e.g. scratch) and point FARSITE_FB_DIR there")
        return FAIL, f"wine64 can't start {exe}: {first.strip()[:120]}{hint}"
    if rc is None:
        return WARN, f"{exe} started but {out} (first Wine run creating its prefix?)"
    return OK, f"{exe} (starts under wine64)"


def _proj_db(exe: str) -> tuple[str, str]:
    """WindNinja needs a readable proj.db (else no grid-north correction of wind directions).
    PROJ looks in PROJ_DATA (older: PROJ_LIB), else in <install prefix>/share/proj."""
    var = "PROJ_DATA" if os.environ.get("PROJ_DATA") else "PROJ_LIB" if os.environ.get("PROJ_LIB") else None
    dirs = os.environ[var].split(os.pathsep) if var else [str(Path(exe).resolve().parent.parent / "share" / "proj")]
    for d in dirs:
        db = Path(d) / "proj.db"
        if db.is_file() and os.access(db, os.R_OK):
            return OK, str(db)
    return WARN, f"no readable proj.db in {os.pathsep.join(dirs)} ({var or 'WindNinja prefix'})"


def _existing(path: Path) -> Path:
    """path, or its nearest existing parent (FIRE_ROOT may not exist before setup)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def checks(folders: list[Path] | None, workers: int, fresh: bool = False,
           download_only: bool = False) -> list[tuple[str, str, str]]:
    """fresh=True: outputs will be cleaned first, so every case needs every download.
    folders=None: checked before setup — no cases yet, so every download is needed.
    download_only: only what the download steps need (internet, LFPS_EMAIL, disk)."""
    inputs = cfg.INPUTS_SUBDIR_NAME
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
        need_lfps = need_wx = need_wn = need_hrrr = ["all"]
    else:
        no_meta = [f.name for f in folders if not (f / METADATA_FILENAME).exists()]
        add(FAIL if no_meta else OK, "case metadata",
            f"{len(no_meta)} case(s) lack {METADATA_FILENAME}: {' '.join(no_meta[:8])}" if no_meta
            else f"{len(folders)} case(s)")

        need_lfps = [f for f in folders if not (f / "LANDFIRE.tif").exists()]
        gsi = cfg.LIVE_FUEL_MOISTURE_SOURCE == "gsi"
        need_wx = [f for f in folders if fresh or not (f / inputs / cfg.WXS_FILE_NAME).exists()
                   or (gsi and not (f / inputs / "live_fuel_moisture.json").exists())]
        need_wn = [f for f in folders if fresh or not (f / inputs / cfg.WS_TIF_NAME).exists()]
        hrrr_done = Path(inputs, cfg.WINDNINJA_SUBDIR, cfg.HRRR_LOCAL_SUBDIR, COMPLETE_MARKER)
        need_hrrr = [f for f in need_wn if fresh or not (f / hrrr_done).exists()]
    if cfg.WINDNINJA_MODE != "hrrrLocal":
        need_hrrr = []
    if download_only:
        need_wn = []

    # ---- credentials ----------------------------------------------------------
    if need_lfps:
        add(OK if cfg.LANDFIRE_EMAIL else FAIL, "LFPS_EMAIL",
            cfg.LANDFIRE_EMAIL or f"unset, but {len(need_lfps)} case(s) need a LANDFIRE download "
                                  "(export LFPS_EMAIL=you@example.com)")

    # ---- executables ------------------------------------------------------------
    tools = [] if download_only else ["gdal_translate", "ogr2ogr", "gdalsrsinfo", cfg.ELMFIRE_EXE, "wine64"]
    if need_wn:
        tools.append("gdalbuildvrt")
    for t in tools:
        path = _which(t)
        missing = _missing_libs(path) if path and t != "wine64" else []
        add(FAIL if not path or missing else OK, f"exe {Path(str(t)).name}",
            f"{path}: missing libraries {' '.join(missing)}" if missing else path or "not found on PATH")
    if not download_only:
        farsite = Path(cfg.FARSITE_FB_DIR) / cfg.FARSITE_EXE_NAME
        if not farsite.exists():
            add(FAIL, "FARSITE binary", f"{farsite} not found")
        elif _which("wine64"):
            level, detail = _farsite(farsite)
            add(level, "FARSITE binary", detail)
        nelson = Path(cfg.NELSON_EXE)
        level, detail = _nelson(nelson) if nelson.exists() else (FAIL, f"{nelson} not found")
        add(level, "Nelson model", detail)
    if need_wn:
        level, detail = _windninja()
        add(level, "WindNinja_cli", detail)
        if not cfg.WINDNINJA_CONDA_ENV:   # in another env, that env's activation sets PROJ_DATA
            wn = _which("WindNinja_cli")
            if wn:
                missing = _missing_libs(wn)
                if missing:
                    add(FAIL, "WindNinja_cli libs", f"missing {' '.join(missing)}")
                level, detail = _proj_db(wn)
                add(level, "PROJ database", detail)

    if need_hrrr:
        import rasterio
        with rasterio.Env() as env:
            grib = "GRIB" in env.drivers()
        add(OK if grib else FAIL, "GDAL GRIB driver", "available" if grib else
            "missing: HRRR files can't be read (conda install -c conda-forge libgdal-grib)")

    # ---- datasets -----------------------------------------------------------------
    for label, path, required in (("roads", cfg.ROADS_GPKG, True),
                                  ("waterways", cfg.WATER_GPKG, True),
                                  ("backup rivers", cfg.BACKUP_WATER_GPKG, False)):
        if download_only:
            break
        exists = Path(path).exists()
        add(OK if exists else (FAIL if required else WARN), f"data {label}", str(path))

    # ---- network (compute nodes often have none) --------------------------------------
    urls = []
    if need_lfps:
        urls.append(("LFPS API", cfg.LFPS_BASE_API, need_lfps))
    if need_wx:
        urls.append(("OpenMeteo", cfg.OPENMETEO_URL, need_wx))
    if need_hrrr:
        urls.append(("HRRR archive", cfg.HRRR_BASE_URL + "/", need_hrrr))
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


def run(folders: list[Path] | None, workers: int, fresh: bool = False, download_only: bool = False) -> bool:
    """Print the checks; return False if any check failed."""
    res = checks(folders, workers, fresh, download_only)
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
