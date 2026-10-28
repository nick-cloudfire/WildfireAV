"""
Helpers shared by the per-case pipeline steps.

Step contract
-------------
Every step module exposes ``main(case_dir=None)``:

* With a ``case_dir`` it processes that one case and **raises** on any problem,
  so the driver can stop the case and report which step failed and why.
* It returns :data:`SKIPPED` (via :func:`skipped`) when its outputs already
  exist, so the driver can tell "skipped" from "done".
* Without a ``case_dir`` it loops over every case under ``FIRE_ROOT`` via
  :func:`for_each_case`, which logs failures per case and carries on.

Progress inside long steps goes through :func:`progress`, which prints the
message and also publishes it to the case's ``status.json`` (see
``runPipelineParallel.CaseStatus``) so the batch heartbeat and
``tools/monitorBatch.py`` can show it.
"""

from __future__ import annotations

import os
import re
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import xy as transform_xy
from rasterio.windows import Window

import pipelineConfig as cfg

SKIPPED = "skipped"

# Set by runPipelineParallel while a case is running; None when a step module
# is run standalone.  Duck-typed: anything with a ``detail(str)`` method.
STATUS = None


def skipped(reason: str) -> str:
    print(f"  Skipped — {reason}")
    return SKIPPED


def progress(msg: str) -> None:
    """Print a progress line and publish it as the case's live status detail."""
    print(f"  {msg}")
    if STATUS is not None:
        STATUS.detail(msg.strip())


@contextmanager
def atomic_write(path: Path) -> Iterator[Path]:
    """Yield a temporary sibling path; rename it onto ``path`` only on success.

    Steps skip when their outputs exist, so an output must never exist half
    written (e.g. a case killed mid-write at a SLURM time limit).  The temp
    name keeps the extension so GDAL picks the right driver.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.partial{path.suffix}")
    tmp.unlink(missing_ok=True)
    try:
        yield tmp
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def require(*paths: Path, hint: str = "") -> None:
    """Raise FileNotFoundError naming every missing input (and which step makes it)."""
    missing = [Path(p) for p in paths if not Path(p).exists()]
    if missing:
        names = ", ".join(str(p) for p in missing)
        raise FileNotFoundError(f"missing input(s): {names}" + (f" — {hint}" if hint else ""))


def case_window(meta: dict, start_col: str, end_col: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (start, end) from case metadata as naive-UTC timestamps; raise if unusable."""
    out = []
    for col in (start_col, end_col):
        ts = pd.to_datetime(meta.get(col), errors="coerce")
        if pd.isna(ts):
            raise ValueError(f"case_metadata.json has no valid '{col}' (got {meta.get(col)!r})")
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        out.append(ts)
    start, end = out
    if end <= start:
        raise ValueError(f"{end_col} ({end}) is not after {start_col} ({start})")
    return start, end


# ---------------------------------------------------------------------------
# Hourly inputs: weather, winds and dead fuel moisture share one clock
# ---------------------------------------------------------------------------
# Band 1 of every hourly raster (ws/wd from WindNinja, m1/m10/m100 from Nelson)
# is the hour containing the satellite ignition, floor(SatelliteIgnitionTime),
# and band i is i - 1 hours later.  Each band's valid time (UTC) is written into
# the GeoTIFF so the alignment can be checked (and seen in QGIS / gdalinfo).

BAND_TIME_TAG = "VALID_TIME_UTC"


def ignition_hour(meta: dict) -> pd.Timestamp:
    """The hour containing the satellite ignition: valid time of band 1 of every hourly raster."""
    start, _ = case_window(meta, cfg.COL_SATELLITE_IGNITION, cfg.COL_SATELLITE_END)
    return start.floor("h")


def read_wxs(path: Path) -> pd.DataFrame:
    """Parse a RAWS .wxs file into a DataFrame indexed by naive-UTC hour."""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    header = next((i for i, ln in enumerate(lines)
                   if ln.strip().startswith("Year") and "WindSpd" in ln), None)
    if header is None:
        raise ValueError(f"no 'Year ... WindSpd' header line in {path}")
    rows = []
    for ln in lines[header + 1:]:
        p = re.split(r"\s+", ln.strip())
        if len(p) < 10:
            continue
        hhmm = p[3].zfill(4)
        ts = pd.Timestamp(int(p[0]), int(p[1]), int(p[2]), int(hhmm[:2]), int(hhmm[2:]))
        rows.append((ts, *map(float, p[4:10])))
    df = pd.DataFrame(rows, columns=["dt", "temp_C", "rh", "pcp", "wind_kph", "wind_dir_deg", "cloud_pct"])
    if df.empty:
        raise ValueError(f"no data rows in {path}")
    return df.set_index("dt").sort_index()


def write_band_times(tif: Path, times) -> None:
    """Record each band's valid time (UTC) as its description and a VALID_TIME_UTC tag."""
    with rasterio.open(tif, "r+") as ds:
        if ds.count != len(times):
            raise ValueError(f"{tif.name}: {ds.count} bands but {len(times)} times")
        for i, t in enumerate(pd.DatetimeIndex(times), start=1):
            ds.set_band_description(i, f"{t:%Y-%m-%d %H:%M} UTC")
            ds.update_tags(i, **{BAND_TIME_TAG: f"{t:%Y-%m-%dT%H:%M}Z"})


def read_band_times(tif: Path) -> pd.DatetimeIndex | None:
    """Valid times written by write_band_times, or None for a raster without them."""
    with rasterio.open(tif) as ds:
        tags = [ds.tags(i).get(BAND_TIME_TAG) for i in range(1, ds.count + 1)]
    if not all(tags):
        return None
    return pd.DatetimeIndex([pd.Timestamp(t).tz_localize(None) for t in tags])


def require_starts_at(tif: Path, first: pd.Timestamp, rerun_from: str) -> pd.DatetimeIndex:
    """Band times of an hourly raster; raise unless band 1 is valid at *first*."""
    times = read_band_times(tif)
    hint = (f"re-run from that step: python cleanPipelineOutputs.py --from-step {rerun_from} "
            f"--include-wind-tifs --cases {tif.parent.parent.name}")
    if times is None:
        raise ValueError(f"{tif.name} has no band times (made before they were recorded); {hint}")
    if times[0] != first:
        raise ValueError(f"{tif.name} band 1 is valid at {times[0]}, expected the ignition hour {first}; {hint}")
    return times


def for_each_case(fn: Callable[[Path], object], case_dir=None):
    """Run ``fn`` on one case (errors propagate) or on every case (errors logged)."""
    if case_dir is not None:
        return fn(Path(case_dir))

    from case_metadata import case_dirs
    folders = case_dirs(Path(cfg.FIRE_ROOT))
    failed = []
    for i, folder in enumerate(folders, 1):
        print(f"\n[{i}/{len(folders)}] {folder.name}")
        t0 = time.monotonic()
        try:
            fn(folder)
        except Exception as exc:
            failed.append(folder.name)
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"  ({fmt_duration(time.monotonic() - t0)})")
    print(f"\n{len(folders) - len(failed)}/{len(folders)} cases OK"
          + (f"; failed: {' '.join(failed)}" if failed else ""))
    return None


# ---------------------------------------------------------------------------
# Ignition snapping (ELMFIRE namelist and FARSITE ignition shapefile)
# ---------------------------------------------------------------------------

def snap_to_valid_fuel(
    fuels_tif: Path,
    x: float,
    y: float,
    valid_min: float = 101.0,
    max_radius_cells: int = 2000,
) -> tuple[float, float, bool]:
    """
    Snap (x, y) to the centre of the nearest pixel with fuel code >= valid_min
    (i.e. a burnable FBFM40 class).  Returns (x, y, was_moved).
    """
    with rasterio.open(fuels_tif) as ds:
        row0, col0 = ds.index(x, y)
        if not (0 <= row0 < ds.height and 0 <= col0 < ds.width):
            b = ds.bounds
            raise ValueError(
                f"Ignition point ({x:.0f}, {y:.0f}) lies outside {fuels_tif.name} "
                f"({b.left:.0f}, {b.bottom:.0f}, {b.right:.0f}, {b.top:.0f}); the LANDFIRE "
                "domain should contain the whole burn perimeter. Re-download it: "
                "cleanPipelineOutputs.py --from-step landfire --cases <id>"
            )

        # Square windows of doubling radius.  A hit at distance d is the nearest
        # one once the window covers the whole circle of radius d (d <= r).
        r = 0
        while True:
            r0, r1 = max(row0 - r, 0), min(row0 + r, ds.height - 1)
            c0, c1 = max(col0 - r, 0), min(col0 + r, ds.width - 1)
            arr = ds.read(1, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1), masked=False)
            valid = arr >= valid_min
            if ds.nodata is not None:
                valid &= arr != ds.nodata
            if np.issubdtype(arr.dtype, np.floating):
                valid &= ~np.isnan(arr)
            if valid.any():
                rows, cols = np.nonzero(valid)
                rows, cols = rows + r0, cols + c0
                d2 = (rows - row0) ** 2 + (cols - col0) ** 2
                k = int(np.argmin(d2))
                whole = r0 == 0 and c0 == 0 and r1 == ds.height - 1 and c1 == ds.width - 1
                if d2[k] <= r * r or r >= max_radius_cells or whole:
                    xc, yc = transform_xy(ds.transform, int(rows[k]), int(cols[k]), offset="center")
                    return float(xc), float(yc), r > 0
            if r >= max_radius_cells:
                break
            r = min(max(2 * r, 1), max_radius_cells)

    raise RuntimeError(
        f"No burnable fuel (FBFM40 >= {valid_min:g}) within {max_radius_cells} cells of the ignition point"
    )
