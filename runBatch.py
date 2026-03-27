#!/usr/bin/env python3
"""
Run runPipelineParallel.process_case for many cases in parallel.

Why ProcessPoolExecutor and not threads
---------------------------------------
process_case() redirects sys.stdout / sys.stderr to a per-case log file.
That redirection is process-wide state, so concurrent threads would clobber
each other's log handles.  Separate processes each get their own stdout/stderr
and the redirection works correctly.

Concurrency note
----------------
Each case in runPipelineParallel processes one case sequentially — API calls are
made one at a time per case.  Total concurrent API connections ≈ --workers.
Keep --workers low enough not to overwhelm the APIs or your network.
A value of 2–4 is usually a good starting point.


Usage
-----
Run all cases under FIRE_ROOT (default):
    python runBatch.py

Run with 4 parallel cases:
    python runBatch.py --workers 4

Run a specific subset of cases:
    python runBatch.py --cases 00001 00003 00007

Skip cases that already have Elmfire output (time_of_arrival_*.tif):
    python runBatch.py --skip-done

Dry-run (list cases that would be processed, then exit):
    python runBatch.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pipelineConfig as cfg
from case_metadata import case_dirs
from runPipelineParallel import process_case


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_done(case_dir: Path) -> bool:
    """Return True if Elmfire has already produced time-of-arrival output."""
    outputs = case_dir / cfg.ELMFIRE_OUTPUTS_SUBDIR
    return outputs.is_dir() and any(outputs.glob("time_of_arrival_*.tif"))


def _discover_cases(
    case_root: Path,
    specific: list[str] | None,
    skip_done: bool,
) -> list[Path]:
    if specific:
        folders = [case_root / name for name in specific]
        missing = [f for f in folders if not f.is_dir()]
        if missing:
            print("WARNING: these case directories were not found:")
            for m in missing:
                print(f"  {m}")
        folders = [f for f in folders if f.is_dir()]
    else:
        folders = case_dirs(case_root)

    if skip_done:
        before = len(folders)
        folders = [f for f in folders if not _is_done(f)]
        print(f"Skipping {before - len(folders)} already-completed cases.")

    return folders


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the per-case pipeline for multiple cases in parallel.",
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
        default=cfg.MAX_PARALLEL_CASES,
        help="Number of cases to run simultaneously (total API connections ≈ workers).",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        metavar="FOLDER",
        help="Run only these specific case folders (e.g. 00001 00003).",
    )
    parser.add_argument(
        "--skip-done",
        action="store_true",
        help="Skip cases that already have time_of_arrival_*.tif output.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the cases that would be processed, then exit.",
    )
    args = parser.parse_args()

    case_root = Path(args.case_root)
    if not case_root.is_dir():
        print(f"ERROR: case root does not exist: {case_root}", file=sys.stderr)
        sys.exit(1)

    folders = _discover_cases(case_root, args.cases, args.skip_done)
    if not folders:
        print("No cases to process.")
        return

    if args.dry_run:
        print(f"{'Case':<10}  {'Status'}")
        print("-" * 20)
        for f in folders:
            status = "done" if _is_done(f) else "pending"
            print(f"  {f.name:<10}  {status}")
        print(f"\n{len(folders)} case(s) would be processed.")
        return

    n_total   = len(folders)
    n_workers = min(args.workers, n_total)

    print(f"Cases : {n_total}  |  Workers : {n_workers}  |  Root : {case_root}")
    print()

    # Track which cases are currently executing so we can report "→ started"
    pending_queue: deque[Path] = deque(folders[n_workers:])
    running: set[str]          = {f.name for f in folders[:n_workers]}

    print(f"  {'TIME':>8}   {'RESULT':<6}  {'CASE':<10}  {'PROGRESS':>10}  NOTE")
    print(f"  {'-'*8}   {'-'*6}  {'-'*10}  {'-'*10}  ----")

    # Print the initial "started" banner
    print(
        f"  {'':>8}   {'START':<6}  "
        f"{', '.join(sorted(running)):<10}",
        flush=True,
    )

    t0         = time.monotonic()
    ok_cases:   list[str] = []
    fail_cases: list[str] = []

    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        future_to_folder = {
            pool.submit(process_case, f, True): f   # log_only=True: no stdout noise
            for f in folders
        }

        for future in as_completed(future_to_folder):
            folder  = future_to_folder[future]
            elapsed = time.monotonic() - t0

            running.discard(folder.name)

            # Start next queued case (for display purposes)
            note = ""
            if pending_queue:
                next_case = pending_queue.popleft()
                running.add(next_case.name)
                note = f"→ {next_case.name} started"

            try:
                future.result()
                ok_cases.append(folder.name)
                result_tag = "OK"
            except Exception as exc:
                fail_cases.append(folder.name)
                short_err  = f"{type(exc).__name__}: {exc}"[:60]
                result_tag = f"FAIL  {short_err}"
                note       = f"(see pipeline.log)"

            n_done    = len(ok_cases) + len(fail_cases)
            progress  = f"{n_done}/{n_total}"
            time_str  = _fmt_elapsed(elapsed)

            print(
                f"  {time_str:>8}   {result_tag:<6}  {folder.name:<10}  "
                f"{progress:>10}  {note}",
                flush=True,
            )

    total = time.monotonic() - t0
    print()
    print(f"{'='*60}")
    print(f"Finished {n_total} cases in {_fmt_elapsed(total)}")
    print(f"  OK   : {len(ok_cases)}")
    print(f"  FAIL : {len(fail_cases)}")
    if fail_cases:
        print("\nFailed cases (check pipeline.log in each folder):")
        for name in sorted(fail_cases):
            print(f"  {name}")


if __name__ == "__main__":
    # The `if __name__ == "__main__"` guard is required for ProcessPoolExecutor
    # on platforms that use the "spawn" start method (Windows, macOS default).
    # On Linux (including WSL) the default is "fork", but the guard is good practice.
    main()
