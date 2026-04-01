#!/usr/bin/env python3
"""
Real-time monitor for runBatch.py / runPipelineParallel.py.

Reads pipeline.log (and windninja_cli.log during step 5) for every case under
FIRE_ROOT and prints a refreshing status table every REFRESH_SECONDS seconds.

Usage
-----
    python monitorBatch.py                   # watches cfg.FIRE_ROOT
    python monitorBatch.py --case-root /path # custom root
    python monitorBatch.py --interval 5      # refresh every 5 s

Press Ctrl-C to exit.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pipelineConfig as cfg
from case_metadata import case_dirs

REFRESH_SECONDS = 10

# ---------------------------------------------------------------------------
# Step metadata — must match the print() calls in runPipelineParallel.py
# ---------------------------------------------------------------------------

STEPS: dict[int, str] = {
    1: "download_landfire",
    2: "split_landfire_bands",
    3: "make_adj_phi",
    4: "download_weather_wxs",
    5: "download_and_run_windninja",
    6: "apply_nelson_model",
    7: "create_barrier_file",
    8: "create_elmfire_input_files",
    9: "run_elmfire",
}

STEP_SHORT: dict[int, str] = {
    1: "landfire",
    2: "split bands",
    3: "phi / adj",
    4: "weather",
    5: "windninja",
    6: "nelson",
    7: "barrier",
    8: "elm inputs",
    9: "elmfire",
}

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_ANSI = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text

def green(t: str)  -> str: return _c("32",    t)
def red(t: str)    -> str: return _c("31",    t)
def yellow(t: str) -> str: return _c("33",    t)
def cyan(t: str)   -> str: return _c("36",    t)
def dim(t: str)    -> str: return _c("2",     t)
def bold(t: str)   -> str: return _c("1",     t)

STATUS_FMT = {
    "done":     green,
    "running":  cyan,
    "failed":   red,
    "starting": yellow,
    "pending":  dim,
    "skipped":  dim,
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CaseStatus:
    name:      str
    status:    str    # pending | starting | running | done | failed | skipped
    step_num:  int    # 0 = not started
    step_short:str
    detail:    str    # progress / error snippet
    elapsed_s: float  # 0 if not started


# ---------------------------------------------------------------------------
# Elapsed-time helpers
# ---------------------------------------------------------------------------

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


def _parse_first_timestamp(lines: list[str]) -> float | None:
    """
    Extract the first HH:MM:SS timestamp written by make_logger and return it
    as a Unix timestamp (using today's or yesterday's date as appropriate).
    """
    ts_re = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s")
    for line in lines:
        m = ts_re.match(line.strip())
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            now = datetime.datetime.now()
            candidate = now.replace(hour=h, minute=mi, second=s, microsecond=0)
            if candidate > now + datetime.timedelta(seconds=10):
                candidate -= datetime.timedelta(days=1)
            return candidate.timestamp()
    return None


def _elapsed_from_log(log_file: Path, lines: list[str]) -> float:
    """Best-effort elapsed seconds since case start."""
    start = _parse_first_timestamp(lines)
    if start is not None:
        return time.time() - start
    # Fall back to log-file age
    try:
        return time.time() - log_file.stat().st_mtime + (
            log_file.stat().st_mtime - log_file.stat().st_ctime
        )
    except OSError:
        return 0.0


def _done_elapsed(log_file: Path) -> float:
    """For finished cases: duration = last-write-time − creation-time of log."""
    try:
        st = log_file.stat()
        return max(0.0, st.st_mtime - st.st_ctime)
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# WindNinja progress
# ---------------------------------------------------------------------------

_WN_STEP_RE        = re.compile(r"(?:step|time)\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
_WN_DOWNLOAD_RE    = re.compile(r"(download|fetch|retriev|request|grib|http)", re.IGNORECASE)
_WN_TOTAL_STEPS_RE = re.compile(r"Running WindNinja \((\d+) hourly steps\)")


def _latest_log_detail(log_path: Path) -> str:
    """Return the most informative last line from a windninja_cli.log."""
    try:
        lines = log_path.read_text(errors="ignore").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if _WN_STEP_RE.search(s):
            return "solving"
        if _WN_DOWNLOAD_RE.search(s):
            return "downloading forecast"
        break
    return ""


def _windninja_detail(case_dir: Path) -> str:
    wn_root = case_dir / cfg.INPUTS_SUBDIR_NAME / cfg.WINDNINJA_SUBDIR
    if not wn_root.exists():
        return ""

    step_dirs  = sorted(wn_root.glob("step_*"))
    chunk_dirs = sorted(wn_root.glob("chunk_*"))

    # --- WXS mode (domainAverageInitialization, one step per hour) ---
    if step_dirs:
        # Total steps is written to pipeline.log by the WXS script
        n_total = None
        pipeline_log = case_dir / "pipeline.log"
        if pipeline_log.exists():
            try:
                m = _WN_TOTAL_STEPS_RE.search(pipeline_log.read_text(errors="ignore"))
                if m:
                    n_total = int(m.group(1))
            except OSError:
                pass

        n_started = len(step_dirs)
        tag = f"step {n_started}/{n_total}" if n_total else f"step {n_started}"

        step_logs = sorted(
            wn_root.rglob("windninja_cli.log"),
            key=lambda p: p.stat().st_mtime,
        )
        if step_logs:
            detail = _latest_log_detail(step_logs[-1])
            if detail:
                return f"{tag} · {detail}"
        return tag

    # --- wxModel mode (wxModelInitialization, chunked download) ---
    if chunk_dirs:
        chunk_logs = sorted(
            wn_root.rglob("windninja_cli.log"),
            key=lambda p: p.stat().st_mtime,
        )
        if not chunk_logs:
            return f"chunk 0/{len(chunk_dirs)}"

        active_log = chunk_logs[-1]
        chunk_name = active_log.parent.name
        chunk_idx  = int(chunk_name.split("_")[-1]) if "_" in chunk_name else 0
        tag        = f"chunk {chunk_idx + 1}/{len(chunk_dirs)}"
        detail     = _latest_log_detail(active_log)
        return f"{tag} · {detail}" if detail else tag

    return ""


# ---------------------------------------------------------------------------
# LFPS detail (step 1)
# ---------------------------------------------------------------------------

_LFPS_KEYWORDS = ("Pending", "Executing", "Queued", "Succeeded",
                  "Downloading", "Unpacking", "Submitting")
_LFPS_QUEUE_RE = re.compile(r"(Pending|Executing|Queued).*queue position:\s*(\d+)", re.IGNORECASE)


def _lfps_detail(lines: list[str]) -> str:
    for line in reversed(lines):
        stripped = line.strip()
        if any(kw in stripped for kw in _LFPS_KEYWORDS):
            # Strip leading timestamp and case tag: "HH:MM:SS [XXXXX] message"
            cleaned = re.sub(r"^\d{2}:\d{2}:\d{2}\s+\[\w+\]\s*", "", stripped)
            m = _LFPS_QUEUE_RE.search(cleaned)
            if m:
                return f"{m.group(1)} · queue: {m.group(2)}"
            return cleaned[:45]
    return ""


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def _parse_case(case_dir: Path) -> CaseStatus:
    name = case_dir.name

    # ── DONE ──────────────────────────────────────────────────────────────
    outputs = case_dir / cfg.ELMFIRE_OUTPUTS_SUBDIR
    if outputs.is_dir() and any(outputs.glob("time_of_arrival_*.tif")):
        log_file = case_dir / "pipeline.log"
        return CaseStatus(
            name, "done", 9, STEP_SHORT[9], "",
            _done_elapsed(log_file) if log_file.exists() else 0.0,
        )

    log_file = case_dir / "pipeline.log"

    # ── PENDING ────────────────────────────────────────────────────────────
    if not log_file.exists():
        return CaseStatus(name, "pending", 0, "", "", 0.0)

    try:
        content = log_file.read_text(errors="ignore")
    except OSError:
        return CaseStatus(name, "pending", 0, "", "", 0.0)

    lines = content.splitlines()
    elapsed_s = _elapsed_from_log(log_file, lines)

    # ── Parse current step ─────────────────────────────────────────────────
    step_re = re.compile(r"=== STEP (\d+)/\d+:\s*(\S+)")
    step_num   = 0
    step_label = ""
    for line in lines:
        m = step_re.search(line)
        if m:
            step_num   = int(m.group(1))
            step_label = m.group(2)

    if step_num == 0:
        return CaseStatus(name, "starting", 0, "", "", elapsed_s)

    step_short = STEP_SHORT.get(step_num, step_label)

    # ── FAILED: traceback in tail ──────────────────────────────────────────
    tail = lines[-60:]
    if any("Traceback (most recent call last)" in ln for ln in tail):
        err = ""
        for line in reversed(tail):
            if re.match(
                r"\s*\w*(Error|Exception|RuntimeError|FileNotFoundError"
                r"|ValueError|KeyError|CalledProcessError)[:\s]",
                line,
            ):
                err = line.strip()[:52]
                break
        return CaseStatus(name, "failed", step_num, step_short, err, elapsed_s)

    # ── RUNNING: build detail for current step ─────────────────────────────
    if step_num == 5:
        detail = _windninja_detail(case_dir)
    elif step_num == 1:
        detail = _lfps_detail(lines)
    else:
        detail = ""

    return CaseStatus(name, "running", step_num, step_short, detail, elapsed_s)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

_W_CASE    = 7
_W_STATUS  = 9
_W_STEP    = 20
_W_DETAIL  = 42
_W_ELAPSED = 9
_LINE_W    = _W_CASE + _W_STATUS + _W_STEP + _W_DETAIL + _W_ELAPSED + 10


def _bar() -> str:
    return "─" * _LINE_W


def _render(statuses: list[CaseStatus], interval: int) -> str:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts  = {s: sum(1 for c in statuses if c.status == s)
               for s in STATUS_FMT}

    header = (
        f"{'CASE':<{_W_CASE}}  "
        f"{'STATUS':<{_W_STATUS}}  "
        f"{'STEP':<{_W_STEP}}  "
        f"{'DETAIL':<{_W_DETAIL}}  "
        f"{'ELAPSED':>{_W_ELAPSED}}"
    )

    rows: list[str] = []
    rows.append(
        bold(f"Elmfire Pipeline Monitor")
        + f"  —  {now_str}"
        + dim(f"  (refresh {interval}s)")
    )
    rows.append(_bar())
    rows.append(bold(header))
    rows.append(_bar())

    for cs in statuses:
        if cs.status == "pending":
            continue
        fmt       = STATUS_FMT.get(cs.status, dim)
        step_str  = f"{cs.step_num}/9  {cs.step_short}" if cs.step_num else "—"
        detail    = cs.detail[:_W_DETAIL]
        elapsed   = _fmt_elapsed(cs.elapsed_s)

        rows.append(
            f"{cs.name:<{_W_CASE}}  "
            f"{fmt(cs.status):<{_W_STATUS + (len(fmt('x')) - 1)}}  "
            f"{step_str:<{_W_STEP}}  "
            f"{detail:<{_W_DETAIL}}  "
            f"{elapsed:>{_W_ELAPSED}}"
        )

    rows.append(_bar())

    summary_parts = []
    for s in ("done", "running", "failed", "starting", "pending"):
        n = counts.get(s, 0)
        if n:
            fmt = STATUS_FMT.get(s, dim)
            summary_parts.append(fmt(f"{n} {s}"))
    rows.append("  ".join(summary_parts) + dim(f"  ({len(statuses)} total)"))

    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------

_SORT_ORDER = {"running": 0, "starting": 1, "failed": 2, "done": 3,
               "pending": 4, "skipped": 5}


def _sort_key(cs: CaseStatus):
    return (_SORT_ORDER.get(cs.status, 9), cs.name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor Elmfire batch pipeline status in real time.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--case-root",
        default=str(cfg.FIRE_ROOT),
        help="Root directory containing numbered case folders.",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=REFRESH_SECONDS,
        help="Refresh interval in seconds.",
    )
    args = parser.parse_args()

    case_root = Path(args.case_root)
    if not case_root.is_dir():
        print(f"ERROR: case root does not exist: {case_root}", file=sys.stderr)
        sys.exit(1)

    clear_cmd = "clear" if os.name != "nt" else "cls"

    try:
        while True:
            folders  = case_dirs(case_root)
            statuses = sorted([_parse_case(f) for f in folders], key=_sort_key)
            os.system(clear_cmd)
            print(_render(statuses, args.interval))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")


if __name__ == "__main__":
    main()
