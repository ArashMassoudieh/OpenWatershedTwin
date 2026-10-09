#!/usr/bin/env python3
"""USGS observation feed for a watershed twin: recent flow and stage at the gages, for display next to the model.

NWIS instantaneous values (waterservices.usgs.gov/nwis/iv, provisional) for the last `days` days -> hourly means
(hour-ending, UTC). Flow cfs -> m3/s. Stage: gage height ft -> m, minus the gage height of zero flow of the
calibration (`zero_flow_gh_m`, 30_gage_geometry.py), i.e. depth above the control, comparable to the gage
segment's obs_depth; negative values set to 0. USGS fill/qualifier codes (e.g. -999999 equipment malfunction)
are dropped. Writes <id>_flow.csv and <id>_stage.csv, rows `t,value` (OHQ day serial, display units); a gage or
parameter without data keeps its previous file.

  usgs_feed.py --config watersheds/RockCreek/forcing/usgs_feed.json
Config: {"output_dir": "../deployments/live/observations", "days": 45,
         "gages": {"01648000": {"zero_flow_gh_m": 0.28956}, ...}}
"""
import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

EPOCH = pd.Timestamp("1899-12-30", tz="UTC")
FT, CFS = 0.3048, 0.0283168466


def log(*a):
    print("[usgs_feed]", *a, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    cpath = Path(a.config).resolve()
    c = json.loads(cpath.read_text())
    out = (cpath.parent / c["output_dir"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    gages = c["gages"]
    r = requests.get("https://waterservices.usgs.gov/nwis/iv/", timeout=120, params={
        "format": "json", "sites": ",".join(gages), "parameterCd": "00060,00065", "period": f"P{int(c.get('days', 45))}D"})
    r.raise_for_status()
    got = {}
    for ts in r.json()["value"]["timeSeries"]:
        site = ts["sourceInfo"]["siteCode"][0]["value"]
        code = ts["variable"]["variableCode"][0]["value"]
        nodata = ts["variable"].get("noDataValue", -999999.0)
        rows = [(v["dateTime"], float(v["value"])) for blk in ts["values"] for v in blk["value"]]
        if not rows:
            continue
        s = pd.Series([x[1] for x in rows], index=pd.to_datetime([x[0] for x in rows], utc=True))
        s = s[(s != nodata) & (s > -9999)]
        if s.empty:
            continue
        h = s.resample("1h", label="right", closed="right").mean().dropna()
        if code == "00060":
            kind, v = "flow", h * CFS
        else:
            kind, v = "stage", (h * FT - gages[site].get("zero_flow_gh_m", 0.0)).clip(lower=0)
        got[(site, kind)] = v
    for site in gages:
        for kind in ("flow", "stage"):
            v = got.get((site, kind))
            if v is None:
                log(f"{site} {kind}: no data, previous file kept")
                continue
            t = ((v.index - EPOCH) / pd.Timedelta(days=1)).values
            f = out / f"{site}_{kind}.csv"
            tmp = f.with_suffix(".tmp")
            np.savetxt(tmp, np.column_stack([t, v.values]), fmt=["%.5f", "%.5g"], delimiter=",")
            tmp.replace(f)
            log(f"{site} {kind}: {len(v)} hours to {v.index[-1].strftime('%Y-%m-%d %H:%M')} UTC")


if __name__ == "__main__":
    main()
