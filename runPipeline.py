#!/usr/bin/env python
"""
Master pipeline driver for the FirePairs workflow.

- Centralizes all paths and options.
- Supports resume-from-last-successful-step via a checkpoint file.
- Supports restart-from-scratch (clean generated outputs + clear checkpoint).
"""

from pathlib import Path
import shutil
import json
from datetime import datetime
import pipelineConfig

# -------------------------
# STEUP FOR FILE LOG
# -------------------------
from contextlib import redirect_stdout, redirect_stderr
import sys

class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()

# -------------------------
# GLOBAL CONFIG
# -------------------------

FIRE_ROOT = Path(pipelineConfig.FIRE_ROOT)
FIRE_SUMMARY_CSV = Path(pipelineConfig.FIRE_SUMMARY_CSV)
FIRE_SUMMARY_WITH_SATELLITE = Path(pipelineConfig.FIRE_SUMMARY_WITH_SATELLITE_CSV)
SATELLITE_GPKG = Path(pipelineConfig.SATELLITE_GPKG)
MTBS_PERIMS = Path(pipelineConfig.MTBS_PERIMS_RAW)
USFS_POINTS = Path(pipelineConfig.USFS_POINTS_RAW)
STATE_FILE = FIRE_ROOT / "_pipeline_state.json"

# -------------------------
# RESUME / RESTART CONFIG
# -------------------------
# "resume"  = continue after last successful step in state file
# "restart" = delete generated outputs and run from scratch
RUN_MODE = "restart"  # change to "restart" to nuke generated outputs and rerun from step 0

# -------------------------
# CLEAN FUNCTION
# -------------------------

def clean_generated():
    """
    Delete all numbered FirePairs folders and derived summary files.
    DOES NOT touch the big source data like MTBS perims, USFS points, satellite GPKG, etc.
    """
    print(f"\n*** CLEAN MODE: deleting generated FirePairs cases under {FIRE_ROOT} ***")

    if not FIRE_ROOT.exists():
        print(f"  FIRE_ROOT does not exist: {FIRE_ROOT}")
        return

    # Remove 00001, 00002, ... style folders
    for child in FIRE_ROOT.iterdir():
        if child.is_dir() and child.name.isdigit() and len(child.name) == 5:
            print(f"  Removing folder {child}")
            shutil.rmtree(child, ignore_errors=True)

    # Remove derived CSVs
    for csv in (FIRE_SUMMARY_CSV, FIRE_SUMMARY_WITH_SATELLITE):
        if csv.exists():
            print(f"  Removing summary file {csv}")
            csv.unlink()

    print("*** CLEAN COMPLETE ***\n")

# -------------------------
# STATE / CHECKPOINT HELPERS
# -------------------------

def load_state() -> dict:
    """
    Load checkpoint state from STATE_FILE.
    Returns {} if missing or unreadable.
    """
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt state file -> treat as no state
            return {}
    return {}

def save_state(last_successful_step: str):
    """
    Write checkpoint state (last successful step + timestamp).
    """
    state = {
        "last_successful_step": last_successful_step,
        "updated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def clear_state():
    """
    Delete checkpoint file if it exists.
    """
    if STATE_FILE.exists():
        STATE_FILE.unlink()


# -------------------------
# STEP WRAPPERS
# -------------------------

def step_process_scars_and_points():
    import processScarsAndPoints as m
    print("\n=== STEP: process_scars_and_points ===")
    m.main()

def step_separate_scars_to_cases():
    import separateScarsAndPointsToCases as m
    print("\n=== STEP: separate_scars_to_cases ===")
    m.main()


def step_get_satellite_end_times():
    import getSatelliteEndTimes as m
    print("\n=== STEP: get_satellite_end_times ===")
    m.main()
    
def step_erase_invalid_cases():
    import eraseInvalidCases as m
    print("\n=== STEP: erase_invalid_cases ===")
    m.main()
    
def step_download_landfire():
    import getLandfireProductsForFireSim as m
    print("\n=== STEP: download_landfire ===")
    m.main()

def step_split_landfire_bands():
    import splitLandfireTifBands as m
    print("\n=== STEP: split_landfire_bands ===")
    m.main()

def step_make_adj_phi():
    import makePhiAndAdjFiles as m
    print("\n=== STEP: make_adj_phi ===")
    m.main()

def step_download_weather_wxs():
    import downloadWeatherData as m
    print("\n=== STEP: download_weather_wxs ===")
    m.main()

def step_apply_nelson_model():
    import applyNelsonModel as m
    print("\n=== STEP: apply_nelson_model ===")
    m.main_parallel()
    
def step_create_barrier_file():
    import getBarrierFile as m
    print("\n=== STEP: create_barrier_file ===")
    m.main()
    
def step_create_elmfire_input_files():
    import createElmfireInputFiles as m
    print("\n=== STEP: create_elmfire_input_files ===")
    m.main()


# Map step names to functions
STEP_FUNCTIONS = {
    "process_scars_and_points": step_process_scars_and_points,
    "separate_scars_to_cases": step_separate_scars_to_cases,
    "get_satellite_end_times": step_get_satellite_end_times,
    "erase_invalid_cases": step_erase_invalid_cases,
    "download_landfire": step_download_landfire,
    "split_landfire_bands": step_split_landfire_bands,
    "make_adj_phi": step_make_adj_phi,
    "download_weather_wxs": step_download_weather_wxs,
    "apply_nelson_model": step_apply_nelson_model,
    "create_barrier_file": step_create_barrier_file,
    "create_elmfire_input_files": step_create_elmfire_input_files,
}

# -------------------------
# MAIN
# -------------------------

def main():
    # Fixed pipeline order
    ordered_step_names = [
        "process_scars_and_points",
        "separate_scars_to_cases",
        "get_satellite_end_times",
        "erase_invalid_cases",
        "download_landfire",
        "split_landfire_bands",
        "make_adj_phi",
        "download_weather_wxs",
        "apply_nelson_model",
        "create_barrier_file",
        "create_elmfire_input_files",
    ]

    if RUN_MODE not in ("resume", "restart"):
        raise ValueError("RUN_MODE must be 'resume' or 'restart'")

    # Determine starting step index
    if RUN_MODE == "restart":
        print("\n=== RESTART MODE: cleaning and starting from scratch ===")
        clean_generated()
        clear_state()
        start_index = 0
    else:
        state = load_state()
        last = state.get("last_successful_step")

        if last in ordered_step_names:
            start_index = ordered_step_names.index(last) + 1
            print(f"\n=== RESUME MODE: last successful step = {last} ===")
            if start_index >= len(ordered_step_names):
                print("Nothing to do: pipeline already completed all steps.")
                return
            print(f"Resuming from: {ordered_step_names[start_index]}")
        else:
            print("\n=== RESUME MODE: no valid checkpoint found; starting from scratch ===")
            start_index = 0

    # Run from start_index forward; checkpoint after each successful step
    for name in ordered_step_names[start_index:]:
        func = STEP_FUNCTIONS[name]
        print(f"\n=== RUNNING STEP: {name} ===")
        try:
            func()
        except Exception:
            print(f"\n!!! STEP FAILED: {name} !!!")
            print("Checkpoint NOT updated.")
            raise
        else:
            save_state(name)
            print(f"=== STEP SUCCESS: {name} (checkpoint saved to {STATE_FILE}) ===")

    print("\nALL STEPS COMPLETED.")


if __name__ == "__main__":
    LOG_FILE = FIRE_ROOT / "pipeline.log"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(LOG_FILE, "w", encoding="utf-8") as log:
        tee = Tee(sys.stdout, log)
        with redirect_stdout(tee), redirect_stderr(tee):
            main()
