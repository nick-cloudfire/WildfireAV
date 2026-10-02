#!/usr/bin/env python3
"""
Step "nelson": dead-fuel moisture (m1/m10/m100) with the Nelson C# model.

1. Convert cc/dem/slp/asp GeoTIFFs to ENVI BSQ (the C# model's input format).
2. Run NELSON_EXE on weather.wxs + those grids (CONDITIONING_DAYS spin-up).
3. Convert the m1/m10/m100 BSQ outputs back to compressed GeoTIFFs.
4. Remove the intermediate BSQ/HDR/XML files.
"""

from pathlib import Path

import pipelineConfig as cfg
from common import atomic_write, for_each_case, require, skipped
from parallel_api import run_subprocess

NELSON_EXE = Path(cfg.NELSON_EXE)
_B = cfg.LANDFIRE_BAND_FILE_NAMES
INPUT_STEMS = [_B[4], _B[0], _B[1], _B[2]]   # cc, dem, slp, asp


def _translate(src: Path, dst: Path, *opts: str) -> None:
    run_subprocess(["gdal_translate", "-q", "--config", "GDAL_CACHEMAX", "512", *opts, str(src), str(dst)])


def _clean(folder: Path) -> None:
    for ext in ("*.bsq", "*.hdr", "*.xml"):
        for f in folder.glob(ext):
            f.unlink()


def _process(case_dir: Path):
    inputs = case_dir / cfg.INPUTS_SUBDIR_NAME
    outs = [inputs / f"{m}.tif" for m in cfg.FMC_FILE_NAMES]
    if all(p.exists() for p in outs):
        return skipped("m1/m10/m100.tif already exist")

    wxs = inputs / cfg.WXS_FILE_NAME
    require(wxs, *(inputs / f"{s}.tif" for s in INPUT_STEMS), hint="run the weather and split_bands steps first")
    require(NELSON_EXE, hint="build it with `dotnet publish -c Release` (see README) or fix NELSON_EXE")

    _clean(inputs)
    try:
        for stem in INPUT_STEMS:
            _translate(inputs / f"{stem}.tif", inputs / f"{stem}.bsq", "-of", "ENVI", "-co", "INTERLEAVE=BSQ")

        cc, dem, slp, asp = (inputs / f"{s}.bsq" for s in INPUT_STEMS)
        print(f"  Running Nelson ({cfg.CONDITIONING_DAYS} conditioning days)")
        run_subprocess([str(NELSON_EXE), str(wxs), str(dem), str(slp), str(asp), str(cc),
                        str(cfg.CONDITIONING_DAYS)], cwd=str(NELSON_EXE.parent))

        for out in outs:
            bsq = out.with_suffix(".bsq")
            require(bsq, hint="Nelson exited 0 but did not write this output")
            with atomic_write(out) as tmp:
                _translate(bsq, tmp, "-of", "GTiff", "-co", "COMPRESS=ZSTD", "-co", "BIGTIFF=YES",
                           "-co", "NUM_THREADS=8")
    finally:
        _clean(inputs)
    print(f"  Wrote {', '.join(p.name for p in outs)}")


def main(case_dir=None):
    return for_each_case(_process, case_dir)


if __name__ == "__main__":
    main()
