#!/usr/bin/env python3
"""Flood trigger of a watershed twin (docs/flood_service_design.md): run after each short-range cycle.

For every site of the registry (watersheds/<W>/sites.json "sites"):
  1. reads the latest member flows of tools/shortrange_ensemble.py (<out_dir>/latest.json -> members.npz);
  2. adds one AR(1) structural-error series per member, eps(t) = phi eps(t-dt) + s_Q sqrt(1 - phi^2) eta,
     phi = exp(-dt / tau_Q) (the member's own s_Q, tau_Q from the members file), from the residual observed at the
     gauge at the issue time (log obs - log member; 0 without an observation), applied to every link as Q exp(eps);
  3. ranks the members by forecast peak at the trigger link; the site's members (e.g. 50, 90, 95) are the members
     at those percentiles of the peak (never the pointwise envelope); the statistic (trigger.statistic, default p50)
     is that percentile of the peaks;
  4. state: "triggered" when the statistic reaches the threshold, "watch" when the highest mapped member does,
     else "none"; "mapping" / "mapped" while / after the flood service works on a job. trigger.map_on lists the
     states that start a 2D job (default ["triggered"]; ["watch", "triggered"] maps a watch hours ahead, so the
     maps are ready before a flash flood: at ~3 instance-hours a job, false alarms are cheap);
  5. maps at once from the pre-built library (the first level at or above each member's peak stage, the site's USGS
     rating), and, when triggered, a job to the flood service (POST /jobs) with the members' link flows; a new job
     only if a member's peak changed by more than repeat.min_change or the last job is older than repeat.max_age_h;
  6. follows the job (GET /jobs/<id>) on later runs (or with --wait) and copies its maps and impacts to the web
     folder, where they replace the library maps.
Writes <web>/alerts/<site>.json (the viewer's popup), <web>/alerts/index.json (active alerts) and the files under
<web>/alerts/<site>/<issued>/; the trigger's own memory is <watershed>/state/alerts/<site>.json.

  flood_trigger.py <watershed_dir> --web <web outputs dir> [--members <members.npz>] [--no-submit] [--wait]
  flood_trigger.py <watershed_dir> --web <dir> --library-png <service sites dir>   # render the library maps once
"""
import argparse
import json
import math
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

EPOCH = pd.Timestamp("1899-12-30", tz="UTC")
from_ser = lambda d: (EPOCH + pd.to_timedelta(d, unit="D")).round("s")
ser = lambda t: (pd.Timestamp(t) - EPOCH).total_seconds() / 86400.0
CFS, FT = 0.0283168466, 0.3048
SEED = 20261024
BAND = (5, 25, 50, 75, 95)


# ---------------------------------------------------------------------------------------------- inputs
def observed(site, t_obs):
    """Observed gauge flow (m3/s) at t_obs (the last value within 2 h before it), or None.
    error_model.obs: {"file": ..., "format": "ohq"} (usgs_feed.py: t,value in m3/s) or "usgs_csv" (time,value cfs)."""
    o = site.get("error_model", {}).get("obs")
    if not o or not Path(o["file"]).exists():
        return None
    if o.get("format", "ohq") == "usgs_csv":
        d = pd.read_csv(o["file"], usecols=["time", "value"], parse_dates=["time"]).dropna()
        t, v = d.time.map(ser).to_numpy(), d.value.to_numpy() * CFS
    else:
        d = pd.read_csv(o["file"], header=None, names=["t", "v"], comment="#").apply(pd.to_numeric, errors="coerce").dropna()
        t, v = d.t.to_numpy(), d.v.to_numpy()
    k = np.flatnonzero((t <= t_obs + 1e-6) & (t >= t_obs - 2 / 24) & (v > 0))
    return float(v[k[-1]]) if len(k) else None


def rating(path):
    r = pd.read_csv(path, sep="\t", comment="#")
    r = r[pd.to_numeric(r.INDEP, errors="coerce").notna()]
    return r.INDEP.astype(float).to_numpy(), r.DEP.astype(float).to_numpy()


def with_error(Q, t, t_obs, links, site, mem, members):
    """Q[row, time, link] with each member's AR(1) error, started at t_obs from the observed residual (row m is
    member members[m] of the members file)."""
    gi = links.index(site.get("error_model", {}).get("gauge_link", site["trigger"]["link"]))
    i0 = int(np.argmin(abs(t - t_obs)))
    q_obs = observed(site, t_obs)
    rng = np.random.default_rng(SEED + int(round(t_obs * 96)))
    dt = float(t[1] - t[0])
    out = Q.copy()
    for m, k in enumerate(members):
        sq, tq = float(mem.at[k, "s_Q"]), float(mem.at[k, "tau_Q"])
        phi = math.exp(-dt / tq)
        e = np.zeros(len(t))
        qs = Q[m, i0, gi]
        e[i0] = math.log(q_obs / qs) if q_obs and np.isfinite(qs) and qs > 0 else 0.0
        eta = rng.standard_normal(len(t))
        for i in range(i0 + 1, len(t)):
            e[i] = phi * e[i - 1] + sq * math.sqrt(1 - phi ** 2) * eta[i]
        out[m, i0:] = Q[m, i0:] * np.exp(e[i0:])[:, None]
    return out, q_obs


# ---------------------------------------------------------------------------------------------- service
def call(site, method, path, body=None):
    sv = site["service"]
    req = urllib.request.Request(sv["url"].rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    tok = os.environ.get(sv.get("token_env", ""), "")
    if tok:
        req.add_header("X-Floodmap-Token", tok)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if path.split("?")[0].count("/files/") else json.loads(data)


def collect(site, job, dest: Path, web: Path):
    """Copy a finished job's maps and impacts to dest; {member: map entry}."""
    st = call(site, "GET", f"/jobs/{job['id']}")
    job.update(state=st.get("state"), progress=st.get("progress"), message=st.get("message"))
    maps = {}
    if st.get("state") != "done":
        return maps
    dest.mkdir(parents=True, exist_ok=True)
    for m, prods in st.get("products", {}).items():
        png = dest / f"{m}_max_depth.png"
        png.write_bytes(call(site, "GET", f"/jobs/{job['id']}/files/{prods['max_depth_png']}"))
        imp = json.loads(call(site, "GET", f"/jobs/{job['id']}/files/{prods['impacts']}"))
        (dest / f"{m}_impacts.json").write_text(json.dumps(imp, indent=1))
        maps[m] = dict(source="2d", png=str(png.relative_to(web)), job=job["id"], issued_utc=job["issued_utc"],
                       stage_ft=imp.get("gauge", {}).get("peak_stage_ft"), impacts=imp)
    return maps


# ---------------------------------------------------------------------------------------------- library
def library_maps(site, peaks_cms, web):
    lib = site.get("library")
    if not lib or not Path(lib["index"]).exists():
        return {}
    idx = pd.read_csv(lib["index"])
    h, q = rating(lib["rating"])
    out = {}
    for m, pk in peaks_cms.items():
        stage = float(np.interp(pk / CFS, q, h))
        hit = idx[idx.stage_ft >= stage - 1e-9]
        if stage < idx.stage_ft.min() - 1e-9:
            out[m] = dict(source="library", stage_ft=round(stage, 2), png=None, note="below the lowest library map")
            continue
        r = (hit.iloc[0] if len(hit) else idx.iloc[-1])
        pdir = web / "alerts" / site["_name"] / "library"
        png = pdir / (Path(r.file).stem + ".png")
        imp = {k: (v.item() if hasattr(v, "item") else v) for k, v in r.items() if k not in ("file", "frame", "stage_ft", "inflow_cfs")}
        out[m] = dict(source="library", stage_ft=round(stage, 2), library_stage_ft=float(r.stage_ft),
                      png=str(png.relative_to(web)) if png.exists() else None, impacts=imp)
    return out


def render_library(reg, wdir, web, sites_dir):
    """PNG of every library map (floodlib.render_map, the service's look) into <web>/alerts/<site>/library/."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/floodmap/pipeline"))
    import floodlib as fl
    import rasterio
    for name, site in reg["sites"].items():
        lib = site.get("library")
        if not lib or not Path(lib["index"]).exists():
            print(f"{name}: no library index"); continue
        svc = fl.load_site(Path(sites_dir), site["service"]["site"])
        pdir = web / "alerts" / name / "library"; pdir.mkdir(parents=True, exist_ok=True)
        idx = pd.read_csv(lib["index"])
        dem = Path(lib.get("dem", svc["_root"] / svc["dem"]))
        z = rasterio.open(dem).read(1)
        for _, r in idx.iterrows():
            with rasterio.open(Path(lib["index"]).parent / r.file) as s:
                h, tr, crs = s.read(1), s.transform, s.crs
            ext = (tr.c, tr.c + h.shape[1] * tr.a, tr.f + h.shape[0] * tr.e, tr.f)
            assets = fl.impact_assets(svc, tr, h.shape, crs, ext)
            fl.render_map(pdir / (Path(r.file).stem + ".png"), z, h, tr, ext, assets, site["label"],
                          f"library map: gauge stage {r.stage_ft:.1f} ft ({r.inflow_cfs:,.0f} cfs)",
                          svc["impacts"].get("wet_depth_m", 0.15))
            print(f"{name}: {r.file} -> png", flush=True)


# ---------------------------------------------------------------------------------------------- main
def run_site(name, site, wdir, web, Q, t, t_obs, links, mem, members, args):
    site["_name"] = name
    trig = site["trigger"]
    thr = float(trig["threshold_cms"])
    Qe, q_obs = with_error(Q, t, t_obs, links, site, mem, members)
    ti = links.index(trig["link"])
    fut = t >= t_obs - 1e-9
    g = Qe[:, :, ti]
    ok = np.isfinite(g[:, fut]).all(axis=1)
    peaks = np.where(ok, np.nanmax(np.where(fut, g, -np.inf), axis=1), np.nan)
    good = np.flatnonzero(ok)
    order = good[np.argsort(peaks[good])]
    pct = lambda p: int(order[min(int(np.ceil(p / 100 * len(order))) - 1, len(order) - 1)])
    pick = {f"p{p}": pct(p) for p in site.get("members", [50, 90, 95])}
    stat_name = trig.get("statistic", "p50")
    stat = float(np.percentile(peaks[good], float(stat_name[1:])))
    top = max(float(peaks[k]) for k in pick.values())
    issued = from_ser(t_obs)
    # times where every good member has flows (a fresh spin-up has no history before its start): JSON has no NaN
    have = np.isfinite(Qe[good]).all(axis=(0, 2))
    t, Qe, g, fut = t[have], Qe[:, have], g[:, have], fut[have]
    times_iso = [from_ser(x).strftime("%Y-%m-%dT%H:%M:%SZ") for x in t]
    state = "triggered" if stat >= thr else ("watch" if top >= thr else "none")
    band = {f"q{p}": [round(float(v), 3) for v in np.nanpercentile(g[good], p, axis=0)] for p in BAND}
    peak_cms = {m: round(float(peaks[k]), 2) for m, k in pick.items()}
    peak_t = {m: times_iso[int(np.nanargmax(np.where(fut, g[k], -np.inf)))] for m, k in pick.items()}

    # memory of the last job
    mpath = wdir / "state/alerts" / f"{name}.json"; mpath.parent.mkdir(parents=True, exist_ok=True)
    memo = json.loads(mpath.read_text()) if mpath.exists() else {}
    job = memo.get("job")
    rep = site.get("repeat", {})
    stamp = issued.strftime("%Y%m%dT%H%MZ")
    map_on = trig.get("map_on", ["triggered"])          # states that start a 2D job (e.g. ["watch", "triggered"])
    if state in map_on and args.no_submit:
        print(f"{name} {stamp}: {state}: a 2D job would be submitted now for "
              + ", ".join(m for m in pick if peak_cms[m] >= thr) + " (--no-submit)", flush=True)
    if state in map_on and not args.no_submit:
        age_h = (issued - pd.Timestamp(job["issued_utc"])).total_seconds() / 3600 if job else 1e9
        changed = (not job) or job.get("state") == "failed" or any(
            abs(peak_cms[m] / max(job["peaks_cms"].get(m, 1e-6), 1e-6) - 1) > rep.get("min_change", 0.1) for m in peak_cms)
        if changed or age_h > rep.get("max_age_h", 1.0):
            # map only members whose forecast peak reaches the threshold (at a watch the median is often far below it)
            send = {m: k for m, k in pick.items() if peak_cms[m] >= thr}
            body = dict(site=site["service"]["site"], issued_utc=issued.isoformat(), requester=f"flood_trigger {name}",
                        members={m: dict(t_utc=times_iso, links={ln: [round(float(v), 5) for v in Qe[k, :, j]]
                                                                  for j, ln in enumerate(links)}) for m, k in send.items()})
            r = call(site, "POST", "/jobs", body)
            job = dict(id=r["job_id"], issued_utc=issued.isoformat(), peaks_cms=peak_cms, state="queued", progress=0.0)
            print(f"{name}: job {job['id']} submitted", flush=True)
    maps = library_maps(site, peak_cms, web)
    while job and job.get("state") not in ("done", "failed", "collected"):
        got = collect(site, job, web / "alerts" / name / pd.Timestamp(job["issued_utc"]).strftime("%Y%m%dT%H%MZ"), web)
        if got:
            job["state"] = "collected"; job["maps"] = got
        if not args.wait or job.get("state") in ("failed", "collected"):
            break
        time.sleep(30)
    if job and job.get("maps"):
        age_h = (issued - pd.Timestamp(job["issued_utc"])).total_seconds() / 3600
        if age_h <= 3 * rep.get("max_age_h", 1.0):
            maps.update(job["maps"])
    if state in map_on and job:
        state = "mapped" if job.get("state") == "collected" else ("mapping" if job.get("state") != "failed" else state)
    memo["job"] = job
    mpath.write_text(json.dumps(memo, indent=1, default=str))

    alert = dict(site=name, label=site["label"], issued_utc=issued.isoformat(), state=state, statistic=stat_name,
                 statistic_cms=round(stat, 2), threshold_cms=thr, trigger_label=trig.get("label"),
                 observed_cms=None if q_obs is None else round(q_obs, 3),
                 forecast=dict(peak_cms=peak_cms, peak_time_utc=peak_t, members={m: members[k] for m, k in pick.items()},
                               band=dict(t_utc=times_iso, **band)),
                 maps=maps, job=None if not job else {k: job.get(k) for k in ("id", "state", "progress", "message", "issued_utc")},
                 demo_url=site.get("demo_url"), viewer=site.get("viewer"))
    ad = web / "alerts"; ad.mkdir(parents=True, exist_ok=True)
    tmp = ad / f"{name}.json.tmp"; tmp.write_text(json.dumps(alert, indent=1)); tmp.replace(ad / f"{name}.json")
    print(f"{name} {stamp}: {state}; {stat_name} peak {stat / CFS:,.0f} cfs vs {thr / CFS:,.0f}; "
          + ", ".join(f"{m} {v / CFS:,.0f}" for m, v in peak_cms.items())
          + (f"; obs {q_obs / CFS:,.0f} cfs" if q_obs else "; no obs"), flush=True)
    return state


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("watershed_dir", type=Path)
    ap.add_argument("--web", type=Path, required=True, help="the viewer's outputs folder (alerts/ is written there)")
    ap.add_argument("--members", type=Path, help="a members.npz (default: the short-range latest.json)")
    ap.add_argument("--no-submit", action="store_true", help="no flood-service jobs (library maps only)")
    ap.add_argument("--wait", action="store_true", help="wait for the job and collect its maps")
    ap.add_argument("--library-png", metavar="SERVICE_SITES_DIR", help="render the library maps and exit")
    a = ap.parse_args()
    wdir = a.watershed_dir.resolve()
    reg = json.loads((wdir / "sites.json").read_text())
    if a.library_png:
        return render_library(reg, wdir, a.web, a.library_png)
    sr = reg["shortrange"]
    mf = a.members or wdir / json.loads((wdir / sr["out_dir"] / "latest.json").read_text())["dir"] / "members.npz"
    z = np.load(mf)
    Q, t, links, members, t_obs = z["Q"], z["t"], [str(x) for x in z["links"]], [int(x) for x in z["members"]], float(z["t_obs"])
    mem = pd.read_csv(wdir / sr["members_file"], comment="#")
    active = []
    for name, site in reg["sites"].items():
        if run_site(name, site, wdir, a.web, Q, t, t_obs, links, mem, members, a) != "none":
            active.append(name)
    idx = a.web / "alerts/index.json"
    idx.write_text(json.dumps(dict(updated_utc=from_ser(t_obs).isoformat(), alerts=active), indent=1))


if __name__ == "__main__":
    main()
