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
        row0 = min(max(row0, 0), ds.height - 1)
        col0 = min(max(col0, 0), ds.width - 1)

        for r in range(0, max_radius_cells + 1):
            r0, r1 = max(row0 - r, 0), min(row0 + r, ds.height - 1)
            c0, c1 = max(col0 - r, 0), min(col0 + r, ds.width - 1)
            arr = ds.read(1, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1), masked=False)
            valid = arr >= valid_min
            if ds.nodata is not None:
                valid &= arr != ds.nodata
            if np.issubdtype(arr.dtype, np.floating):
                valid &= ~np.isnan(arr)
            if not valid.any():
                continue
            rows, cols = np.nonzero(valid)
            rows, cols = rows + r0, cols + c0
            k = int(np.argmin((rows - row0) ** 2 + (cols - col0) ** 2))
            xc, yc = transform_xy(ds.transform, int(rows[k]), int(cols[k]), offset="center")
            return float(xc), float(yc), r > 0

    raise RuntimeError(
        f"No burnable fuel (FBFM40 >= {valid_min:g}) within {max_radius_cells} cells of the ignition point"
    )
