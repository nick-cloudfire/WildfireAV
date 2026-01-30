#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Jan 28 11:17:25 2026

@author: nick
"""

import pipelineConfig as cfg
from pathlib import Path
import shutil
import pandas as pd

FIRE_ROOT               = cfg.FIRE_ROOT
INFO_CSV                = cfg.FIRE_SUMMARY_WITH_SATELLITE_CSV
COL_SATELLITE_IGNITION  = cfg.COL_SATELLITE_IGNITION
COL_POINT_DISCOVERY     = cfg.COL_POINT_DISCOVERY
COL_POINT_FIREOUT       = cfg.COL_POINT_FIREOUT
COL_SATELLITE_END       = cfg.COL_SATELLITE_END
EVENT_END_COL           = cfg.EVENT_END_COL
COL_SAT_CHAIN_END_TIME  = cfg.COL_SAT_CHAIN_END_TIME
COL_SAT_END_AREA        = cfg.COL_SAT_END_AREA
MIN_HOURS_DURATION      = cfg.MIN_HOURS_DURATION

def main():
    df = pd.read_csv(
        FIRE_ROOT / INFO_CSV,
        parse_dates=[
            "perim_ignition",
            COL_POINT_DISCOVERY,
            COL_POINT_FIREOUT,
            COL_SATELLITE_IGNITION,
            COL_SAT_CHAIN_END_TIME,
            COL_SAT_END_AREA,
            COL_SATELLITE_END,
            EVENT_END_COL
        ]
    )
    toDelete = set()
    for folder in df["folder"]:
        folder-=1
        if not df[COL_SATELLITE_IGNITION][int(folder)] == df[COL_SATELLITE_IGNITION][int(folder)]:
            toDelete.add(folder+1)
        if df[COL_SATELLITE_END][int(folder)] - df[COL_SATELLITE_IGNITION][int(folder)] < pd.Timedelta(hours=MIN_HOURS_DURATION):
            toDelete.add(folder+1)
    toDelete = list(toDelete)
    for folder in toDelete:
        folder = FIRE_ROOT / f'{folder:05d}'
        if folder.exists():
            print(f"Removing folder: {folder}")
            shutil.rmtree(folder)
    
if __name__ == "__main__":
    df = main()