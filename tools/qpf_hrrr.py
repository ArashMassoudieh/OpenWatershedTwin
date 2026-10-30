#!/usr/bin/env python3
"""HRRR rain forecast (QPF) per twin unit, for the short-range ensemble (tools/shortrange_ensemble.py).

At an issue time T the tool takes the newest HRRR run (NOAA, 3 km, hourly runs) whose files were published by T,
and writes its forecast rain per unit as rows (start, end, depth_m; days since 1899-12-30 UTC), the format of the
radar rain files:
  - forecast hours 1-18 from the sub-hourly files (wrfsubhf, 15-min accumulations, every run);
  - hours 19-48 (to the horizon) from the hourly surface files (wrfsfcf, 1-h accumulations) of the newest
    synoptic run (00/06/12/18Z) published by T.
Only the APCP messages are downloaded, by byte range from the .idx files (~240 KB per 15-min field). The archive on
AWS (noaa-hrrr-bdp-pds) keeps each file's upload time (Last-Modified), so a replay of a past storm sees exactly the
forecasts that existed at each issue time; the live system uses the same rule with T = now.

Areal averaging: area fractions of the HRRR cells over each unit polygon (in the HRRR Lambert grid), built once and
stored next to the radar weights (forcing/hrrr_weights.json).

  qpf_hrrr.py weights <watershed_dir>
  qpf_hrrr.py fetch   <watershed_dir> --issue <utc> [--horizon-h 24]
Outputs: <cache>/<issue YYYYmmddTHHMMZ>/rain_<unit>.csv and meta.json (runs, files, availability). Unit depths of
each downloaded field are cached per run (<cache>/runs/<init>.json), so a run is downloaded once.
Config: sites.json "shortrange.qpf": {"cache_dir": "...", "scale": 1.0, "lagged_runs": 6} (time-lagged ensemble:
the newest lagged_runs runs, written to <issue>/run<k>/; members take them in turn).
"""
import argparse
import json
import tempfile
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
EPOCH = pd.Timestamp("1899-12-30", tz="UTC")
ser = lambda t: (pd.Timestamp(t) - EPOCH).total_seconds() / 86400.0
SUBH_H, LONG_H = 18, 48
S = requests.Session()


def log(*a):
    print("[qpf_hrrr]", *a, flush=True)


def url(init, kind, fh):
    return f"{BUCKET}/hrrr.{init:%Y%m%d}/conus/hrrr.t{init:%H}z.{kind}f{fh:02d}.grib2"


def published(u):
    """Upload time of a file (UTC), or None if it does not exist."""
    r = S.head(u, timeout=30)
    if r.status_code != 200:
        return None
    return pd.Timestamp(parsedate_to_datetime(r.headers["Last-Modified"]))


def apcp_ranges(u):
    """[(label, start, end)] of the APCP messages of a file, from its .idx."""
    r = S.get(u + ".idx", timeout=30)
    r.raise_for_status()
    lines = [l.split(":") for l in r.text.strip().splitlines()]
    out = []
    for i, l in enumerate(lines):
        if l[3] == "APCP":
            end = int(lines[i + 1][1]) - 1 if i + 1 < len(lines) else ""
            out.append((l[5], int(l[1]), end))
    return out


def read_message(u, a, b):
    import rasterio
    r = S.get(u, headers={"Range": f"bytes={a}-{b}"}, timeout=60)
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".grib2") as tf:
        tf.write(r.content); tf.flush()
        with rasterio.open(tf.name) as src:
            return src.read(1).astype("float64"), src.transform, src.crs


def interval(label, init, fh):
    """(start, end) of an accumulation label: '300-315 min acc fcst' or '5-6 hour acc fcst'."""
    a, unit = label.split(" ")[0], label.split(" ")[1]
    x0, x1 = (float(v) for v in a.split("-"))
    k = pd.Timedelta(minutes=1) if unit == "min" else pd.Timedelta(hours=1)
    return init + x0 * k, init + x1 * k


# ---------------------------------------------------------------------------------------------- weights
def cmd_weights(wdir, probe_init="2025-07-19T12:00Z"):
    import geopandas as gpd
    from shapely.geometry import box
    feed = json.loads((wdir / "forcing/mrms_feed.json").read_text())
    units = gpd.read_file(wdir / "forcing" / feed["units_geojson"])
    init = pd.Timestamp(probe_init)
    u = url(init, "wrfsfc", 1)
    lab, a, b = apcp_ranges(u)[-1]
    arr, tr, crs = read_message(u, a, b)
    uu = units.to_crs(crs)
    minx, miny, maxx, maxy = uu.total_bounds
    inv = ~tr
    cols, rows = zip(*[inv * p for p in ((minx, maxy), (maxx, miny))])
    c0, c1 = int(np.floor(min(cols))) - 1, int(np.ceil(max(cols))) + 1
    r0, r1 = int(np.floor(min(rows))) - 1, int(np.ceil(max(rows))) + 1
    cells = []
    for r in range(r0, r1):
        for c in range(c0, c1):
            x0, y0 = tr * (c, r)
            x1, y1 = tr * (c + 1, r + 1)
            cells.append((r, c, box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))))
    grid = gpd.GeoDataFrame({"r": [x[0] for x in cells], "c": [x[1] for x in cells]}, geometry=[x[2] for x in cells], crs=crs)
    W = {}
    for _, un in uu.iterrows():
        cand = grid[grid.intersects(un.geometry)]
        ar = cand.geometry.intersection(un.geometry).area
        w = ar / ar.sum()
        W[str(un[feed["unit_id_field"]])] = [[int(r), int(c), round(float(x), 6)] for r, c, x in zip(cand.r, cand.c, w) if x > 1e-6]
    out = dict(grid="HRRR CONUS 3 km Lambert (rows/cols of the full grid)", shape=list(arr.shape), units=W,
               note="area fractions of HRRR cells over each unit polygon; built by tools/qpf_hrrr.py weights")
    p = wdir / "forcing/hrrr_weights.json"
    p.write_text(json.dumps(out))
    log(f"weights: {len(W)} units, {sum(len(v) for v in W.values())} cell entries -> {p}")


def unit_depths(arr, W):
    return {u: float(sum(arr[r, c] * w for r, c, w in cells) / sum(w for _, _, w in cells)) for u, cells in W["units"].items()}


# ---------------------------------------------------------------------------------------------- fetch
def run_fields(cache, init, kind, hours, W, T):
    """{fh: [(start, end, {unit: mm})]} for the hours of a run published by T; cached per run."""
    p = cache / "runs" / f"{init:%Y%m%dT%H}_{kind}.json"
    c = json.loads(p.read_text()) if p.exists() else {}
    out = {}
    for fh in hours:
        k = str(fh)
        if k not in c:
            u = url(init, kind, fh)
            pub = published(u)
            if pub is None:
                continue
            fields = []
            labs = apcp_ranges(u)
            if kind == "wrfsfc":
                labs = [l for l in labs if l[0].startswith(f"{fh - 1}-{fh} hour")]
            for lab, a, b in labs:
                arr, _, _ = read_message(u, a, b)
                s, e = interval(lab, init, fh)
                fields.append([s.isoformat(), e.isoformat(), unit_depths(arr, W)])
            c[k] = dict(published=pub.isoformat(), fields=fields)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(c))
        if pd.Timestamp(c[k]["published"]) <= T:
            out[fh] = [(pd.Timestamp(s), pd.Timestamp(e), d) for s, e, d in c[k]["fields"]]
    return out


def newest_runs(cache, T, kind, hours, W, n=1, every_h=1, back_h=8):
    """The newest n runs (init, fields) all of whose `hours` were published by T, newest first."""
    out, t0 = [], T.floor("h")
    for k in range(back_h + 1):
        init = t0 - pd.Timedelta(hours=k)
        if init.hour % every_h:
            continue
        if published(url(init, kind, hours[-1])) is None and published(url(init, kind, hours[0])) is None:
            continue
        f = run_fields(cache, init, kind, hours, W, T)
        if len(f) == len(hours):
            out.append((init, f))
            if len(out) == n:
                break
    return out


def write_rows(od, rows, T, scale):
    od.mkdir(parents=True, exist_ok=True)
    tot = {}
    for u, rr in rows.items():
        rr = sorted(rr)
        with open(od / f"rain_{u}.csv", "w") as f:
            f.writelines(f"{ser(s):.7f},{ser(e):.7f},{max(v, 0.0) * scale / 1000.0:.6f}\n" for s, e, v in rr if e > T)
        tot[u] = round(sum(max(v, 0.0) * scale for s, e, v in rr if e > T), 2)
    return tot


def cmd_fetch(wdir, issue, horizon_h):
    """Time-lagged forecast: the newest `lagged_runs` sub-hourly runs published by T (run0 = newest), each extended
    to the horizon with the newest synoptic run's hourly rain. Folders <issue>/run<k>/; the issue folder itself
    holds run0 (single-run use)."""
    reg = json.loads((wdir / "sites.json").read_text())
    q = reg["shortrange"].get("qpf", {})
    cache = Path(q.get("cache_dir", wdir / "cache/qpf_hrrr")).expanduser()
    if not cache.is_absolute():
        cache = (wdir / cache).resolve()
    W = json.loads((wdir / "forcing/hrrr_weights.json").read_text())
    T = pd.Timestamp(issue)
    T = T.tz_localize("UTC") if T.tzinfo is None else T.tz_convert("UTC")
    t0 = time.time()
    n = int(q.get("lagged_runs", 1))
    scale = float(q.get("scale", 1.0))
    need_end = T + pd.Timedelta(hours=horizon_h)
    subs = newest_runs(cache, T, "wrfsubh", list(range(1, SUBH_H + 1)), W, n=n, back_h=n + 6)
    longs = newest_runs(cache, T, "wrfsfc", list(range(1, LONG_H + 1)), W, n=1, every_h=6, back_h=12)
    init_l, fl = longs[0] if longs else (None, {})
    od = cache / T.strftime("%Y%m%dT%H%MZ")
    meta = dict(issue_utc=T.isoformat(), hourly_run=None if init_l is None else init_l.isoformat(), scale=scale, runs=[])
    for k, (init_s, fs) in enumerate(subs):
        rows = {u: [] for u in W["units"]}
        end_s = None
        for fh in sorted(fs):
            for s, e, d in fs[fh]:
                for u, v in d.items():
                    rows[u].append((s, e, v))
                end_s = e if end_s is None or e > end_s else end_s
        for fh in sorted(fl):
            for s, e, d in fl[fh]:
                if (end_s is None or s >= end_s) and s < need_end:
                    for u, v in d.items():
                        rows[u].append((s, e, v))
        tot = write_rows(od / f"run{k}", rows, T, scale)
        if k == 0:
            write_rows(od, rows, T, scale)
        meta["runs"].append(dict(k=k, subhourly_run=init_s.isoformat(), mean_total_mm=round(float(np.mean(list(tot.values()))), 2),
                                 max_total_mm=max(tot.values())))
    meta["seconds"] = round(time.time() - t0, 1)
    od.mkdir(parents=True, exist_ok=True)
    (od / "meta.json").write_text(json.dumps(meta, indent=1))
    log(f"{T:%Y-%m-%d %H:%M}Z: " + "; ".join(f"run{r['k']} {r['subhourly_run'][11:13]}Z {r['mean_total_mm']} mm" for r in meta["runs"])
        + f" (hourly {meta['hourly_run']}, {meta['seconds']} s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["weights", "fetch"])
    ap.add_argument("watershed_dir", type=Path)
    ap.add_argument("--issue", help="issue time (UTC); default now")
    ap.add_argument("--horizon-h", type=float, default=24)
    a = ap.parse_args()
    wdir = a.watershed_dir.resolve()
    if a.cmd == "weights":
        cmd_weights(wdir)
    else:
        cmd_fetch(wdir, a.issue or pd.Timestamp.now(tz="UTC"), a.horizon_h)


if __name__ == "__main__":
    main()
