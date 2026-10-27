#!/usr/bin/env python3
"""
Validation report: every case's simulated burn area compared with the observed
MTBS perimeter, for each fire model (ELMFIRE, FARSITE).

Writes to FIRE_ROOT (and copies to REPORT_COPY_DIR, if set):
    validation_report.pdf   cover, four summary pages, one page per case
    validation_metrics.csv  one row per evaluated case (areas, scores, wind)

    python validation/getValidationPDF.py                 # all cases
    python validation/getValidationPDF.py --cases 12 47   # only these -> validation_00012_00047.pdf

How the scores are computed: caseMetrics.py.  Page layouts: casePage.py,
summaryPages.py.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE.parent), str(_HERE)]

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

import pipelineConfig as cfg
from case_metadata import case_dirs
from caseMetrics import MODELS, Case, evaluate_case
from casePage import case_page, error_page
from summaryPages import cover_page, summary_pages

log = logging.getLogger("validation")

OUT_PDF = cfg.FIRE_ROOT / "validation_report.pdf"
OUT_CSV = cfg.FIRE_ROOT / "validation_metrics.csv"
NOT_IGNITED_JACCARD = 0.01   # best Jaccard of all models below this: the simulation did not take off

matplotlib.rcParams.update({"pdf.fonttype": 42, "font.family": "DejaVu Sans Mono"})


def _ignited(c: Case) -> bool:
    return max((c.score(mc, "jaccard") for mc in MODELS if c.model_toas.get(mc.label)), default=0.0) \
        >= NOT_IGNITED_JACCARD


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
    folders = case_dirs(cfg.FIRE_ROOT, cases)
    if not folders:
        raise RuntimeError(f"No cases found under {cfg.FIRE_ROOT}")
    single = len(folders) == 1
    if out_pdf is None:
        out_pdf = OUT_PDF if cases is None else OUT_PDF.with_name(f"validation_{'_'.join(f.name for f in folders)}.pdf")
    out_csv = OUT_CSV if out_pdf == OUT_PDF else out_pdf.with_name(
        out_pdf.stem.replace("validation_report", "validation_metrics") + ".csv")

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
        pd.DataFrame([c.metrics for c in evaluated]).to_csv(out_csv, index=False)
        log.info("Wrote %s", out_csv)
        written.append(out_csv)

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
