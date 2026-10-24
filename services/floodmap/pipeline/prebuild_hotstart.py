#!/usr/bin/env python3
"""Pre-build a site's base-flow hot starts: prebuild_hotstart.py <site> <links_csv> <q1> [<q2> ...]

A job starts its 2D run from the pre-built state whose inflow-edge base flow is nearest (in log flow) to the
forecast inflow at the window start, instead of spinning up (saves ~1 h of a 1 m run). Each level is a spin-up of
the site's grid with constant flows: FILL_H hours at FILL_X times the base flow, then HOLD_H hours at it, so the
channel fills and drains to a steady base flow (as the Sligo hindcast spin-up, flood2d/scripts/04_make_run.py).
The base flow is split between the inflow edge and the point sources in the twin's own proportions at base flow:
the links at the first row of <links_csv> (01_ohq_event_bc.py format, e.g. a member's forecast at base flow),
scaled so the inflow edge carries <q>.
Writes <data_root>/<hotstart.dir>/h0_q<q>.tif and index.json (levels, the DEM's sha256, timings).
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import floodlib as fl  # noqa: E402

SITES_DIR = Path(os.environ.get("FLOODMAP_SITES_DIR", Path(__file__).resolve().parents[1] / "sites"))
FILL_H, FILL_X, HOLD_H = 3.0, 5.0, 6.0


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    site_name, links_csv, levels = sys.argv[1], sys.argv[2], [float(x) for x in sys.argv[3:]]
    site = fl.load_site(SITES_DIR, site_name)
    info = site["_sources"]
    d = site["_root"] / site["hotstart"]["dir"]
    d.mkdir(parents=True, exist_ok=True)
    base = pd.read_csv(links_csv).iloc[0]
    edge = info["inflow"].get("links", {info["inflow"]["link"]: 1.0})
    q_edge0 = sum(f * float(base[l]) for l, f in edge.items())
    idx_path = d / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {"levels": []}
    for q in levels:
        r = q / q_edge0
        run = d / f"build_q{q:g}"
        run.mkdir(exist_ok=True)
        t = np.array([0.0, FILL_H * 3600, FILL_H * 3600 + 1800, 1e9])
        mult = np.array([FILL_X, FILL_X, 1.0, 1.0])
        fl.write_bc(run / "bc_inflow.csv", t, q * mult, f"hot-start spin-up, edge base {q} m3/s")
        pfiles = {}
        for n, s in info["sources"].items():
            qb = r * s["fraction"] * float(base[s.get("link", n)])
            pfiles[n] = run / f"bc_{n}.csv"
            fl.write_bc(pfiles[n], t, qb * mult, f"hot-start spin-up {n}")
        start = pd.Timestamp("2000-01-01T00:00Z")
        (run / "config.txt").write_text(fl.config_text(site, f"hotstart_q{q:g}", start, (FILL_H + HOLD_H) * 3600, run / "bc_inflow.csv",
                                                       pfiles, None, 3600))
        t0 = time.time()
        env = {**os.environ, "OMP_NUM_THREADS": str(site.get("threads", 12))}
        with open(run / "run.log", "w") as log:
            rc = subprocess.run([site["dwe_binary"], "--config", "config.txt", "--out", "out"], cwd=run, stdout=log,
                                stderr=subprocess.STDOUT, env=env).returncode
        if rc != 0:
            sys.exit(f"spin-up failed for {q} (see {run}/run.log)")
        f = f"h0_q{q:g}.tif"
        (d / f).write_bytes((run / "out/depth_final.tif").read_bytes())
        g = pd.read_csv(run / "out/gauges.csv", comment="#")
        lv = dict(q_cms=q, file=f, spinup_wall_s=round(time.time() - t0), q_out_final=round(float(g.Q_out_cms.iloc[-1]), 4),
                  q_in_final=round(float(g.Q_in_edge_cms.iloc[-1] + g.Q_in_points_cms.iloc[-1]), 4))
        idx["levels"] = [l for l in idx["levels"] if l["q_cms"] != q] + [lv]
        idx["levels"].sort(key=lambda l: l["q_cms"])
        idx.update(site=site_name, dem_sha256=sha256(site["_root"] / site["dem"]), spin=dict(fill_h=FILL_H, fill_x=FILL_X, hold_h=HOLD_H),
                   built_utc=pd.Timestamp.now(tz="UTC").isoformat())
        idx_path.write_text(json.dumps(idx, indent=1))
        print(f"level {q} m3/s: {lv}", flush=True)


if __name__ == "__main__":
    main()
