#!/usr/bin/env python3
"""
Per-case pipeline driver.

Runs every simulation-preparation and fire-model step for one case directory
(as produced by ``setupPipeline.py``).  ``runBatch.py`` / ``runWildfireAV``
launch one of these per case as a separate process.

Usage
-----
    python runPipelineParallel.py /path/to/00001            # log to terminal + pipeline.log
    python runPipelineParallel.py /path/to/00001 --quiet    # pipeline.log only (batch mode)
    python runPipelineParallel.py --list-steps              # show the step plan

Per case it writes
------------------
- ``pipeline.log``  – full, timestamped output of every step (and their subprocesses)
- ``status.json``   – machine-readable progress: current step, live detail,
                      per-step state/duration, error.  Read by runBatch's
                      heartbeat and by tools/monitorBatch.py.

Steps
-----
The step plan depends on ``WINDNINJA_SOURCE`` (see ``plan()``):

  "install": landfire, split_bands, adj_phi, weather, windninja, nelson,
             barrier, elmfire_inputs, prepare_farsite, run_elmfire, run_farsite
  "farsite": as above without windninja, then run_farsite,
             farsite_wind_to_geotiff, run_elmfire

Every step skips itself when its outputs already exist, so re-running a case
resumes where it stopped.

Exit status: 0 ok, 1 failed, 143 interrupted (SIGTERM/SIGINT).
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE.parent), str(_HERE)]   # repo root (pipelineConfig) + pipeline/

import pipelineConfig as cfg
import common
from common import SKIPPED, fmt_duration

STATUS_FILE = "status.json"
LOG_FILE    = "pipeline.log"


# ---------------------------------------------------------------------------
# Step plan
# ---------------------------------------------------------------------------

def plan(wind_source: str | None = None) -> list[tuple[str, str]]:
    """Return the ordered [(step_key, module_name), ...] for a wind source."""
    wind_source = wind_source or cfg.WINDNINJA_SOURCE
    if wind_source not in ("install", "farsite"):
        raise ValueError(f"WINDNINJA_SOURCE must be 'install' or 'farsite', got {wind_source!r}")
    steps = [
        ("landfire",        "getLandfireProductsForFireSim"),
        ("split_bands",     "splitLandfireTifBands"),
        ("adj_phi",         "makePhiAndAdjFiles"),
        ("weather",         "downloadWeatherData"),
        ("live_moisture",   "liveFuelMoisture"),
    ]
    if wind_source == "install":
        steps.append(("windninja", "runWindninja"))
    steps += [
        ("nelson",          "applyNelsonModel"),
        ("barrier",         "getBarrierFile"),
        ("elmfire_inputs",  "createElmfireInputFiles"),
        ("prepare_farsite", "prepareFarsite"),
    ]
    if wind_source == "install":
        steps += [("run_elmfire", "runElmfireCase"), ("run_farsite", "runFarsiteCase")]
    else:
        steps += [
            ("run_farsite",             "runFarsiteCase"),
            ("farsite_wind_to_geotiff", "farsiteWindToGeotiff"),
            ("run_elmfire",             "runElmfireCase"),
        ]
    return steps


def describe_wind() -> str:
    if cfg.WINDNINJA_SOURCE == "farsite":
        return "farsite"
    return f"windninja/{cfg.WINDNINJA_MODE}"


# ---------------------------------------------------------------------------
# status.json
# ---------------------------------------------------------------------------

def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def read_status(case_dir: Path) -> dict | None:
    try:
        return json.loads((Path(case_dir) / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return None


class CaseStatus:
    """Owns <case>/status.json; every update is written atomically."""

    MIN_DETAIL_INTERVAL_S = 5.0   # throttle progress() writes on chatty steps

    def __init__(self, case_dir: Path, steps: list[tuple[str, str]]):
        self.path = case_dir / STATUS_FILE
        self._lock = threading.Lock()
        self._last_detail_write = 0.0
        self._flush_timer: threading.Timer | None = None
        self.data = {
            "case": case_dir.name,
            "state": "running",
            "step": None,
            "step_index": 0,
            "n_steps": len(steps),
            "detail": "",
            "wind": describe_wind(),
            "started": _now(),
            "step_started": None,
            "updated": _now(),
            "finished": None,
            "seconds": None,
            "error": None,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "slurm_job": os.environ.get("SLURM_JOB_ID"),
            "steps": {key: {"state": "pending", "seconds": None} for key, _ in steps},
        }
        self._t0 = time.monotonic()
        self.write()

    def write(self) -> None:
        with self._lock:
            self.data["updated"] = _now()
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            os.replace(tmp, self.path)

    def begin(self, index: int, key: str) -> None:
        self.data.update(step=key, step_index=index, detail="", step_started=_now())
        self.data["steps"][key]["state"] = "running"
        self.write()

    def end(self, key: str, state: str, seconds: float) -> None:
        self.data["steps"][key].update(state=state, seconds=round(seconds, 1))
        self.data["detail"] = ""
        self.write()

    def detail(self, msg: str) -> None:
        """Publish live progress, at most every MIN_DETAIL_INTERVAL_S.

        A throttled message is flushed by a timer, so the last one before a
        long silent stretch (e.g. "WindNinja chunk 1/2") is never lost.
        """
        self.data["detail"] = msg[:200]
        wait = self._last_detail_write + self.MIN_DETAIL_INTERVAL_S - time.monotonic()
        if wait <= 0:
            self._last_detail_write = time.monotonic()
            self.write()
        elif self._flush_timer is None or not self._flush_timer.is_alive():
            self._flush_timer = threading.Timer(wait, self._flush)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def _flush(self) -> None:
        self._last_detail_write = time.monotonic()
        self.write()

    def finish(self, state: str, error: str | None = None) -> None:
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        if self.data["step"] and state != "done":
            self.data["steps"][self.data["step"]]["state"] = state
        self.data.update(state=state, error=error, finished=_now(),
                         seconds=round(time.monotonic() - self._t0, 1))
        self.write()


# ---------------------------------------------------------------------------
# Logging: prefix every line (including subprocess output) with a timestamp
# ---------------------------------------------------------------------------

class _Stamped:
    """Text stream that timestamps each line and fans out to several streams.

    Deliberately has no ``fileno()``, so ``parallel_api.run_subprocess`` pipes
    child output through it line by line and those lines get stamped too.
    """

    def __init__(self, *streams):
        self.streams = streams
        self._bol = True
        self._lock = threading.Lock()

    def write(self, data: str) -> int:
        if not data:
            return 0
        with self._lock:
            out = []
            for piece in data.splitlines(keepends=True):
                if self._bol and piece.strip():
                    out.append(time.strftime("%H:%M:%S "))
                out.append(piece)
                self._bol = piece.endswith(("\n", "\r"))
            text = "".join(out)
            for s in self.streams:
                s.write(text)
                s.flush()
        return len(data)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()

    def isatty(self) -> bool:
        return False


class Interrupted(BaseException):
    """Raised from the SIGTERM/SIGINT handler (e.g. SLURM time limit, scancel)."""


def _on_signal(signum, _frame):
    # SLURM signals every process in the job and runBatch forwards SIGTERM too;
    # ignore repeats so the status/log cleanup below is not itself interrupted.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise Interrupted(signal.Signals(signum).name)


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------

def process_case(case_dir: Path, quiet: bool = False) -> bool:
    """Run every step for one case.  Returns True on success, False on failure."""
    case_dir = Path(case_dir).absolute()
    if not case_dir.is_dir():
        raise FileNotFoundError(f"case directory not found: {case_dir}")

    steps = plan()
    status = CaseStatus(case_dir, steps)
    common.STATUS = status

    with open(case_dir / LOG_FILE, "w", encoding="utf-8", buffering=1) as log:
        out = _Stamped(log) if quiet else _Stamped(log, sys.__stdout__)
        with redirect_stdout(out), redirect_stderr(out):
            return _run_steps(case_dir, steps, status)


def _run_steps(case_dir: Path, steps, status: CaseStatus) -> bool:
    n = len(steps)
    print(f"CASE {case_dir.name}  ·  {n} steps  ·  wind={describe_wind()}  ·  "
          f"host={status.data['host']}  pid={os.getpid()}"
          + (f"  slurm_job={status.data['slurm_job']}" if status.data["slurm_job"] else ""))
    print(f"  dir: {case_dir}")

    t_case = time.monotonic()
    key = None
    try:
        for i, (key, module) in enumerate(steps, 1):
            print(f"\n=== STEP {i}/{n}: {key} ===")
            status.begin(i, key)
            t0 = time.monotonic()
            result = importlib.import_module(module).main(case_dir)
            elapsed = time.monotonic() - t0
            state = "skipped" if result == SKIPPED else "done"
            status.end(key, state, elapsed)
            print(f"--- {key}: {state} ({fmt_duration(elapsed)})")
    except Interrupted as sig:
        print(f"\n!!! INTERRUPTED by {sig} during step '{key}' — re-run to resume.")
        status.finish("interrupted", f"interrupted by {sig} during {key}")
        raise
    except Exception as exc:
        traceback.print_exc()
        err = f"{type(exc).__name__}: {exc}".strip()
        print(f"\n!!! CASE FAILED at step '{key}': {err}")
        status.finish("failed", err.splitlines()[0][:500])
        return False

    status.finish("done")
    print(f"\nCASE COMPLETED in {fmt_duration(time.monotonic() - t_case)}.")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the per-case pipeline for one case directory.")
    p.add_argument("case_dir", nargs="?", help="case directory, e.g. /scratch/me/FirePairs/00001")
    p.add_argument("--quiet", "-q", action="store_true", help="write only to pipeline.log")
    p.add_argument("--list-steps", action="store_true", help="print the step plan and exit")
    args = p.parse_args(argv)

    if args.list_steps or not args.case_dir:
        print(f"Step plan (wind={describe_wind()}):")
        for i, (key, module) in enumerate(plan(), 1):
            print(f"  {i:2d}. {key:<24} {module}.py")
        if not args.case_dir and not args.list_steps:
            print("\nFor many cases use runBatch.py or runWildfireAV.")
        return 0

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        return 0 if process_case(Path(args.case_dir), quiet=args.quiet) else 1
    except Interrupted:
        return 143


if __name__ == "__main__":
    sys.exit(main())
