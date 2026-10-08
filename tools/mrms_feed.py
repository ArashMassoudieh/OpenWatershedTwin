#!/usr/bin/env python3
"""Rainfall feed for a watershed twin: NOAA MRMS radar/gauge QPE for the recent past, Open-Meteo forecast after it.

Writes one OpenHydroQual precipitation file per unit (sub-catchment): rows `start,end,depth_m` (OHQ day serials,
UTC), hourly, contiguous, covering [now - window_days, now + forecast_days]. The twin reads them through its
forcing map (provider "csv"), so a single file covers both its Advance (observed) and Forecast stages.

Sources, by priority for each hour:
  1. MRMS MultiSensor_QPE_01H_Pass2 (radar + gauges; about 1-2 h behind real time),
  2. MRMS MultiSensor_QPE_01H_Pass1 (radar + early gauges; about 20 min behind) for hours Pass2 does not have yet;
     re-fetched as Pass2 when it appears,
  3. Open-Meteo forecast (hourly precipitation at each unit's points) after the last MRMS hour.
MRMS files come from the NOAA open-data bucket on AWS (noaa-mrms-pds, CONUS, from November 2020); only the window
around the watershed is kept, cached per hour as .npz. A QPE_01H file valid at HH:00 is the accumulation over the
hour ENDING then.

Areal averaging: weights of the 0.01 deg MRMS cells overlapping each unit polygon (area fractions, computed in a
projected CRS), built once and stored as JSON next to the config. Missing cells are left out and the weights of
the rest renormalised; an hour with no valid cell over a unit is treated as missing.

Bias factors (scale): `mrms_scale` and `openmeteo.scale` multiply the depths, e.g. to match the rainfall the model
was calibrated with (Rock Creek: AORC is about 15 % wetter than MRMS, so mrms_scale = 1.15).

Usage:
  mrms_feed.py --config watersheds/RockCreek/forcing/mrms_feed.json             # live update
  mrms_feed.py --config ... --backfill 2020-11-01 2025-10-01 --no-forecast        # history (cache + files)
Config (paths relative to the config file):
  { "units_geojson": "../gis/subcatchments.geojson", "unit_id_field": "id",
    "output_dir": "../deployments/live/forcing", "file_pattern": "rain_{id}.csv",
    "cache_dir": "../../../cache/mrms", "weights_file": "mrms_weights.json",
    "window_days": 30, "mrms_scale": 1.15,
    "openmeteo": {"forecast_days": 10, "scale": 1.0} }
"""
import argparse
import datetime as dt
import gzip
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import requests

BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
PRODUCTS = {"pass2": "MultiSensor_QPE_01H_Pass2_00.00", "pass1": "MultiSensor_QPE_01H_Pass1_00.00"}
CELL = 0.01                                          # MRMS CONUS grid spacing (deg)
OHQ_EPOCH = dt.datetime(1899, 12, 30, tzinfo=dt.timezone.utc)
UTC = dt.timezone.utc


def serial(t):
    return (t - OHQ_EPOCH).total_seconds() / 86400.0


def log(*a):
    print(f"[mrms_feed {dt.datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z]", *a, flush=True)


# ---------------------------------------------------------------------------------------------- configuration
class Config:
    def __init__(self, path):
        self.path = Path(path).resolve()
        c = json.loads(self.path.read_text())
        base = self.path.parent
        self.units_geojson = (base / c["units_geojson"]).resolve()
        self.unit_id_field = c.get("unit_id_field", "id")
        self.output_dir = (base / c["output_dir"]).resolve()
        self.file_pattern = c.get("file_pattern", "rain_{id}.csv")
        self.cache_dir = (base / c.get("cache_dir", "mrms_cache")).resolve()
        self.weights_file = (base / c.get("weights_file", "mrms_weights.json")).resolve()
        self.window_days = float(c.get("window_days", 30))
        self.mrms_scale = float(c.get("mrms_scale", 1.0))
        om = c.get("openmeteo", {})
        self.om_forecast_days = int(om.get("forecast_days", 10))
        self.om_scale = float(om.get("scale", 1.0))
        self.om_models = om.get("models")             # e.g. "best_match"; None = API default
        self.margin = float(c.get("window_margin_deg", 0.03))


# ---------------------------------------------------------------------------------------------- weights
def build_weights(cfg):
    """Area fractions of the MRMS cells over each unit polygon, on a window grid around the watershed."""
    import geopandas as gpd
    from shapely.geometry import box

    units = gpd.read_file(cfg.units_geojson).to_crs(4326)
    minx, miny, maxx, maxy = units.total_bounds
    # window aligned with the MRMS grid (cell edges at -130 + k*0.01 lon, 55 - k*0.01 lat)
    col0 = int(np.floor((minx - cfg.margin + 130.0) / CELL))
    col1 = int(np.ceil((maxx + cfg.margin + 130.0) / CELL))
    row0 = int(np.floor((55.0 - (maxy + cfg.margin)) / CELL))
    row1 = int(np.ceil((55.0 - (miny - cfg.margin)) / CELL))
    cells = []
    for r in range(row0, row1):
        for c in range(col0, col1):
            lon0, lat1 = -130.0 + c * CELL, 55.0 - r * CELL
            cells.append((r - row0, c - col0, box(lon0, lat1 - CELL, lon0 + CELL, lat1)))
    grid = gpd.GeoDataFrame({"r": [x[0] for x in cells], "c": [x[1] for x in cells]},
                            geometry=[x[2] for x in cells], crs=4326)
    utm = units.estimate_utm_crs()
    gu, uu = grid.to_crs(utm), units.to_crs(utm)
    weights = {}
    for _, u in uu.iterrows():
        cand = gu[gu.intersects(u.geometry)]
        a = cand.geometry.intersection(u.geometry).area
        w = a / a.sum()
        uid = str(u[cfg.unit_id_field])
        weights[uid] = [[int(r), int(c), round(float(x), 6)] for r, c, x in zip(cand.r, cand.c, w) if x > 1e-6]
    out = {"row0": row0, "col0": col0, "nrows": row1 - row0, "ncols": col1 - col0, "cell_deg": CELL,
           "note": "MRMS CONUS 0.01 deg grid; window origin row0/col0 from 55N/130W; weights = area fractions",
           "units": weights}
    cfg.weights_file.write_text(json.dumps(out))
    log(f"weights: {len(weights)} units, window {out['nrows']} x {out['ncols']} cells -> {cfg.weights_file}")
    return out


def load_weights(cfg):
    if cfg.weights_file.exists():
        return json.loads(cfg.weights_file.read_text())
    return build_weights(cfg)


# ---------------------------------------------------------------------------------------------- MRMS
def mrms_url(product, t):
    p = PRODUCTS[product]
    return f"{BUCKET}/CONUS/{p}/{t:%Y%m%d}/MRMS_{p}_{t:%Y%m%d-%H}0000.grib2.gz"


def fetch_window(product, t, W, session):
    """Window of the QPE_01H field valid at t (mm), or None if the file does not exist (yet)."""
    import rasterio
    from rasterio.windows import Window

    r = session.get(mrms_url(product, t), timeout=60)
    if r.status_code in (403, 404):
        return None
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".grib2") as tf:
        tf.write(gzip.decompress(r.content))
        tf.flush()
        with rasterio.open(tf.name) as src:
            a = src.read(1, window=Window(W["col0"], W["row0"], W["ncols"], W["nrows"])).astype("float32")
    a[a < 0] = np.nan                                  # -1 / -3: no coverage
    return a


def cache_path(cfg, t):
    return cfg.cache_dir / f"{t:%Y%m}" / f"{t:%Y%m%d-%H}.npz"


def get_hour(cfg, t, W, session, now):
    """(array mm, product) for the hour ending at t, from the cache or the bucket; Pass1 entries younger than a
    day are retried as Pass2."""
    f = cache_path(cfg, t)
    if f.exists():
        z = np.load(f)
        prod = str(z["product"])
        if prod == "pass2" or (now - t) > dt.timedelta(days=1):
            return z["mm"], prod
    for prod in ("pass2", "pass1"):
        try:
            a = fetch_window(prod, t, W, session)
        except Exception as e:                         # network trouble: try the other product / next run
            log(f"warning: {prod} {t:%Y-%m-%d %H}Z: {e}")
            a = None
        if a is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(f, mm=a, product=prod)
            return a, prod
    if f.exists():                                     # keep a cached Pass1 if Pass2 is still missing
        z = np.load(f)
        return z["mm"], str(z["product"])
    return None, None


def unit_depths(a, W):
    """Areal-average depth (mm) per unit for one hourly field; NaN where no valid cell."""
    out = {}
    for uid, cells in W["units"].items():
        v = np.array([a[r, c] for r, c, _ in cells])
        w = np.array([x for _, _, x in cells])
        ok = np.isfinite(v)
        out[uid] = float(np.sum(v[ok] * w[ok]) / np.sum(w[ok])) if ok.any() else np.nan
    return out


# ---------------------------------------------------------------------------------------------- Open-Meteo
def openmeteo_forecast(cfg, session):
    """Hourly precipitation (mm in the hour ending at the time stamp) at each unit's representative point."""
    import geopandas as gpd

    units = gpd.read_file(cfg.units_geojson).to_crs(4326)
    pts = units.geometry.representative_point()
    ids = [str(x) for x in units[cfg.unit_id_field]]
    res = {}
    for i in range(0, len(ids), 50):
        q = {"latitude": ",".join(f"{p.y:.4f}" for p in pts[i:i + 50]),
             "longitude": ",".join(f"{p.x:.4f}" for p in pts[i:i + 50]),
             "hourly": "precipitation", "past_days": 2, "forecast_days": cfg.om_forecast_days, "timezone": "GMT"}
        if cfg.om_models:
            q["models"] = cfg.om_models
        r = session.get("https://api.open-meteo.com/v1/forecast", params=q, timeout=60)
        r.raise_for_status()
        js = r.json()
        js = js if isinstance(js, list) else [js]
        for uid, j in zip(ids[i:i + 50], js):
            h = j["hourly"]
            res[uid] = {dt.datetime.fromisoformat(t).replace(tzinfo=UTC): (v if v is not None else 0.0)
                        for t, v in zip(h["time"], h["precipitation"])}
    return res


# ---------------------------------------------------------------------------------------------- main
def hours(t0, t1):
    t = t0
    while t <= t1:
        yield t
        t += dt.timedelta(hours=1)


def run(cfg, t_from, t_to, forecast):
    W = load_weights(cfg)
    s = requests.Session()
    now = dt.datetime.now(UTC)
    ids = list(W["units"])
    series = {u: [] for u in ids}                      # (hour end, depth mm, source)
    last_mrms, n_p2, n_p1, n_miss = None, 0, 0, 0
    for t in hours(t_from, t_to):
        a, prod = get_hour(cfg, t, W, s, now)
        if a is None:
            n_miss += 1
            continue
        d = unit_depths(a, W)
        for u in ids:
            if np.isfinite(d[u]):
                series[u].append((t, d[u] * cfg.mrms_scale, prod))
        last_mrms = t
        n_p2 += prod == "pass2"
        n_p1 += prod == "pass1"
    log(f"MRMS {t_from:%Y-%m-%d %H}Z .. {t_to:%Y-%m-%d %H}Z: pass2 {n_p2} h, pass1 {n_p1} h, missing {n_miss} h; "
        f"last {last_mrms}")
    if last_mrms is None:
        log("error: no MRMS data in the window")
        return 2
    # hours inside the MRMS period that are missing everywhere stay gaps (= no rain in OHQ); report them
    om_from = last_mrms + dt.timedelta(hours=1)
    n_om = 0
    if forecast:
        om = openmeteo_forecast(cfg, s)
        for u in ids:
            for t, v in sorted(om[u].items()):
                if t >= om_from:
                    series[u].append((t, max(0.0, v) * cfg.om_scale, "openmeteo"))
        n_om = sum(1 for t in om[ids[0]] if t >= om_from)
        log(f"Open-Meteo forecast from {om_from:%Y-%m-%d %H}Z: {n_om} h")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    for u in ids:
        rows = sorted(series[u])
        with open(cfg.output_dir / cfg.file_pattern.format(id=u), "w") as f:
            for t, v, _ in rows:
                f.write(f"{serial(t - dt.timedelta(hours=1)):.5f},{serial(t):.5f},{v / 1000.0:.6e}\n")
    status = {"updated_utc": now.isoformat(timespec="seconds"), "mrms_from_utc": t_from.isoformat(),
              "mrms_last_hour_end_utc": last_mrms.isoformat(), "pass2_hours": n_p2, "pass1_hours": n_p1,
              "missing_hours": n_miss, "forecast_from_utc": om_from.isoformat() if forecast else None,
              "forecast_hours": n_om, "mrms_scale": cfg.mrms_scale, "openmeteo_scale": cfg.om_scale,
              "units": len(ids)}
    (cfg.output_dir / "rain_status.json").write_text(json.dumps(status, indent=1))
    log(f"wrote {len(ids)} files to {cfg.output_dir}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"), help="UTC dates YYYY-MM-DD (hours ending in)")
    ap.add_argument("--no-forecast", action="store_true", help="MRMS only (no Open-Meteo tail)")
    ap.add_argument("--rebuild-weights", action="store_true")
    a = ap.parse_args()
    cfg = Config(a.config)
    if a.rebuild_weights:
        build_weights(cfg)
    if a.backfill:
        t0 = dt.datetime.fromisoformat(a.backfill[0]).replace(tzinfo=UTC) + dt.timedelta(hours=1)
        t1 = dt.datetime.fromisoformat(a.backfill[1]).replace(tzinfo=UTC)
    else:
        t1 = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        t0 = t1 - dt.timedelta(days=cfg.window_days)
    sys.exit(run(cfg, t0, t1, forecast=not a.no_forecast))


if __name__ == "__main__":
    main()
