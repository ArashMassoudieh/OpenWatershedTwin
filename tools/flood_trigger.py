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
     else "none"; also "watch" when the members on the newest forecast run alone reach it (trigger.watch_on_latest),
     held for trigger.watch_hold_h after the last cycle that met it (a held watch starts no job); "mapping" /
     "mapped" while / after the flood service works on a job. trigger.map_on lists the
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
import functools
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
@functools.lru_cache(maxsize=4)
def _obs_series(path, fmt, mtime):
    """(t, v) of an observation file, cached per file and modification time (replays read it every cycle)."""
    if fmt == "usgs_csv":
        d = pd.read_csv(path, usecols=["time", "value"], parse_dates=["time"]).dropna()
        return d.time.map(ser).to_numpy(), d.value.to_numpy() * CFS
    d = pd.read_csv(path, header=None, names=["t", "v"], comment="#").apply(pd.to_numeric, errors="coerce").dropna()
    return d.t.to_numpy(), d.v.to_numpy()


OBS_MAX_AGE_D = 2.0                 # an observed residual older than this carries no information (>> tau_Q ~ 3 h)


def observed(site, t_obs, with_time=False):
    """Last observed gauge flow (m3/s) at or before t_obs within OBS_MAX_AGE_D, or None; with_time: (q, t_q).
    error_model.obs: {"file": ..., "format": "ohq"} (usgs_feed.py: t,value in m3/s) or "usgs_csv" (time,value cfs)."""
    o = site.get("error_model", {}).get("obs")
    if not o or not Path(o["file"]).exists():
        return (None, None) if with_time else None
    t, v = _obs_series(o["file"], o.get("format", "ohq"), Path(o["file"]).stat().st_mtime)
    k = np.flatnonzero((t <= t_obs + 1e-6) & (t >= t_obs - OBS_MAX_AGE_D) & (v > 0))
    if not len(k):
        return (None, None) if with_time else None
    return (float(v[k[-1]]), float(t[k[-1]])) if with_time else float(v[k[-1]])


def rating(path):
    r = pd.read_csv(path, sep="\t", comment="#")
    r = r[pd.to_numeric(r.INDEP, errors="coerce").notna()]
    return r.INDEP.astype(float).to_numpy(), r.DEP.astype(float).to_numpy()


def with_error(Q, t, t_obs, links, site, mem, members, state_path=None):
    """Q[row, time, link] with each member's AR(1) error (row m is member members[m] of the members file).

    The error of each member is one continuous AR(1) series across cycles (sigma_Q, tau_Q of the member): it
    continues from the member's value at the previous cycle, saved in state_path, and starts from the stationary
    distribution only at the very first cycle. No observation is needed, so the same holds at ungauged sites.
    Optional (error_model.anchor = true): set it at t_obs from the observed residual at the gauge instead (the last
    observation, carried to t_obs by the AR(1) model when it is older). Off by default: in the 2021-2025 replays
    anchoring pinned every member to the rising observed flow and gave false warnings for all three near misses."""
    gi = links.index(site.get("error_model", {}).get("gauge_link", site["trigger"]["link"]))
    i0 = int(np.argmin(abs(t - t_obs)))
    q_obs, t_q = observed(site, t_obs, with_time=True)
    anchor = bool(site.get("error_model", {}).get("anchor", False))
    rng = np.random.default_rng(SEED + int(round(t_obs * 96)))
    dt = float(t[1] - t[0])
    prev = {}
    if state_path is not None and Path(state_path).exists():
        js = json.loads(Path(state_path).read_text())
        if float(js["t"]) <= t_obs + 1e-9:
            prev = {int(k): (float(js["t"]), float(v)) for k, v in js["eps"].items()}
    e0 = {}
    out = Q.copy()
    for m, k in enumerate(members):
        sq, tq = float(mem.at[k, "s_Q"]), float(mem.at[k, "tau_Q"])
        phi = math.exp(-dt / tq)
        e = np.zeros(len(t))
        # residual at the last observation, carried to t_obs by the AR(1) model: conditional mean r exp(-a/tau) and
        # spread sigma sqrt(1 - exp(-2a/tau)) (exact for a gauge outage; a = 0 when the observation is current).
        # Without any observation within OBS_MAX_AGE_D: a draw from the stationary distribution N(0, sigma).
        jq = int(np.argmin(abs(t - t_q))) if q_obs else -1
        qs = Q[m, jq, gi] if q_obs and t[0] <= t_q else np.nan
        if anchor and q_obs and np.isfinite(qs) and qs > 0:
            a = max(float(t[i0] - t_q), 0.0); f = math.exp(-a / tq)
            e[i0] = math.log(q_obs / qs) * f
            if f < 1:                                        # no draw when current: replays reproduce exactly
                e[i0] += sq * math.sqrt(1 - f * f) * rng.standard_normal()
        elif int(k) in prev:                                 # no observation: continue the member's own series
            tp, ep = prev[int(k)]; f = math.exp(-max(t_obs - tp, 0.0) / tq)
            e[i0] = ep * f + sq * math.sqrt(max(1 - f * f, 0.0)) * rng.standard_normal()
        else:
            e[i0] = sq * rng.standard_normal()
        e0[int(k)] = float(e[i0])
        eta = rng.standard_normal(len(t))
        for i in range(i0 + 1, len(t)):
            e[i] = phi * e[i - 1] + sq * math.sqrt(1 - phi ** 2) * eta[i]
        out[m, i0:] = Q[m, i0:] * np.exp(e[i0:])[:, None]
    if state_path is not None:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(json.dumps({"t": t_obs, "eps": e0}))
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
def run_site(name, site, wdir, web, Q, t, t_obs, links, mem, members, args, lag_run=None):
    site["_name"] = name
    trig = site["trigger"]
    thr = float(trig["threshold_cms"])
    Qe, q_obs = with_error(Q, t, t_obs, links, site, mem, members, state_path=wdir / "state/alerts" / f"{name}_ar1.json")
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
    # watch from either signal: the whole (time-lagged) ensemble, or the members on the newest forecast run alone
    # (it reacts first to a new forecast); a watch raised by the newest run alone maps that run's members
    reason = None
    watch_lag = top >= thr
    watch_new = False
    if lag_run is not None and trig.get("watch_on_latest", True):
        sub = np.array([k for k in good if lag_run[k] == 0])
        if len(sub) >= 10:
            o2 = sub[np.argsort(peaks[sub])]
            pick_new = {f"p{p}": int(o2[min(int(np.ceil(p / 100 * len(o2))) - 1, len(o2) - 1)]) for p in site.get("members", [50, 90, 95])}
            watch_new = max(float(peaks[k]) for k in pick_new.values()) >= thr
    state_now = "triggered" if stat >= thr else ("watch" if (watch_lag or watch_new) else "none")
    if state_now == "watch":
        reason = "ensemble" if watch_lag else "newest forecast run"
        if not watch_lag:
            pick = pick_new
    # sticky watch: held for watch_hold_h after the last cycle that met the watch (or warning) condition
    mpath = wdir / "state/alerts" / f"{name}.json"; mpath.parent.mkdir(parents=True, exist_ok=True)
    memo = json.loads(mpath.read_text()) if mpath.exists() else {}
    state = state_now
    if state_now != "none":
        memo["last_watch_utc"] = issued.isoformat()
    elif memo.get("last_watch_utc"):
        held_h = (issued - pd.Timestamp(memo["last_watch_utc"])).total_seconds() / 3600
        if held_h <= float(trig.get("watch_hold_h", 0)):
            state, reason = "watch", f"held (last met {pd.Timestamp(memo['last_watch_utc']).strftime('%H:%MZ')})"
    band = {f"q{p}": [round(float(v), 3) for v in np.nanpercentile(g[good], p, axis=0)] for p in BAND}
    peak_cms = {m: round(float(peaks[k]), 2) for m, k in pick.items()}
    peak_t = {m: times_iso[int(np.nanargmax(np.where(fut, g[k], -np.inf)))] for m, k in pick.items()}

    # memory of the last job
    job = memo.get("job")
    rep = site.get("repeat", {})
    stamp = issued.strftime("%Y%m%dT%H%MZ")
    map_on = trig.get("map_on", ["triggered"])          # states that start a 2D job (e.g. ["watch", "triggered"])
    if state_now in map_on and args.no_submit:
        print(f"{name} {stamp}: {state}: a 2D job would be submitted now for "
              + ", ".join(m for m in pick if peak_cms[m] >= thr) + " (--no-submit)", flush=True)
    if state_now in map_on and not args.no_submit:
        age_h = (issued - pd.Timestamp(job["issued_utc"])).total_seconds() / 3600 if job else 1e9
        changed = (not job) or job.get("state") == "failed" or any(
            abs(peak_cms[m] / max(job["peaks_cms"].get(m, 1e-6), 1e-6) - 1) > rep.get("min_change", 0.1) for m in peak_cms)
        if changed or age_h > rep.get("max_age_h", 1.0):
            # map only members whose forecast peak reaches the threshold (at a watch the median is often far below it)
            # highest member first: the job runs members in order, and the worst case is the map needed first
            send = dict(sorted(((m, k) for m, k in pick.items() if peak_cms[m] >= thr), key=lambda x: -peak_cms[x[0]]))
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

    p_exceed = float(np.mean(peaks[good] >= thr)) if len(good) else None      # forecast probability of reaching the threshold
    alert = dict(site=name, label=site["label"], issued_utc=issued.isoformat(), state=state, watch_reason=reason, statistic=stat_name,
                 p_exceed=None if p_exceed is None else round(p_exceed, 4),
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
        lag_run = z["lag_run"] if "lag_run" in z.files else None
        if run_site(name, site, wdir, a.web, Q, t, t_obs, links, mem, members, a, lag_run) != "none":
            active.append(name)
    idx = a.web / "alerts/index.json"
    idx.write_text(json.dumps(dict(updated_utc=from_ser(t_obs).isoformat(), alerts=active), indent=1))


if __name__ == "__main__":
    main()
