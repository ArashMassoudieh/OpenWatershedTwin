#!/usr/bin/env python3
"""Data for a flood-alert demo page: a replay of one storm through the forecast-triggered flood-map chain.

From a replay folder of tools/shortrange_ensemble.py (cycles in <out_dir>/<T_obs>/members.npz, run with the
forecast rain each cycle had), this tool re-evaluates the trigger at every cycle (tools/flood_trigger.py, no job
submission) and collects, per cycle: the alert state, the forecast band at the trigger reach, the percentile
members' peaks, and the library maps; plus the observed gauge flow, the observed basin rain, the forecast rain of
each lagged HRRR run (tools/qpf_hrrr.py), and the 2D maps of the flood-service jobs the replay submitted. A 2D map
is shown from the time its computation would have finished: the job's cycle time plus the members' model
minutes (as measured, in submission order).

  flood_demo.py <replay_dir> --site sligo_maple --jobs <id> [<id> ...] --obs <usgs csv> --out <dir>
               [--window 2025-07-19T06:00Z 2025-07-20T04:00Z] [--title "..."]
Writes <out>/data.json and <out>/maps/*.png; the page is web/demo/flood_demo.html (copied to <out>/index.html).
"""
import argparse
import json
import shutil
import sys
import tempfile
import types
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import flood_trigger as ft          # noqa: E402
import shortrange_ensemble as se    # noqa: E402

CFS = 0.0283168466
iso = lambda t: pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def basin_rain(sr, units, areas, T, t0, t1, lag=None, qpf_dir=None):
    """Basin-mean rain (mm per 15 min) on a 15-min grid [t0, t1): observed (layered radar) or one lagged QPF run."""
    grid = np.arange(t0, t1 - 1e-9, 1 / 96)
    tot = np.zeros(len(grid))
    w = np.array([areas[u] for u in units]) / sum(areas.values())
    for k, (u, wu) in enumerate(zip(units, w), start=1):
        if qpf_dir is None:
            rows, _ = se.rain_rows({**sr, "forcing": {**sr["forcing"], "forecast": None}}, u, k, T, t0 - 1, t1)
        else:
            p = qpf_dir / f"run{lag}" / f"rain_{u}.csv"
            rows = se.read_rows(p) if p.exists() else np.zeros((0, 3))
        for s, e, d in rows:
            if e <= t0 or s >= t1 or e <= s:
                continue
            # spread each bin's depth over the 15-min cells it covers
            for i in np.flatnonzero((grid < e) & (grid + 1 / 96 > s)):
                ov = min(e, grid[i] + 1 / 96) - max(s, grid[i])
                tot[i] += wu * d * 1000 * ov / (e - s)
    return grid, tot


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("replay_dir", type=Path)
    ap.add_argument("--site", required=True)
    ap.add_argument("--jobs", nargs="*", default=[])
    ap.add_argument("--obs", type=Path, required=True, help="USGS IV discharge csv (time,value cfs)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--window", nargs=2, default=None)
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    R = a.replay_dir.resolve()
    reg = json.loads((R / "sites.json").read_text())
    sr, site = reg["shortrange"], reg["sites"][a.site]
    out = a.out.resolve(); (out / "maps").mkdir(parents=True, exist_ok=True)
    cyc = sorted((R / sr["out_dir"]).glob("*/members.npz"))
    mem = pd.read_csv(R / sr["members_file"], comment="#")
    thr = float(site["trigger"]["threshold_cms"])

    # ---- re-evaluate the trigger at every cycle (fresh state, no jobs); library maps rendered in <R>/web
    work = Path(tempfile.mkdtemp(prefix="flood_demo_"))
    for d in ("deployments", "forcing"):
        (work / d).symlink_to(R / d)
    shutil.copy(R / "sites.json", work / "sites.json")
    args = types.SimpleNamespace(no_submit=True, wait=False)
    cycles, libs = [], set()
    for f in cyc:
        z = np.load(f)
        Q, t, links, members, t_obs = z["Q"], z["t"], [str(x) for x in z["links"]], [int(x) for x in z["members"]], float(z["t_obs"])
        st = ft.run_site(a.site, dict(site), work, R / "web", Q, t, t_obs, links, mem, members, args,
                         z["lag_run"] if "lag_run" in z.files else None)
        al = json.loads((R / "web/alerts" / f"{a.site}.json").read_text())
        maps = {}
        for m, v in al["maps"].items():
            if v.get("png"):
                libs.add(v["png"])
                maps[m] = dict(source="library", stage_ft=v["stage_ft"], library_stage_ft=v.get("library_stage_ft"),
                               png="maps/" + Path(v["png"]).name, impacts=v.get("impacts"))
        b = al["forecast"]["band"]
        cycles.append(dict(cycle_utc=iso(se.from_ser(float(z["cycle"]))), data_utc=al["issued_utc"][:19] + "Z", state=st,
                           watch_reason=al.get("watch_reason"),
                           statistic_cfs=round(al["statistic_cms"] / CFS), peak_cfs={m: round(v / CFS) for m, v in al["forecast"]["peak_cms"].items()},
                           peak_time_utc=al["forecast"]["peak_time_utc"], maps=maps,
                           band=dict(t=b["t_utc"], **{k: [round(x / CFS) for x in b[k]] for k in b if k != "t_utc"})))
        print(f"{cycles[-1]['cycle_utc']}: {st}", flush=True)
    for p in libs:
        shutil.copy(R / "web" / p, out / "maps" / Path(p).name)

    # ---- 2D jobs: maps from the service, ready at cycle time + model minutes
    jobs = []
    url = site["service"]["url"].rstrip("/")
    for jid in a.jobs:
        stt = json.loads(urllib.request.urlopen(f"{url}/jobs/{jid}", timeout=30).read())
        req_issue = pd.Timestamp(stt.get("issued_utc"))
        cyc_t = next((c["cycle_utc"] for c in cycles if pd.Timestamp(c["data_utc"]) == req_issue), iso(req_issue))
        ready = pd.Timestamp(cyc_t)
        mm = {}
        for m, prods in stt.get("products", {}).items():
            png = f"maps/{jid}_{m}.png"
            (out / png).write_bytes(urllib.request.urlopen(f"{url}/jobs/{jid}/files/{prods['max_depth_png']}", timeout=60).read())
            imp = json.loads(urllib.request.urlopen(f"{url}/jobs/{jid}/files/{prods['impacts']}", timeout=60).read())
            ready = ready + pd.Timedelta(minutes=float(imp.get("model_minutes", 0)))
            mm[m] = dict(png=png, impacts=imp, ready_utc=iso(ready), stage_ft=imp.get("gauge", {}).get("peak_stage_ft"))
        jobs.append(dict(id=jid, state=stt.get("state"), data_utc=iso(req_issue), cycle_utc=cyc_t, members=mm))
        print(f"job {jid}: {stt.get('state')}, members {list(mm)}", flush=True)

    # ---- observations and rain on the page window
    w0, w1 = (pd.Timestamp(x) for x in (a.window or (cycles[0]["cycle_utc"], cycles[-1]["band"]["t"][-1])))
    o = pd.read_csv(a.obs, usecols=["time", "value"], parse_dates=["time"]).dropna().set_index("time").value
    o = o[w0 - pd.Timedelta(hours=1):w1]                       # the gauge's own 5-min values (crossing and peak times)
    feed = json.loads((R / "forcing/mrms_feed.json").read_text())
    import geopandas as gpd
    g = gpd.read_file(R / "forcing" / feed["units_geojson"])
    areas = dict(zip(g[feed["unit_id_field"]].astype(str), g.area_km2))
    units = [f"SC{n:02d}" for n in range(1, sr.get("n_units", 27) + 1)]
    t0, t1 = se.ser(w0), se.ser(w1)
    grid, obs_rain = basin_rain(sr, units, areas, t1 + 1, t0, t1)
    fc = sr["forcing"].get("forecast") or {}
    nlag = int(fc.get("lagged", 1))
    for c in cycles:
        qd = Path(fc["dir"].format(issue=pd.Timestamp(c["cycle_utc"]).strftime("%Y%m%dT%H%MZ"))) if fc else None
        runs = []
        if qd and qd.exists():
            meta = json.loads((qd / "meta.json").read_text())
            for r in meta.get("runs", []):
                _, v = basin_rain(sr, units, areas, None, t0, t1, lag=r["k"], qpf_dir=qd)
                runs.append(dict(run=r["subhourly_run"][:13] + "Z", mm15=[round(x, 3) for x in v], total_mm=round(float(v.sum()), 1)))
        c["qpf_runs"] = runs
    data = dict(title=a.title, site=a.site, label=site["label"], trigger_label=site["trigger"].get("label"),
                threshold_cfs=round(thr / CFS), statistic=site["trigger"].get("statistic", "p50"),
                map_on=site["trigger"].get("map_on", ["triggered"]), viewer=site.get("viewer"),
                window=[iso(w0), iso(w1)], rain_t=[iso(se.from_ser(x)) for x in grid], rain_obs_mm15=[round(x, 3) for x in obs_rain],
                obs=dict(t=[iso(x) for x in o.index], cfs=[None if not np.isfinite(v) else round(float(v)) for v in o.values]),
                cycles=cycles, jobs=jobs, built_utc=iso(pd.Timestamp.now(tz="UTC")))
    (out / "data.json").write_text(json.dumps(data))
    page = TOOLS.parent / "web/demo/flood_demo.html"
    if page.exists():
        shutil.copy(page, out / "index.html")
    shutil.rmtree(work, ignore_errors=True)
    print(f"{len(cycles)} cycles, {len(jobs)} jobs, {len(libs)} library maps -> {out}")


if __name__ == "__main__":
    main()
