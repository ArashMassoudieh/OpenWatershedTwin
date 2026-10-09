#!/usr/bin/env python3
"""Reference-ET feed for a watershed twin: gridMET for the past (as the model was calibrated), Open-Meteo after it.

Writes one OpenHydroQual series per unit: rows `t,value` (OHQ day serial, reference ET in m/day), daily, from
`start` to the end of the Open-Meteo forecast. The twin reads them through its forcing map (provider "csv").

  1. gridMET 'pet' (ASCE standardized Penman-Monteith grass reference ET, 1/24 deg, daily; about a day behind),
     area-weighted over each unit's polygon (weights of the overlapping cells, as in the calibration project's
     41_forcing.py). gridMET days end at midnight MST (07 UTC); each value is placed at 19 UTC of its day.
  2. Open-Meteo daily et0_fao_evapotranspiration (FAO-56 grass reference) at the unit centroids for the days after
     the last gridMET day, times `openmeteo.scale`. The ratio of the two over the overlap is printed, to check it.
gridMET years are cached as NetCDF (the current year is refreshed when older than `refresh_hours`).

  et_feed.py --config watersheds/RockCreek/forcing/et_feed.json
Config (paths relative to the config file):
  {"units_geojson": "../gis/subcatchments.geojson", "unit_id_field": "id", "output_dir": "../deployments/live/forcing",
   "file_pattern": "et_{id}.csv", "cache_dir": "../../../cache/gridmet", "start": "2024-10-01",
   "refresh_hours": 6, "openmeteo": {"past_days": 10, "forecast_days": 10, "scale": 1.0}}
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pygridmet
import requests
import xarray as xr
from shapely.geometry import box

EPOCH = dt.datetime(1899, 12, 30)


def log(*a):
    print("[et_feed]", *a, flush=True)


def serial_day(d):
    return (pd.Timestamp(d).to_pydatetime().replace(tzinfo=None) - EPOCH).days


def gridmet_year(year, bounds, cache, refresh_h, today):
    f = cache / f"gridmet_pet_{year}.nc"
    current = year == today.year
    if f.exists() and (not current or time.time() - f.stat().st_mtime < refresh_h * 3600):
        return xr.open_dataset(f).load()
    end = min(dt.date(year, 12, 31), today - dt.timedelta(days=1))
    for back in range(0, 4):                         # the newest day may not be published yet
        try:
            ds = pygridmet.get_bygeom(bounds, (f"{year}-01-01", str(end - dt.timedelta(days=back))), variables=["pet"])
            break
        except Exception as e:                       # noqa: BLE001 (service range errors vary)
            err = e
    else:
        raise SystemExit(f"gridMET {year}: {err}")
    ds = ds.load()
    tmp = f.with_suffix(".tmp")
    ds.to_netcdf(tmp)
    tmp.replace(f)
    return ds


def weights(lat, lon, units):
    d = 1 / 48
    cells = [(i, j, box(lon[j] - d, lat[i] - d, lon[j] + d, lat[i] + d)) for i in range(len(lat)) for j in range(len(lon))]
    g = gpd.GeoDataFrame({"i": [c[0] for c in cells], "j": [c[1] for c in cells]},
                         geometry=[c[2] for c in cells], crs=4326).to_crs(units.crs)
    ov = gpd.overlay(g, units[["uid", "geometry"]], how="intersection")
    ov["a"] = ov.area
    ov["w"] = ov.a / ov.groupby("uid").a.transform("sum")
    return ov[["uid", "i", "j", "w"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    cpath = Path(a.config).resolve()
    c = json.loads(cpath.read_text())
    rel = lambda p: (cpath.parent / p).resolve()
    out = rel(c["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    cache = rel(c["cache_dir"])
    cache.mkdir(parents=True, exist_ok=True)
    units = gpd.read_file(rel(c["units_geojson"]))
    units["uid"] = units[c.get("unit_id_field", "id")]
    ids = sorted(units.uid)
    utm = units.to_crs(units.estimate_utm_crs())
    lonlat = units.to_crs(4326)
    b = tuple(lonlat.total_bounds + np.array([-0.05, -0.05, 0.05, 0.05]))
    today = dt.datetime.now(dt.timezone.utc).date()
    start = dt.date.fromisoformat(c.get("start", "2024-10-01"))

    # 1. gridMET
    ds = xr.concat([gridmet_year(y, b, cache, c.get("refresh_hours", 6), today)
                    for y in range(start.year, today.year + 1)], "time")
    ds = ds.sel(time=slice(str(start), None))
    pet = ds.pet.transpose("time", "lat", "lon").values
    W = weights(ds.lat.values, ds.lon.values, utm)
    G = pd.DataFrame(index=pd.DatetimeIndex(ds.time.values).normalize(), columns=ids, dtype=float)
    for uid, grp in W.groupby("uid"):
        v = pet[:, grp.i.values, grp.j.values]
        ok = np.isfinite(v)
        G[uid] = np.where(ok.any(1), (np.where(ok, v, 0) * grp.w.values).sum(1) / (ok * grp.w.values).sum(1).clip(1e-12), np.nan)
    G = G.dropna(how="all")
    log(f"gridMET {G.index[0].date()} .. {G.index[-1].date()}, basin mean {np.nanmean(G.values):.2f} mm/d")

    # 2. Open-Meteo at the unit centroids
    om = c.get("openmeteo", {})
    cen = lonlat.set_index("uid").geometry.to_crs(utm.crs).centroid.to_crs(4326)
    r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=60, params={
        "latitude": ",".join(f"{cen[u].y:.4f}" for u in ids), "longitude": ",".join(f"{cen[u].x:.4f}" for u in ids),
        "daily": "et0_fao_evapotranspiration", "timezone": "America/New_York",
        "past_days": om.get("past_days", 10), "forecast_days": om.get("forecast_days", 10)})
    r.raise_for_status()
    js = r.json()
    js = js if isinstance(js, list) else [js]
    O = pd.DataFrame({u: pd.Series(j["daily"]["et0_fao_evapotranspiration"], index=pd.DatetimeIndex(j["daily"]["time"]),
                                   dtype=float) for u, j in zip(ids, js)})
    ov = O.index.intersection(G.index)
    if len(ov):
        ratio = np.nanmean(G.loc[ov].values) / max(1e-9, np.nanmean(O.loc[ov].values))
        log(f"overlap {len(ov)} d: gridMET / Open-Meteo = {ratio:.2f}")
    O = O[O.index > G.index[-1]] * om.get("scale", 1.0)
    E = pd.concat([G, O]).sort_index()
    E = E.interpolate(limit_direction="both")
    log(f"Open-Meteo {O.index[0].date()} .. {O.index[-1].date()}" if len(O) else "no Open-Meteo days")

    t = np.array([serial_day(x) for x in E.index], dtype=float) + 19 / 24
    for u in ids:
        f = out / c.get("file_pattern", "et_{id}.csv").format(id=u)
        tmp = f.with_suffix(".tmp")
        np.savetxt(tmp, np.column_stack([t, E[u].values / 1000.0]), fmt=["%.5f", "%.6e"], delimiter=",")
        tmp.replace(f)
    log(f"wrote {len(ids)} files to {out} ({len(E)} days, to {E.index[-1].date()})")


if __name__ == "__main__":
    sys.exit(main())
