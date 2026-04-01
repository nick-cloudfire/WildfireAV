#!/usr/bin/env python3
"""
Debug table: ws band count, m1 band count, wxs fire-window rows,
             simulation duration in hours, start time, end time.

Usage
-----
    python debugBandCounts.py                    # all cases under FIRE_ROOT
    python debugBandCounts.py --case-root /path  # custom root
    python debugBandCounts.py 00003 00006        # specific cases
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
import rasterio

import pipelineConfig as cfg
from case_metadata import case_dirs, read_case_metadata

START_COL          = cfg.COL_SATELLITE_IGNITION
END_COL            = cfg.COL_SATELLITE_END
PRECONDITIONING_H  = cfg.CONDITIONING_DAYS * 24

WS_PATH  = lambda d: d / cfg.INPUTS_SUBDIR_NAME / cfg.WS_TIF_NAME
M1_PATH  = lambda d: d / cfg.INPUTS_SUBDIR_NAME / (cfg.FMC_FILE_NAMES[0] + ".tif")
WXS_PATH = lambda d: d / cfg.INPUTS_SUBDIR_NAME / cfg.WXS_FILE_NAME


def _band_count(path: Path) -> str:
    if not path.exists():
        return "—"
    try:
        with rasterio.open(path) as src:
            return str(src.count)
    except Exception:
        return "ERR"


def _fmt_dt(val) -> str:
    if val is None:
        return "—"
    try:
        ts = pd.to_datetime(val)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        return ts.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(val)


def _duration_hours(start, end) -> str:
    try:
        s = pd.to_datetime(start)
        e = pd.to_datetime(end)
        if s.tzinfo is not None:
            s = s.tz_convert("UTC").tz_localize(None)
        if e.tzinfo is not None:
            e = e.tz_convert("UTC").tz_localize(None)
        hours = (e - s).total_seconds() / 3600
        return f"{hours:.1f}"
    except Exception:
        return "—"


def _wxs_fire_rows(wxs_path: Path) -> str:
    """Count WXS data rows that fall after the preconditioning period."""
    if not wxs_path.exists():
        return "—"
    try:
        lines = wxs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        header_idx = next(
            (i for i, ln in enumerate(lines)
             if ln.strip().startswith("Year") and "WindSpd" in ln),
            None,
        )
        if header_idx is None:
            return "ERR"
        data_rows = [ln for ln in lines[header_idx + 1:]
                     if len(re.split(r"\s+", ln.strip())) >= 10]
        fire_rows = max(0, len(data_rows) - PRECONDITIONING_H)
        return str(fire_rows)
    except Exception:
        return "ERR"


def _check(case_dir: Path) -> dict:
    name = case_dir.name
    try:
        meta = read_case_metadata(case_dir)
    except Exception:
        return dict(case=name, ws="—", m1="—", wxs="—", duration="—", start="—", end="—", ok="no meta")

    start = meta.get(START_COL)
    end   = meta.get(END_COL)

    ws_bands = _band_count(WS_PATH(case_dir))
    m1_bands = _band_count(M1_PATH(case_dir))
    wxs_rows = _wxs_fire_rows(WXS_PATH(case_dir))

    match = ""
    if ws_bands not in ("—", "ERR") and m1_bands not in ("—", "ERR"):
        match = "OK" if ws_bands == m1_bands else f"MISMATCH (diff={int(m1_bands)-int(ws_bands):+d})"

    return dict(
        case     = name,
        ws       = ws_bands,
        m1       = m1_bands,
        wxs      = wxs_rows,
        duration = _duration_hours(start, end),
        start    = _fmt_dt(start),
        end      = _fmt_dt(end),
        ok       = match,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Print ws/m1 band-count debug table.")
    parser.add_argument("cases", nargs="*", help="Case names to check (default: all).")
    parser.add_argument("--case-root", default=str(cfg.FIRE_ROOT))
    args = parser.parse_args()

    root = Path(args.case_root)
    if args.cases:
        dirs = [root / c for c in args.cases]
    else:
        dirs = case_dirs(root)

    rows = [_check(d) for d in dirs]

    # Column widths
    cw = dict(case=7, ws=4, m1=4, wxs=6, duration=9, start=16, end=16, ok=22)
    hdr = (
        f"{'CASE':<{cw['case']}}  "
        f"{'WS':>{cw['ws']}}  "
        f"{'M1':>{cw['m1']}}  "
        f"{'WXS':>{cw['wxs']}}  "
        f"{'DUR(h)':>{cw['duration']}}  "
        f"{'START':<{cw['start']}}  "
        f"{'END':<{cw['end']}}  "
        f"{'STATUS':<{cw['ok']}}"
    )
    bar = "─" * len(hdr)
    print(bar)
    print(hdr)
    print(bar)
    for r in rows:
        ok_str = r["ok"]
        print(
            f"{r['case']:<{cw['case']}}  "
            f"{r['ws']:>{cw['ws']}}  "
            f"{r['m1']:>{cw['m1']}}  "
            f"{r['wxs']:>{cw['wxs']}}  "
            f"{r['duration']:>{cw['duration']}}  "
            f"{r['start']:<{cw['start']}}  "
            f"{r['end']:<{cw['end']}}  "
            f"{ok_str:<{cw['ok']}}"
        )
    print(bar)

    n_mismatch = sum(1 for r in rows if "MISMATCH" in r["ok"])
    n_ok       = sum(1 for r in rows if r["ok"] == "OK")
    n_missing  = sum(1 for r in rows if r["ws"] == "—" or r["m1"] == "—" or r["wxs"] == "—")
    print(f"{n_ok} OK  |  {n_mismatch} mismatch  |  {n_missing} missing files  |  {len(rows)} total")


if __name__ == "__main__":
    main()
