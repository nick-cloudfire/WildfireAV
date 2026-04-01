from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from datetime import date, datetime
import pandas as pd
import pipelineConfig as cfg

METADATA_FILENAME = "case_metadata.json"
DATETIME_COLUMNS = {
    "perim_ignition",
    cfg.COL_POINT_DISCOVERY,
    cfg.COL_POINT_FIREOUT,
    cfg.COL_SATELLITE_IGNITION,
    cfg.COL_SAT_CHAIN_END_TIME,
    cfg.COL_SATELLITE_END,
    cfg.EVENT_END_COL,
}


def metadata_path(case_dir: Path) -> Path:
    return Path(case_dir) / METADATA_FILENAME


def _normalise_scalar(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is not None:
            value = value.tz_convert("UTC").tz_localize(None)
        return value.isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def write_case_metadata(case_dir: Path, data: Dict[str, Any]) -> Path:
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    out = metadata_path(case_dir)
    payload = {k: _normalise_scalar(v) for k, v in data.items()}
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out


def read_case_metadata(case_dir: Path) -> Dict[str, Any]:
    case_dir = Path(case_dir)
    path = metadata_path(case_dir)
    if not path.exists():
        raise FileNotFoundError(f"Case metadata not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in list(data):
        if key in DATETIME_COLUMNS and data[key] is not None:
            data[key] = pd.to_datetime(data[key], errors="coerce")
    data.setdefault(cfg.COL_FOLDER, case_dir.name)
    return data


def case_dirs(root: Optional[Path] = None) -> list[Path]:
    root = Path(root or cfg.FIRE_ROOT_LOGIN_NODE)
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit())


def summary_row_to_metadata(row: pd.Series) -> Dict[str, Any]:
    data = row.to_dict()
    folder = data.get(cfg.COL_FOLDER)
    if folder is not None:
        try:
            data[cfg.COL_FOLDER] = f"{int(folder):05d}"
        except Exception:
            data[cfg.COL_FOLDER] = str(folder).zfill(5)
    return {k: _normalise_scalar(v) for k, v in data.items()}


def write_metadata_from_summary(
    summary_csv: Path,
    case_root: Optional[Path] = None,
    max_workers: int = cfg.SETUP_PIPELINE_MAX_WORKERS,
) -> int:
    summary_csv = Path(summary_csv)
    case_root = Path(case_root or cfg.FIRE_ROOT_LOGIN_NODE)
    if not summary_csv.exists():
        raise FileNotFoundError(f"Summary CSV not found: {summary_csv}")
    df = pd.read_csv(summary_csv)
    if cfg.COL_FOLDER not in df.columns:
        raise KeyError(f"Missing '{cfg.COL_FOLDER}' column in {summary_csv}")

    tasks = [
        (case_root / summary_row_to_metadata(row)[cfg.COL_FOLDER],
         summary_row_to_metadata(row))
        for _, row in df.iterrows()
    ]
    tasks = [(d, m) for d, m in tasks if d.exists()]

    def _write(args: tuple[Path, Dict[str, Any]]) -> None:
        case_dir, meta = args
        write_case_metadata(case_dir, meta)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_write, tasks))

    return len(tasks)
