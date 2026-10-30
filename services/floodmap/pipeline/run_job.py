#!/usr/bin/env python3
"""Flood-map job runner: run_job.py <job_dir>. Called by the flood-map service (services/floodmap) for each job.

Reads <job_dir>/request.json (contract: docs/flood_service_design.md, "Flood service API"), and for each member
(e.g. p50, p90, p95, in that order):
  1. picks the 2D window from the member's hydrograph at the site's trigger link (floodlib.run_window);
  2. writes the boundary hydrographs (inflow edge and point sources from the site's link composition);
  3. starts 2D-DWE from the pre-built base-flow state nearest the inflow at the window start (no spin-up);
  4. writes the products to <job_dir>/out/<member>/: max_depth.png (map for the viewer popup), max_depth.tif,
     impacts.json (US units), gauges.csv, and the 2D log.
<job_dir>/status.json is kept current (state, progress 0-1, message, products), so the service and the twin can
follow the job. Site files: <service>/sites/<site>.json, or the folder in $FLOODMAP_SITES_DIR.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import floodlib as fl  # noqa: E402

SITES_DIR = Path(__import__("os").environ.get("FLOODMAP_SITES_DIR", Path(__file__).resolve().parents[1] / "sites"))
LOCAL = "America/New_York"


class Status:
    def __init__(self, job_dir: Path):
        self.path = job_dir / "status.json"
        self.d = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.d.setdefault("products", {})

    def set(self, **kw):
        self.d.update(kw)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1, default=str))
        tmp.replace(self.path)


def run_member(job_dir, site, name, member, st, k, n):
    out = job_dir / "out" / name
    run = out / "run"
    run.mkdir(parents=True, exist_ok=True)
    df = fl.member_frame(member)
    start, end = fl.run_window(df, site)
    t, q_in, pts = fl.hydrographs(df, site, start, end)
    inflow = run / "bc_inflow.csv"
    fl.write_bc(inflow, t, q_in, f"{name}: inflow edge, t = 0 at {start.isoformat()}")
    pfiles = {}
    for pn, q in pts.items():
        pfiles[pn] = run / f"bc_{pn}.csv"
        fl.write_bc(pfiles[pn], t, q, f"{name}: {pn}")
    h0, lvl = fl.choose_hotstart(site, float(q_in[0]))
    t_end = (end - start).total_seconds()
    (run / "config.txt").write_text(fl.config_text(site, f"floodmap_{name}", start, t_end, inflow, pfiles, h0, site.get("t_out_s", 600)))
    st.set(message=f"{name}: 2D run {start.tz_convert(LOCAL):%m-%d %H:%M} - {end.tz_convert(LOCAL):%H:%M} local "
                   f"({t_end / 3600:.1f} h), hot start {lvl['q_cms'] if lvl else 'none'} m3/s")
    env = {**__import__("os").environ, "OMP_NUM_THREADS": str(site.get("threads", 12))}
    t0 = time.time()
    with open(run / "run.log", "w") as log:
        p = subprocess.Popen([site["dwe_binary"], "--config", "config.txt", "--out", "out"], cwd=run, stdout=log,
                             stderr=subprocess.STDOUT, env=env)
        pat = re.compile(r"\bt\s+([0-9.]+) s")
        while p.poll() is None:
            time.sleep(15)
            try:
                tail = (run / "run.log").read_text()[-2000:]
                m = pat.findall(tail)
                if m:
                    st.set(progress=round((k + min(float(m[-1]) / t_end, 1.0)) / n, 3))
            except OSError:
                pass
    if p.returncode != 0:
        raise RuntimeError(f"2D-DWE exit {p.returncode} for {name} (see out/{name}/run/run.log)")
    wall = time.time() - t0
    return products(job_dir, site, name, run, start, df, wall, lvl)


def products(job_dir, site, name, run, start, df, wall, lvl):
    import rasterio
    out = job_dir / "out" / name
    with rasterio.open(run / "out/depth_max.tif") as s:
        hmax = s.read(1); tr = s.transform; crs = s.crs; prof = s.profile
    with rasterio.open(site["_root"] / site["dem"]) as s:
        z = s.read(1)
    ext = (tr.c, tr.c + hmax.shape[1] * tr.a, tr.f + hmax.shape[0] * tr.e, tr.f)
    (out / "max_depth.tif").write_bytes((run / "out/depth_max.tif").read_bytes())
    g = pd.read_csv(run / "out/gauges.csv", comment="#")
    g.to_csv(out / "gauges.csv", index=False)
    # in-bank creek: when the model gauge first reaches the site's in-bank stage, else the hot start
    frames = sorted((run / "out/frames").glob("depth_*.tif"))
    base = None
    gs = site.get("gauges", {})
    for gname, gd in gs.items():
        col = f"{gname}_wse_m"
        if col in g.columns and "inbank_stage_ft" in gd:
            stage = (g[col] - gd["datum_ft"] * fl.FT) / fl.FT
            hit = g.t_s[stage >= gd["inbank_stage_ft"]]
            if len(hit) and frames:
                k = min(int(hit.iloc[0] // float(site.get("t_out_s", 600))), len(frames) - 1)
                base = rasterio.open(frames[k]).read(1) > 0.05
            break
    if base is None:
        base = (rasterio.open(frames[0]).read(1) > 0.05) if frames else np.zeros_like(hmax, dtype=bool)
    assets = fl.impact_assets(site, tr, hmax.shape, crs, ext)
    fc = fl.feature_cells(assets, tr, hmax.shape, base)
    imp = fl.impacts(hmax, base, fc, assets["linear"], site["impacts"].get("wet_depth_m", 0.15))
    peak_stage = None
    for gname, gd in gs.items():
        col = f"{gname}_wse_m"
        if col in g.columns:
            peak_stage = round(float((g[col].max() - gd["datum_ft"] * fl.FT) / fl.FT), 2)
            imp["gauge"] = dict(name=gname, peak_stage_ft=peak_stage,
                                peak_time_local=(start + pd.Timedelta(seconds=float(g.t_s[g[col].idxmax()]))).tz_convert(LOCAL).strftime("%Y-%m-%d %H:%M"))
            break
    trig = site["window"]["trigger_link"]
    imp["forecast_peak_cfs"] = round(float(df[trig].max()) / fl.CFS)
    imp["window_local"] = [start.tz_convert(LOCAL).strftime("%Y-%m-%d %H:%M"),
                           (start + pd.Timedelta(seconds=float(g.t_s.iloc[-1]))).tz_convert(LOCAL).strftime("%Y-%m-%d %H:%M")]
    imp["model_minutes"] = round(wall / 60, 1)
    imp["hot_start_cms"] = lvl["q_cms"] if lvl else None
    (out / "impacts.json").write_text(json.dumps(imp, indent=1))
    gxy = None
    for gname, gd in gs.items():
        gi = site["_sources"]["gauges"][gd["sources_key"]]; gxy = (gi["x"], gi["y"]); break
    sub = f"{name.upper()} member: forecast peak {imp['forecast_peak_cfs']:,} cfs" + (f", model gauge {peak_stage:.1f} ft" if peak_stage is not None else "")
    fl.render_map(out / "max_depth.png", z, hmax, tr, ext, assets, site["label"], sub, site["impacts"].get("wet_depth_m", 0.15), gxy)
    return {"max_depth_png": f"{name}/max_depth.png", "max_depth_tif": f"{name}/max_depth.tif",
            "impacts": f"{name}/impacts.json", "gauges": f"{name}/gauges.csv"}


def main():
    job_dir = Path(sys.argv[1]).resolve()
    req = json.loads((job_dir / "request.json").read_text())
    st = Status(job_dir)
    st.set(state="running", progress=0.0, started_utc=pd.Timestamp.now(tz="UTC").isoformat(), message="starting")
    try:
        site = fl.load_site(SITES_DIR, req["site"])
        names = list(req["members"])
        for k, name in enumerate(names):
            prods = run_member(job_dir, site, name, req["members"][name], st, k, len(names))
            st.d["products"][name] = prods
            st.set(progress=round((k + 1) / len(names), 3), message=f"{name} done")
        st.set(state="done", progress=1.0, finished_utc=pd.Timestamp.now(tz="UTC").isoformat(), message="done")
    except Exception as e:                                   # the service marks non-done exits as failed too
        st.set(state="failed", finished_utc=pd.Timestamp.now(tz="UTC").isoformat(), message=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
