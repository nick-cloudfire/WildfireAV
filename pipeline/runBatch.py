#!/usr/bin/env python3
"""
Run the per-case pipeline for many cases in parallel.

Each case runs ``runPipelineParallel.py <case> --quiet`` in its own process, so
a case that crashes or is OOM-killed cannot take the others down.  The batch
log (your terminal / SLURM .out file) gets:

* one ``START`` / ``OK`` / ``FAIL`` line per case, with the failing step,
  the error and the path to that case's ``pipeline.log``;
* a heartbeat every ``--heartbeat`` minutes listing each running case, its
  current step and live progress (from ``<case>/status.json``), plus an ETA;
* a final report: failures grouped by step, per-step timing, and a CSV.

Usage
-----
    python runBatch.py                       # every case under FIRE_ROOT
    python runBatch.py -n 4                  # 4 cases at a time
    python runBatch.py --cases 00001 00003   # a subset
    python runBatch.py --skip-done           # resume: skip cases that already have ELMFIRE output
    python runBatch.py --dry-run             # list cases and their last status, then exit

Exit status is 1 if any case failed (so ``sbatch --dependency=afterok`` works),
143 if the batch was interrupted.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import signal
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE.parent), str(_HERE)]

import pipelineConfig as cfg
from case_metadata import case_dirs
from common import fmt_duration
from runPipelineParallel import LOG_FILE, describe_wind, plan, read_status

DRIVER = _HERE / "runPipelineParallel.py"


# ---------------------------------------------------------------------------
# Case discovery
# ---------------------------------------------------------------------------

def is_done(case_dir: Path) -> bool:
    """True if ELMFIRE has already produced time-of-arrival output."""
    outputs = case_dir / cfg.ELMFIRE_OUTPUTS_SUBDIR
    return outputs.is_dir() and any(outputs.glob("time_of_arrival_*.tif"))


def discover(case_root: Path, specific: list[str] | None, skip_done: bool) -> list[Path]:
    if specific:
        folders = [case_root / name.zfill(5) if name.isdigit() else case_root / name for name in specific]
        for f in folders:
            if not f.is_dir():
                print(f"WARNING: case directory not found: {f}")
        folders = [f for f in folders if f.is_dir()]
    else:
        folders = case_dirs(case_root)
    if skip_done:
        before = len(folders)
        folders = [f for f in folders if not is_done(f)]
        if before - len(folders):
            print(f"Skipping {before - len(folders)} case(s) that already have ELMFIRE output.")
    return folders


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------

@dataclass
class Result:
    case: str
    state: str                 # ok | failed | interrupted | not_started
    seconds: float = 0.0
    step: str | None = None
    error: str | None = None
    status: dict = field(default_factory=dict)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _signal_error(rc: int) -> str:
    sig = -rc if rc < 0 else rc - 128
    try:
        name = signal.Signals(sig).name
    except ValueError:
        name = f"signal {sig}"
    msg = f"process killed by {name}"
    if sig == signal.SIGKILL:
        msg += " — most likely out of memory (check `sacct -j $SLURM_JOB_ID -o JobID,MaxRSS,State`)"
    return msg


class Batch:
    def __init__(self, folders: list[Path], workers: int, heartbeat_s: float):
        self.folders = folders
        self.workers = max(1, min(workers, len(folders)))
        self.heartbeat_s = heartbeat_s
        self.results: list[Result] = []
        self.running: dict[str, float] = {}       # case -> monotonic start
        self.stopping = False
        self._procs: set[subprocess.Popen] = set()
        self._by_name = {f.name: f for f in folders}
        # Re-entrant: the signal handler runs on the main thread and may
        # interrupt it while it already holds the lock (e.g. mid-heartbeat).
        self._lock = threading.RLock()
        self._t0 = time.monotonic()
        # Share the CPUs between cases so GDAL/OpenMP/BLAS don't each grab every core.
        threads = str(max(1, cfg.AVAILABLE_CPUS // self.workers))
        self._case_env = {**os.environ}
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "GDAL_NUM_THREADS"):
            self._case_env.setdefault(var, threads)

    # -- events --------------------------------------------------------------

    def _counts(self) -> str:
        done = len(self.results)
        failed = sum(r.state == "failed" for r in self.results)
        s = f"{done}/{len(self.folders)} done"
        if failed:
            s += f", {failed} failed"
        eta = self._eta()
        return s + (f", ETA ~{fmt_duration(eta)}" if eta else "")

    def _eta(self) -> float | None:
        durs = [r.seconds for r in self.results if r.state in ("ok", "failed")]
        remaining = len(self.folders) - len(self.results)
        if not durs or not remaining:
            return None
        return statistics.mean(durs) * remaining / self.workers

    def _print(self, line: str) -> None:
        with self._lock:
            print(line, flush=True)

    # -- one case --------------------------------------------------------------

    def _run_one(self, folder: Path) -> Result:
        name = folder.name
        if self.stopping:
            return Result(name, "not_started")
        with self._lock:
            self.running[name] = time.monotonic()
            queued = len(self.folders) - len(self.results) - len(self.running)
        self._print(f"{_ts()}  START  {name}   [{len(self.running)} running, {queued} queued]")

        t0 = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-u", str(DRIVER), str(folder), "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=self._case_env,
        )
        with self._lock:
            self._procs.add(proc)
        _, stderr = proc.communicate()
        with self._lock:
            self._procs.discard(proc)
            self.running.pop(name, None)

        rc = proc.returncode
        st = read_status(folder) or {}
        res = Result(name, "ok", time.monotonic() - t0, st.get("step"), st.get("error"), st)
        if rc == 0:
            pass
        elif rc == 143 or st.get("state") == "interrupted":
            res.state = "interrupted"
        else:
            res.state = "failed"
            if rc < 0 or rc > 128:
                res.error = _signal_error(rc)
            elif not res.error:
                tail = (stderr or "").strip().splitlines()[-3:]
                res.error = " | ".join(tail) or f"exit code {rc}"
        self._report(res, folder)
        return res

    def _report(self, r: Result, folder: Path) -> None:
        with self._lock:
            self.results.append(r)
        tag = {"ok": "OK   ", "failed": "FAIL ", "interrupted": "STOP "}.get(r.state, r.state)
        line = f"{_ts()}  {tag}  {r.case}  {fmt_duration(r.seconds):>7}   [{self._counts()}]"
        if r.state != "ok":
            line += f"\n            at step {r.step or '?'}: {r.error or 'unknown error'}"
            line += f"\n            log: {folder / LOG_FILE}"
        self._print(line)

    # -- heartbeat -------------------------------------------------------------

    def _heartbeat(self) -> None:
        elapsed = fmt_duration(time.monotonic() - self._t0)
        lines = [f"{_ts()}  ····  {elapsed} elapsed · {self._counts()} · {len(self.running)} running"]
        now = time.monotonic()
        for name, started in sorted(self.running.items()):
            st = read_status(self._by_name[name]) or {}
            step = st.get("step") or "starting"
            idx = f"{st.get('step_index', '?')}/{st.get('n_steps', '?')}"
            detail = st.get("detail") or ""
            lines.append(f"            {name}  {idx:>5} {step:<16} {fmt_duration(now - started):>7}"
                         + (f"  {detail}" if detail else ""))
        self._print("\n".join(lines))

    # -- signals ---------------------------------------------------------------

    def _stop(self, signum, _frame) -> None:
        if self.stopping:
            return
        self.stopping = True
        self._print(f"\n{_ts()}  {signal.Signals(signum).name} received — stopping "
                    f"{len(self._procs)} running case(s); queued cases will not start.")
        with self._lock:
            for p in self._procs:
                p.terminate()

    # -- main loop -------------------------------------------------------------

    def run(self) -> list[Result]:
        old = {s: signal.signal(s, self._stop) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                pending = {pool.submit(self._run_one, f) for f in self.folders}
                next_hb = time.monotonic() + self.heartbeat_s
                while pending:
                    timeout = max(1.0, next_hb - time.monotonic()) if self.heartbeat_s > 0 else None
                    _, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                    if self.heartbeat_s > 0 and time.monotonic() >= next_hb:
                        if self.running:
                            self._heartbeat()
                        next_hb = time.monotonic() + self.heartbeat_s
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        return self.results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_summary_csv(results: list[Result], case_root: Path) -> Path:
    step_keys = [k for k, _ in plan()]
    out = case_root / f"run_summary_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "state", "seconds", "failed_step", "error", "host", *[f"t_{k}" for k in step_keys]])
        for r in sorted(results, key=lambda r: r.case):
            steps = r.status.get("steps", {})
            w.writerow([
                r.case, r.state, round(r.seconds), r.step if r.state != "ok" else "",
                r.error or "", r.status.get("host", ""),
                *[(steps.get(k) or {}).get("seconds") if (steps.get(k) or {}).get("state") == "done" else ""
                  for k in step_keys],
            ])
    return out


def print_report(results: list[Result], total_s: float, csv_path: Path | None) -> None:
    by_state = {s: [r for r in results if r.state == s] for s in ("ok", "failed", "interrupted", "not_started")}
    print("\n" + "=" * 72)
    print(f"BATCH FINISHED in {fmt_duration(total_s)} — "
          + ", ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in by_state.items() if v)
          + f" (of {len(results)})")

    timings: dict[str, list[float]] = {}
    for r in results:
        for k, s in r.status.get("steps", {}).items():
            if s.get("state") == "done" and s.get("seconds") is not None:
                timings.setdefault(k, []).append(s["seconds"])
    if timings:
        print("\nStep timing over cases that ran it (median / max):")
        for k, _ in plan():
            if k in timings:
                v = timings[k]
                print(f"  {k:<24} {fmt_duration(statistics.median(v)):>7} / {fmt_duration(max(v)):>7}   (n={len(v)})")

    failed = by_state["failed"] + by_state["interrupted"]
    if failed:
        print("\nFailures by step:")
        groups: dict[str, list[Result]] = {}
        for r in failed:
            groups.setdefault(r.step or "?", []).append(r)
        for step, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f"  {step} ({len(rs)}):")
            for r in sorted(rs, key=lambda r: r.case):
                print(f"    {r.case}  {r.state:<11} {(r.error or '')[:110]}")
        print(f"\n  Details: <case>/{LOG_FILE}.  Re-run just these with:\n"
              f"    --cases {' '.join(sorted(r.case for r in failed))}")
    if by_state["not_started"]:
        print(f"\n{len(by_state['not_started'])} case(s) never started; resume with --skip-done.")
    if csv_path:
        print(f"\nSummary CSV: {csv_path}")
    print("=" * 72)


def run_batch(folders: list[Path], workers: int, heartbeat_min: float = None) -> int:
    """Run cases and print the report.  Returns a process exit code."""
    if not folders:
        print("No cases to process.")
        return 0
    heartbeat_min = cfg.BATCH_HEARTBEAT_MIN if heartbeat_min is None else heartbeat_min
    batch = Batch(folders, workers, heartbeat_s=heartbeat_min * 60)
    print(f"Running {len(folders)} case(s), {batch.workers} at a time  ·  wind={describe_wind()}  ·  "
          f"heartbeat every {heartbeat_min:g} min  ·  per-case logs: <case>/{LOG_FILE}\n", flush=True)
    t0 = time.monotonic()
    results = batch.run()
    csv_path = None
    try:
        csv_path = write_summary_csv(results, folders[0].parent)
    except OSError as e:
        print(f"WARNING: could not write summary CSV: {e}")
    print_report(results, time.monotonic() - t0, csv_path)
    if batch.stopping:
        return 143
    return 1 if any(r.state == "failed" for r in results) else 0


def print_dry_run(folders: list[Path]) -> None:
    print(f"{'CASE':<8} {'LAST STATE':<12} {'STEP':<26} NOTE")
    for f in folders:
        st = read_status(f) or {}
        state = "done" if is_done(f) else st.get("state", "new")
        step = f"{st.get('step_index', '')}/{st.get('n_steps', '')} {st.get('step') or ''}" if st else ""
        print(f"{f.name:<8} {state:<12} {step:<26} {(st.get('error') or '')[:60]}")
    print(f"\n{len(folders)} case(s) would be processed.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_run_args(p: argparse.ArgumentParser) -> None:
    """Arguments shared by runBatch.py and runWildfireAV."""
    p.add_argument("--workers", "-n", type=int, default=cfg.MAX_PARALLEL_CASES,
                   help="cases to run at once")
    p.add_argument("--cases", nargs="+", metavar="ID", help="only these case folders (e.g. 00001 3)")
    p.add_argument("--skip-done", "--resume", dest="skip_done", action="store_true",
                   help="skip cases that already have ELMFIRE output (and do not clean anything)")
    p.add_argument("--heartbeat", type=float, default=cfg.BATCH_HEARTBEAT_MIN, metavar="MIN",
                   help="minutes between progress heartbeats (0 = off)")
    p.add_argument("--no-preflight", action="store_true", help="skip the environment checks")
    p.add_argument("--dry-run", action="store_true", help="list cases and exit")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--case-root", default=str(cfg.FIRE_ROOT), help="directory with numbered case folders")
    add_run_args(p)
    args = p.parse_args()

    case_root = Path(args.case_root)
    if not case_root.is_dir():
        print(f"ERROR: case root does not exist: {case_root}", file=sys.stderr)
        return 2
    folders = discover(case_root, args.cases, args.skip_done)
    if args.dry_run:
        print_dry_run(folders)
        return 0
    if not args.no_preflight:
        import preflight
        if not preflight.run(folders, args.workers):
            return 2
    return run_batch(folders, args.workers, args.heartbeat)


if __name__ == "__main__":
    sys.exit(main())
