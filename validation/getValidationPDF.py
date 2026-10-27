#!/usr/bin/env python3
"""
Validation report: every case's simulated burn area compared with the observed
MTBS perimeter, for each fire model (ELMFIRE, FARSITE).

Writes to FIRE_ROOT (and copies to REPORT_COPY_DIR, if set):
    validation_report.pdf     cover, four summary pages, one page per case
    validation_metrics.csv    one row per evaluated case (areas, scores, wind)
    validation_ignitions.gpkg the same table as ignition points (EPSG:4326), for GIS

    python validation/getValidationPDF.py                 # all cases
    python validation/getValidationPDF.py --cases 12 47   # only these -> validation_00012_00047.pdf

How the scores are computed: caseMetrics.py.  Page layouts: casePage.py,
summaryPages.py.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE.parent), str(_HERE)]

import matplotlib

matplotlib.use("Agg")
import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

import hrrr_grid
import pipelineConfig as cfg
from case_metadata import case_dirs
from caseMetrics import MODELS, SCORE_COLUMNS, Case, evaluate_case
from casePage import case_page, error_page
from satellite import AREA_CRS
from summaryPages import cover_page, summary_pages

log = logging.getLogger("validation")

OUT_PDF = cfg.FIRE_ROOT / "validation_report.pdf"
NOT_IGNITED_JACCARD = 0.01   # best Jaccard of all models below this: the simulation did not take off

matplotlib.rcParams.update({"pdf.fonttype": 42, "font.family": "DejaVu Sans Mono"})


def _ignited(c: Case) -> bool:
    return max((c.score(mc, "jaccard") for mc in MODELS if c.model_toas.get(mc.label)), default=0.0) \
        >= NOT_IGNITED_JACCARD


def _table(cases: list[Case]) -> gpd.GeoDataFrame:
    """One row per case: identity, scores of every model, wind; geometry = reported ignition point."""
    every_model = {f"{mc.key}_{k}": None for mc in MODELS for k in SCORE_COLUMNS}   # same columns every run
    rows, points = [], []
    for c in cases:
        rows.append({"case": c.case_id, "fire_name": c.fire_name, "fire_year": c.fire_year,
                     "ignited": _ignited(c), "simulated_h": c.duration_h, "observed_m2": c.observed_m2,
                     **every_model, **{k: v for k, v in c.metrics.items() if k != "case"}})
        points.append(c.ignition.geometry.iloc[0] if not c.ignition.empty else None)
    return gpd.GeoDataFrame(rows, geometry=points, crs=AREA_CRS).to_crs(4326)   # Case geometry is AREA_CRS


def _write_gpkg(gdf: gpd.GeoDataFrame, path: Path) -> None:
    tmp = path.with_name(f".{path.stem}.partial.gpkg")   # never leave a half-written file
    tmp.unlink(missing_ok=True)
    gdf[gdf.geometry.notna()].to_file(tmp, layer="ignitions", driver="GPKG")
    os.replace(tmp, path)


def _evaluate(folder: Path) -> Case | Exception:
    try:
        c = evaluate_case(folder)
        log.info("ok    %s  %s %s", folder.name, c.fire_name or "", c.fire_year or "")
        return c
    except Exception as e:
        log.warning("FAIL  %s: %s", folder.name, e)
        return e


def main(cases: list[str] | None = None, out_pdf: Path | None = None) -> None:
    """Report on all cases, or only *cases*; summary pages and a metrics CSV when there are several."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("pyproj").setLevel(logging.CRITICAL)
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    folders = hrrr_grid.drop_uncovered(case_dirs(cfg.FIRE_ROOT, cases))
    if not folders:
        raise RuntimeError(f"No cases found under {cfg.FIRE_ROOT}")
    single = len(folders) == 1
    if out_pdf is None:
        out_pdf = OUT_PDF if cases is None else OUT_PDF.with_name(f"validation_{'_'.join(f.name for f in folders)}.pdf")
    stem = out_pdf.stem
    out_csv = out_pdf.with_name(stem.replace("validation_report", "validation_metrics") + ".csv")
    out_gpkg = out_pdf.with_name(stem.replace("validation_report", "validation_ignitions") + ".gpkg")
    if out_csv.name == out_pdf.with_suffix(".csv").name:            # per-case reports: validation_<ids>
        out_csv, out_gpkg = out_pdf.with_suffix(".csv"), out_pdf.with_suffix(".gpkg")

    log.info("Evaluating %d case(s) …", len(folders))
    with ThreadPoolExecutor(max_workers=min(cfg.MAX_PARALLEL_CASES, len(folders))) as pool:
        results = list(pool.map(_evaluate, folders))
    evaluated   = [r for r in results if isinstance(r, Case)]
    ignited     = [c for c in evaluated if _ignited(c)]
    not_ignited = [c for c in evaluated if not _ignited(c)]
    n_failed    = len(results) - len(evaluated)
    log.info("%d evaluated (%d did not ignite), %d without model output",
             len(evaluated), len(not_ignited), n_failed)

    with PdfPages(out_pdf) as pdf:
        if not single:
            pages = [cover_page(len(results), n_failed, ignited, not_ignited, NOT_IGNITED_JACCARD)]
            pages += summary_pages(ignited) if ignited else []
            for fig in pages:
                pdf.savefig(fig)
                plt.close(fig)
        for folder, result in zip(folders, results):
            fig = case_page(result) if isinstance(result, Case) else error_page(folder.name, str(result))
            pdf.savefig(fig)
            plt.close(fig)
    log.info("Wrote %s", out_pdf)

    written = [out_pdf]
    if evaluated and not single:
        table = _table(evaluated)
        pd.DataFrame(table.drop(columns="geometry")).to_csv(out_csv, index=False)
        _write_gpkg(table, out_gpkg)
        log.info("Wrote %s and %s", out_csv.name, out_gpkg.name)
        written += [out_csv, out_gpkg]

    if cfg.REPORT_COPY_DIR:
        dest = Path(cfg.REPORT_COPY_DIR)
        dest.mkdir(parents=True, exist_ok=True)
        for f in written:
            shutil.copy2(f, dest / f.name)
        log.info("Copied %s to %s", ", ".join(f.name for f in written), dest)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", nargs="+", help="case ids (leading zeros optional); default all")
    main(ap.parse_args().cases)
