"""
Fire timing from satellite hotspot detections (VIIRS / MODIS active fire points).

A burn perimeter tells us *where* a fire burned, but not *when*.  Satellite
hotspots give the timing: every detection is buffered by
``SAT_HOTSPOT_BUFFER_DIST`` metres (roughly a VIIRS pixel) and the area covered
by those buffers is accumulated overpass by overpass.

Starting from a given time, detections are followed while consecutive
overpasses are no more than ``SAT_CHAIN_MAX_GAP_DAYS`` apart (the fire's
"chain"); a longer gap is taken as the end of activity.  The fire is
considered to have reached a fraction ``f`` of its size when the covered area
first reaches ``f`` x the effective area, where

    effective area = min(burn perimeter area, final covered area)

(the minimum keeps fires with sparse detections from never reaching the
threshold).  Setup uses this twice: 5 % gives the satellite ignition time,
``COVERAGE_FRACTION`` (90 %) the end time that bounds every simulation.  The
validation report draws the same curve.

All areas are in EPSG:5070 (CONUS Albers equal-area), in m².
"""

from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.ops import unary_union

import pipelineConfig as cfg

AREA_CRS = "EPSG:5070"
MAX_GAP  = pd.Timedelta(days=cfg.SAT_CHAIN_MAX_GAP_DAYS)


def detection_times(df: pd.DataFrame) -> pd.Series:
    """Combine the ACQ_DATE and ACQ_TIME (HHMM, UTC) columns into datetimes."""
    hhmm = df[cfg.SAT_TIME_COL].astype(str).str.replace(r"\D+", "", regex=True).str.zfill(4).str[:4]
    day  = df[cfg.SAT_DATE_COL].astype(str).str.strip().str.slice(0, 10)
    return pd.to_datetime(day + " " + hhmm, errors="coerce")


def first_chain(points: gpd.GeoDataFrame, start: pd.Timestamp) -> gpd.GeoDataFrame:
    """Detections from *start* on, up to the first gap longer than SAT_CHAIN_MAX_GAP_DAYS.

    *points* needs a ``sat_datetime`` column (see detection_times).
    """
    pts = points[points["sat_datetime"] >= start].sort_values("sat_datetime")
    if pts.empty:
        return pts
    gap_after = pts["sat_datetime"].diff() > MAX_GAP
    n = int(np.argmax(gap_after.to_numpy())) if gap_after.any() else len(pts)
    return pts.iloc[:n]


@dataclass
class CoverageCurve:
    """Cumulative area covered by buffered detections, one value per overpass."""
    times:    pd.DatetimeIndex
    areas_m2: np.ndarray
    burn_m2:  float

    @property
    def effective_m2(self) -> float:
        return min(self.burn_m2, float(self.areas_m2[-1])) if len(self.areas_m2) else 0.0

    def time_to_fraction(self, fraction: float) -> pd.Timestamp:
        """First overpass at which coverage reaches fraction x effective area (NaT if never)."""
        target = fraction * self.effective_m2
        if target <= 0:
            return pd.NaT
        reached = np.nonzero(self.areas_m2 >= target)[0]
        return self.times[reached[0]] if reached.size else pd.NaT


def coverage_curve(points: gpd.GeoDataFrame, burn_geom, start: pd.Timestamp) -> CoverageCurve:
    """Coverage curve of the first chain of detections from *start* (points in AREA_CRS)."""
    chain = first_chain(points, start)
    times, areas, covered = [], [], None
    for t, group in chain.groupby("sat_datetime", sort=True):
        footprint = unary_union(group.geometry.buffer(cfg.SAT_HOTSPOT_BUFFER_DIST).to_numpy())
        covered = footprint if covered is None else covered.union(footprint)
        times.append(t)
        areas.append(covered.area)
    return CoverageCurve(pd.DatetimeIndex(times), np.asarray(areas, dtype=float), float(burn_geom.area))
