#!/usr/bin/env python3
"""
Pre-fetch LANDFIRE rasters for all cases before running the main batch.

Why run this before runBatch.py?
---------------------------------
Each LFPS job spends 1-10 min waiting in USGS's server-side queue.  When cases
run sequentially through runPipelineParallel, that wait sits on the critical
path of every case.  This script submits all LFPS jobs concurrently and polls
them all in parallel using threads, so the total LANDFIRE wait is bounded by
the *slowest* single job rather than the sum.

After this script completes, step 1 of every case will be a fast skip-if-exists
no-op, and runBatch.py can proceed directly to the CPU/WindNinja steps.

Concurrency is controlled by LFPS_CONCURRENT_JOBS in pipelineConfig (default 32).

Usage
-----
Pre-fetch all cases:
    python prefetchLandfire.py

Pre-fetch specific cases:
    python prefetchLandfire.py --cases 00001 00003 00007

Dry-run (list cases that need LANDFIRE, then exit):
    python prefetchLandfire.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import pipelineConfig as cfg
from case_metadata import case_dirs
from getLandfireProductsForFireSim import process_folder


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_ANSI = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text

def green(t: str)  -> str: return _c("32", t)
def red(t: str)    -> str: return _c("31", t)
def yellow(t: str) -> str: return _c("33", t)
def cyan(t: str)   -> str: return _c("36", t)
def dim(t: str)    -> str: return _c("2",  t)
def bold(t: str)   -> str: return _c("1",  t)

STATUS_FMT = {
    "pending":     dim,
    "submitting":  yellow,
    "submitted":   yellow,
    "polling":     cyan,
    "executing":   cyan,
    "downloading": cyan,
    "unpacking":   cyan,
    "done":        green,
    "failed":      red,
    "skipped":     dim,
}

_SORT_ORDER = {
    "submitting":  0,
    "submitted":   1,
    "polling":     2,
    "executing":   3,
    "downloading": 4,
    "unpacking":   5,
    "failed":      6,
    "done":        7,
    "skipped":     8,
    "pending":     9,
}

_ACTIVE_STATUSES = frozenset(
    ("submitting", "submitted", "polling", "executing", "downloading", "unpacking")
)


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    name:      str
    status:    str   = "pending"
    detail:    str   = ""
    t_start:   float = field(default_factory=time.monotonic)
    elapsed_s: float = 0.0


_STATE:      dict[str, JobState] = {}
_STATE_LOCK: threading.Lock      = threading.Lock()


def _set_state(name: str, status: str, detail: str = "") -> None:
    with _STATE_LOCK:
        entry = _STATE[name]
        entry.status    = status
        entry.detail    = detail
        if status not in ("done", "failed", "skipped"):
            entry.elapsed_s = time.monotonic() - entry.t_start
        else:
            entry.elapsed_s = time.monotonic() - entry.t_start


# ---------------------------------------------------------------------------
# Custom log function per job
# ---------------------------------------------------------------------------

_QUEUE_RE = re.compile(r"(Pending|Executing|Queued).*queue position:\s*(\d+)", re.IGNORECASE)
_JOBID_RE = re.compile(r"Job ID:\s*(\S+)")


def _make_state_log(name: str):
    """Return a log callable that drives shared state from process_folder messages."""
    def log(msg: str) -> None:
        m_q = _QUEUE_RE.search(msg)
        if m_q:
            word = m_q.group(1).lower()
            st   = "executing" if word == "executing" else "polling"
            _set_state(name, st, f"queue: {m_q.group(2)}")
            return
        m_id = _JOBID_RE.search(msg)
        if m_id:
            short = m_id.group(1)[:8]
            _set_state(name, "submitted", f"job {short}…")
            return
        low = msg.lower()
        if "submitting" in low:
            _set_state(name, "submitting")
        elif "downloading" in low:
            _set_state(name, "downloading")
        elif "unpacking" in low:
            _set_state(name, "unpacking")
    return log


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

_W_CASE    = 7
_W_STATUS  = 12
_W_DETAIL  = 28
_W_ELAPSED = 9
_LINE_W    = _W_CASE + _W_STATUS + _W_DETAIL + _W_ELAPSED + 8


def _fmt_elapsed(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _render(total: int, interval: float) -> str:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _STATE_LOCK:
        snapshot = [
            JobState(
                name      = js.name,
                status    = js.status,
                detail    = js.detail,
                t_start   = js.t_start,
                elapsed_s = (time.monotonic() - js.t_start)
                            if js.status in _ACTIVE_STATUSES
                            else js.elapsed_s,
            )
            for js in _STATE.values()
        ]

    snapshot.sort(key=lambda js: (_SORT_ORDER.get(js.status, 9), js.name))

    counts: dict[str, int] = {}
    for js in snapshot:
        counts[js.status] = counts.get(js.status, 0) + 1

    header = (
        f"{'CASE':<{_W_CASE}}  "
        f"{'STATUS':<{_W_STATUS}}  "
        f"{'DETAIL':<{_W_DETAIL}}  "
        f"{'ELAPSED':>{_W_ELAPSED}}"
    )

    bar = "─" * _LINE_W
    rows: list[str] = [
        bold("LANDFIRE Pre-fetch") + f"  —  {now_str}" + dim(f"  (refresh {interval:.0f}s)"),
        bar,
        bold(header),
        bar,
    ]

    for js in snapshot:
        if js.status == "pending":
            continue
        fmt     = STATUS_FMT.get(js.status, dim)
        detail  = js.detail[:_W_DETAIL]
        elapsed = _fmt_elapsed(js.elapsed_s)
        rows.append(
            f"{js.name:<{_W_CASE}}  "
            f"{fmt(js.status):<{_W_STATUS + (len(fmt('x')) - 1)}}  "
            f"{detail:<{_W_DETAIL}}  "
            f"{elapsed:>{_W_ELAPSED}}"
        )

    rows.append(bar)

    summary_parts = []
    for s in ("submitting", "submitted", "polling", "executing",
              "downloading", "unpacking", "done", "failed", "skipped", "pending"):
        n = counts.get(s, 0)
        if n:
            fmt = STATUS_FMT.get(s, dim)
            summary_parts.append(fmt(f"{n} {s}"))
    rows.append("  ".join(summary_parts) + dim(f"  ({total} total)"))

    return "\n".join(rows)


def _display_loop(total: int, stop_event: threading.Event, interval: float) -> None:
    clear_cmd = "clear" if os.name != "nt" else "cls"
    while not stop_event.is_set():
        os.system(clear_cmd)
        print(_render(total, interval))
        stop_event.wait(interval)
    os.system(clear_cmd)
    print(_render(total, interval))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _needs_landfire(case_dir: Path) -> bool:
    return (case_dir / cfg.BURN_SHAPE_NAME).exists() and \
           not (case_dir / "LANDFIRE.tif").exists()


def _load_summary() -> pd.DataFrame | None:
    """Load the fire summary CSV (used to pass fire_year to process_folder)."""
    path = cfg.FIRE_SUMMARY_CSV_PATH
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["folder"] = df["folder"].astype(str).str.zfill(5)
    df["perim_ignition"] = pd.to_datetime(df["perim_ignition"], errors="coerce", utc=True)
    df["fire_year"] = df["perim_ignition"].dt.year
    return df.set_index("folder")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download LANDFIRE rasters for all cases in parallel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--case-root",
        default=str(cfg.FIRE_ROOT),
        help="Root directory containing numbered case folders.",
    )
    parser.add_argument(
        "--workers", "-n",
        type=int,
        default=cfg.LFPS_CONCURRENT_JOBS,
        help="Number of concurrent LFPS jobs.",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        metavar="FOLDER",
        help="Pre-fetch only these specific case folders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List cases that need LANDFIRE, then exit.",
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=2.0,
        help="Display refresh interval in seconds.",
    )
    args = parser.parse_args()

    case_root = Path(args.case_root)
    if not case_root.is_dir():
        print(f"ERROR: case root does not exist: {case_root}", file=sys.stderr)
        sys.exit(1)

    if args.cases:
        all_folders = [case_root / name for name in args.cases]
    else:
        all_folders = case_dirs(case_root)

    todo = [f for f in all_folders if _needs_landfire(f)]
    done = len(all_folders) - len(todo)

    print(f"Cases found : {len(all_folders)}")
    print(f"Already done: {done}")
    print(f"Need fetch  : {len(todo)}")

    if not todo:
        print("Nothing to do.")
        return

    if args.dry_run:
        print()
        for f in todo:
            print(f"  {f.name}")
        return

    summary = _load_summary()
    if summary is None:
        print(
            "WARNING: fire summary CSV not found — fire year will be read "
            "from each case's metadata (slightly slower).",
        )

    # Initialise shared state for every case
    for f in todo:
        _STATE[f.name] = JobState(name=f.name)

    # Start display thread
    stop_event = threading.Event()
    display_thread = threading.Thread(
        target=_display_loop,
        args=(len(todo), stop_event, args.interval),
        daemon=True,
    )
    display_thread.start()

    t0         = time.monotonic()
    ok_count   = 0
    fail_count = 0

    def _worker(folder: Path):
        log = _make_state_log(folder.name)
        return process_folder(folder, summary, log=log)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_to_folder = {pool.submit(_worker, f): f for f in todo}
            for future in as_completed(future_to_folder):
                folder = future_to_folder[future]
                try:
                    _, success, msg = future.result()
                    if success:
                        ok_count += 1
                        final_status = "skipped" if "skipped" in msg else "done"
                        _set_state(folder.name, final_status)
                    else:
                        fail_count += 1
                        _set_state(folder.name, "failed", msg[:_W_DETAIL])
                except Exception as exc:
                    fail_count += 1
                    _set_state(folder.name, "failed",
                               f"{type(exc).__name__}: {str(exc)[:20]}")
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        display_thread.join()

    total_elapsed = time.monotonic() - t0
    print(f"\n{'=' * 60}")
    print(f"Finished {len(todo)} cases in {total_elapsed:.0f}s")
    print(f"  OK  : {ok_count}")
    print(f"  FAIL: {fail_count}")


if __name__ == "__main__":
    main()
