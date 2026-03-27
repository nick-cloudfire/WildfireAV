from herbie import FastHerbie
import pandas as pd
import numpy as np
import xarray as xr
import geopandas as gpd

xr.set_options(use_new_combine_kwarg_defaults=True)

# -----------------------------
# USER INPUT
# -----------------------------
start = "2023-07-01 11:00"   # UTC
end   = "2023-07-30 09:00"   # UTC
#lat, lon = 34.0, -118.3
elevation = 1748
outfile = "/home/nick/elmfire_validation/weather.wxs"
cache_dir = "/home/nick/data/herbie_cache"
gpkg = r"/home/nick/elmfire_validation/FirePairs/00001/ignition_point.gpkg"

DATES = pd.date_range(start=start, end=end, freq="1h")
layer = None
gdf = gpd.read_file(gpkg) if layer is None else gpd.read_file(gpkg, layer=layer)
g = gdf.geometry.iloc[0]
gdf_wgs84 = gdf.to_crs("EPSG:4326")
g = gdf_wgs84.geometry.iloc[0]
lon, lat = float(g.x), float(g.y)

# IMPORTANT: match inventory level strings exactly
# (These match what you printed: TMP 2 m above ground, RH 2 m above ground, etc.)
wanted = [
    ("TMP",  "2 m above ground"),
    ("RH",   "2 m above ground"),
    ("UGRD", "10 m above ground"),
    ("VGRD", "10 m above ground"),
    ("APCP", "surface"),
    ("TCDC", "entire atmosphere"),  # total cloud cover
]
search = "|".join([f":{v}:{lvl}" for v, lvl in wanted])

FH = FastHerbie(
    DATES,
    model="hrrr",
    product="sfc",
    fxx=[0],               # analysis at each valid time
    save_dir=cache_dir,
)

# 1) Download all hours in parallel (subset only)
FH.download(search)

# -----------------------------
# Helper: normalize H.xarray() output
# -----------------------------
def as_dataset(obj):
    """Herbie.xarray may return Dataset or list[Dataset]. Normalize to a single Dataset."""
    if isinstance(obj, list):
        # safest: merge them, overriding conflicts like heightAboveGround
        return xr.merge(obj, compat="override")
    return obj

# -----------------------------
# Open each hour, subset, and concat on time
# -----------------------------
ds_list = []
for H in FH.file_exists:
    dsi = as_dataset(H.xarray(search))
    # Make sure there's a time coordinate we can concat on
    if "time" not in dsi.coords:
        # Some GRIBs expose valid_time; fall back if needed
        if "valid_time" in dsi.coords:
            dsi = dsi.rename({"valid_time": "time"})
        else:
            # last resort: create a scalar time coord from Herbie object's valid date
            dsi = dsi.assign_coords(time=pd.to_datetime(H.date)).expand_dims("time")
    else:
        # ensure time is a dimension (not scalar) so concat is clean
        if "time" not in dsi.dims:
            dsi = dsi.expand_dims("time")

    ds_list.append(dsi)

ds = xr.concat(ds_list, dim="time")

# -----------------------------
# Pick nearest grid cell robustly
# HRRR often has 2D latitude/longitude on (y, x)
# -----------------------------
def get_latlon_coords(dset):
    # common names
    for lat_name, lon_name in [("latitude", "longitude"), ("lat", "lon")]:
        if lat_name in dset.coords and lon_name in dset.coords:
            return dset[lat_name], dset[lon_name]
    raise KeyError(f"Could not find lat/lon coords. Have coords: {list(dset.coords)}")

latc, lonc = get_latlon_coords(ds)

if latc.ndim == 1 and lonc.ndim == 1:
    # 1D lat/lon grid
    pt = ds.sel({latc.name: lat, lonc.name: lon}, method="nearest")
else:
    # 2D lat/lon grid (typical HRRR)
    dist2 = (latc - lat) ** 2 + (lonc - lon) ** 2
    # dist2 dims likely (y, x)
    dims = dist2.dims
    flat_index = int(dist2.argmin().values)
    shape = tuple(dist2.sizes[d] for d in dims)
    idx = np.unravel_index(flat_index, shape)
    indexers = {dims[i]: idx[i] for i in range(len(dims))}
    pt = ds.isel(**indexers)

# -----------------------------
# Variables: because we subset by exact :VAR:LEVEL, names will be the GRIB shortName
# (usually exactly TMP, RH, UGRD, VGRD, APCP, TCDC)
# -----------------------------
# what we expect from HRRR sfc once loaded
needed = ["t2m", "r2", "u10", "v10", "tp", "tcc"]
missing = [v for v in needed if v not in pt.data_vars]
if missing:
    raise KeyError(f"Missing variables {missing}. Have: {list(pt.data_vars)}")

T  = pt["t2m"]   # K
RH = pt["r2"]    # %
U  = pt["u10"]   # m/s
V  = pt["v10"]   # m/s
P  = pt["tp"]    # kg/m^2 ~ mm
C  = pt["tcc"]   # 0-1

# -----------------------------
# Convert units
# -----------------------------
temp_c = (T.values - 273.15)

rh = RH.values

wind_ms = np.sqrt(U.values**2 + V.values**2)
wind_kmh = wind_ms * 3.6
wind_dir = (270 - np.degrees(np.arctan2(V.values, U.values))) % 360

cloud_raw = C.values
cloud_pct = np.where(cloud_raw <= 1.5, cloud_raw * 100, cloud_raw)

# Hourly precip:
# With fxx=0 analysis files, APCP may be 0 or not meaningful for hourly.
# If you want true hourly accumulation, consider using fxx=[1] and differencing.
precip_mm = P.values

times = pd.to_datetime(ds["time"].values)

out = pd.DataFrame({
    "time": times,
    "Temp": np.round(temp_c).astype(int),
    "RH": np.round(rh).astype(int),
    "Pcp": np.round(precip_mm, 3),
    "WindSpd": np.round(wind_kmh).astype(int),
    "WindDir": np.round(wind_dir).astype(int),
    "Cloud": np.round(cloud_pct).astype(int),
}).sort_values("time").reset_index(drop=True)

# -----------------------------
# Write WXS
# -----------------------------
with open(outfile, "w") as f:
    f.write(f"RAWS_ELEVATION: {elevation}\n")
    f.write("RAWS_UNITS: METRIC\n")
    f.write("RAWS_WINDS: HRRR\n")
    f.write("Year Mth Day Time Temp RH HrlyPcp WindSpd WindDir CloudCov\n")

    for _, r in out.iterrows():
        t = r["time"]
        f.write(
            f"{t.year:4d} {t.month:2d} {t.day:2d} {t.hour:02d}00 "
            f"{r.Temp:4d} {r.RH:3d} {r.Pcp:7.3f} {r.WindSpd:3d} {r.WindDir:3d} {r.Cloud:3d}\n"
        )

print("Saved:", outfile)
print(out.head())
print("Data vars returned:", list(pt.data_vars))