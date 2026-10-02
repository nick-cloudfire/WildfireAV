#!/usr/bin/env python3
"""
Live status table for a batch run, built from every case's ``status.json``
(written by pipeline/runPipelineParallel.py).

Usage
-----
    python tools/monitorBatch.py               # refresh every 10 s (Ctrl-C to exit)
    python tools/monitorBatch.py --once        # print once (log files, cron, ssh one-liners)
    python tools/monitorBatch.py --all         # include finished / not-started cases
    python tools/monitorBatch.py --failed      # only failed / interrupted / dead cases, with errors

States: running · done · failed · interrupted · dead (status says running but
the process is gone — e.g. OOM-killed) · new (never started).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "pipeline")]

import pipelineConfig as cfg
from case_metadata import case_dirs
from common import fmt_duration
from runPipelineParallel import read_status

COLOURS = {"running": "36", "done": "32", "failed": "31", "interrupted": "33", "dead": "31;1", "new": "2"}
ORDER = ["running", "failed", "dead", "interrupted", "done", "new"]
HOST = socket.gethostname()


def _age(iso: str | None) -> float:
    if not iso:
        return 0.0
    return (dt.datetime.now() - dt.datetime.fromisoformat(iso)).total_seconds()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def case_state(case_dir: Path) -> dict:
    st = read_status(case_dir)
    if not st:
        return {"case": case_dir.name, "state": "new"}
    if st.get("state") == "running" and st.get("host") == HOST and st.get("pid") and not _alive(st["pid"]):
        st["state"] = "dead"
        st["error"] = "process gone without updating status (killed? out of memory?)"
    return st


def render(rows: list[dict], show_all: bool, failed_only: bool, colour: bool) -> str:
    c = (lambda s, t: f"\033[{COLOURS.get(s, '0')}m{t}\033[0m") if colour else (lambda s, t: t)
    counts = {s: sum(r["state"] == s for r in rows) for s in ORDER}
    out = [f"Pipeline monitor — {dt.datetime.now():%Y-%m-%d %H:%M:%S} — {cfg.FIRE_ROOT}",
           f"{'CASE':<7} {'STATE':<12} {'STEP':<26} {'IN STEP':>8} {'TOTAL':>8}  DETAIL / ERROR",
           "-" * 110]
    for r in sorted(rows, key=lambda r: (ORDER.index(r["state"]), r["case"])):
        s = r["state"]
        if failed_only and s not in ("failed", "dead", "interrupted"):
            continue
        if not show_all and not failed_only and s in ("done", "new"):
            continue
        step = f"{r.get('step_index', '')}/{r.get('n_steps', '')} {r.get('step') or ''}" if r.get("step") else ""
        running = s == "running"
        in_step = fmt_duration(_age(r.get("step_started"))) if running else ""
        total = fmt_duration(_age(r.get("started")) if running else
                             (r.get("seconds") or _age(r.get("started")) - _age(r.get("updated"))))
        note = (r.get("detail") if running else r.get("error")) or ""
        out.append(f"{r['case']:<7} {c(s, f'{s:<12}')} {step:<26} {in_step:>8} {total:>8}  {note[:80]}")
    out.append("-" * 110)
    out.append("  ".join(c(s, f"{counts[s]} {s}") for s in ORDER if counts[s]) + f"   ({len(rows)} cases)")
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--case-root", default=str(cfg.FIRE_ROOT))
    p.add_argument("--interval", "-i", type=int, default=10, help="refresh seconds")
    p.add_argument("--once", action="store_true", help="print once and exit")
    p.add_argument("--all", action="store_true", help="also list done / never-started cases")
    p.add_argument("--failed", action="store_true", help="only list failed / dead / interrupted cases")
    args = p.parse_args()

    root = Path(args.case_root)
    if not root.is_dir():
        sys.exit(f"case root does not exist: {root}")
    colour = sys.stdout.isatty() and not args.once
    try:
        while True:
            text = render([case_state(f) for f in case_dirs(root)], args.all, args.failed, colour)
            if args.once:
                print(text)
                return
            print("\033[2J\033[H" + text, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
