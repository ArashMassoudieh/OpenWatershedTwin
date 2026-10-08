#!/usr/bin/env python3
"""Sample viewer outputs (docs/viewer_data.md) from a full-state kernel run, for developing the viewer before the
engine writes them.

Input: daily states of every block (CSV: t, <state name>...; as written by a forward driver), the model's .ohq
(unit geometry and parameters), its rainfall files and observation files. A chosen date plays "now"; the days
after it stand in for the forecast.

  make_sample_outputs.py --states v9_states.csv --obs v9_obs.csv --ohq calWY1819v9.ohq \
      --rain-dir forcing_calWY1819v9 --obs-dir obs_calWY1819v9 --now 2019-07-01 --out viewer/sample
"""
import argparse
import datetime as dt
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
EPOCH = dt.datetime(1899, 12, 30)
D_GW = 3.5
LAYER = {"Soil_1": D_GW / 7, "Soil_2": 2 * D_GW / 7, "Soil_3": 4 * D_GW / 7}
GAGES = {"01647850": ("Turkey Branch near Rockville", "Q_01647850", "H_01647850"),
         "01648000": ("Rock Creek at Sherrill Dr", "Q_01648000", "H_01648000"),
         "01648010": ("Rock Creek at Joyce Rd", "Q_01648010", "H_01648010")}

ELEMENTS = [
    {"id": "Catchment", "label": "Pervious surface",
     "variables": [{"id": "ponding", "label": "Ponded depth", "unit": "mm", "min": 0, "max": 5, "palette": "depth"}]},
    {"id": "Impervious_Catchment", "label": "Impervious surface",
     "variables": [{"id": "ponding", "label": "Ponded depth", "unit": "mm", "min": 0, "max": 2, "palette": "depth"}]},
    {"id": "Reach", "label": "Pervious reach",
     "variables": [{"id": "storage", "label": "Storage", "unit": "m3", "palette": "flow"}]},
    {"id": "Impervious_Reach", "label": "Impervious reach",
     "variables": [{"id": "storage", "label": "Storage", "unit": "m3", "palette": "flow"}]},
    {"id": "Soil_1", "label": "Soil 1",
     "variables": [{"id": "moisture", "label": "Moisture content", "unit": "-", "min": 0.05, "max": 0.45,
                    "palette": "moisture"}]},
    {"id": "Soil_2", "label": "Soil 2",
     "variables": [{"id": "moisture", "label": "Moisture content", "unit": "-", "min": 0.05, "max": 0.45,
                    "palette": "moisture"}]},
    {"id": "Soil_3", "label": "Soil 3",
     "variables": [{"id": "moisture", "label": "Moisture content", "unit": "-", "min": 0.05, "max": 0.45,
                    "palette": "moisture"}]},
    {"id": "Groundwater", "label": "Groundwater",
     "variables": [{"id": "water_table", "label": "Water table above stream bed", "unit": "m", "min": -1, "max": 3,
                    "palette": "head"}]},
    {"id": "Near_Stream_Aquifer", "label": "Near-stream aquifer",
     "variables": [{"id": "water_table", "label": "Water table above stream bed", "unit": "m", "min": -1, "max": 3,
                    "palette": "head"}]},
    {"id": "Stream", "label": "Stream",
     "variables": [{"id": "storage", "label": "Channel storage", "unit": "m3", "palette": "flow"}]},
]


def serial(d):
    return (d - EPOCH).total_seconds() / 86400.0


def kv(s):
    return dict(x.split("=", 1) for x in s.split(",") if "=" in x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", required=True)
    ap.add_argument("--obs", required=True)
    ap.add_argument("--ohq", required=True)
    ap.add_argument("--rain-dir", required=True)
    ap.add_argument("--obs-dir", required=True)
    ap.add_argument("--now", default="2019-07-01")
    ap.add_argument("--history-from", default="2018-07-01")
    ap.add_argument("--forecast-days", type=int, default=7)
    ap.add_argument("--params", default="p_imp=0.7768502,p_gwpor=0.06064627,p_fr=0.05,p_nsSy=0.15")
    ap.add_argument("--out", default=str(ROOT / "viewer/sample"))
    a = ap.parse_args()
    P = {k: float(v) for k, v in kv(a.params).items()}
    out = Path(a.out)
    (out / "outputs/units").mkdir(parents=True, exist_ok=True)
    (out / "outputs/gages").mkdir(parents=True, exist_ok=True)
    (out / "gis").mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "watersheds/RockCreek/gis").glob("*.geojson"):
        shutil.copy(f, out / "gis" / f.name)

    hru = {}
    for line in Path(a.ohq).read_text().splitlines():
        if line.startswith("create composite;type=") and "HRU" in line:
            d = kv(line.split(",", 1)[1])
            hru[d["name"]] = d
    reaches = json.loads((ROOT / "watersheds/RockCreek/gis/reaches.geojson").read_text())
    unit_seg = {f["properties"]["id"]: f["properties"]["segments"].split(",")[0] for f in reaches["features"]}

    st = pd.read_csv(a.states)
    t = st.t.values
    now = serial(dt.datetime.fromisoformat(a.now))
    t_from = serial(dt.datetime.fromisoformat(a.history_from))
    t_end = now + a.forecast_days
    keep = (t >= t_from) & (t <= t_end + 1e-6)
    tk = t[keep]

    def var(sc, elem, varid):
        h = hru[sc]
        A = float(h["area"])
        fi = min(float(h["impervious_fraction"]) * P["p_imp"], 0.95)
        surf, D, thick = float(h["surface_elevation"]), float(h["depth_to_groundwater"]), float(h["groundwater_thickness"])
        base = surf - D - thick
        bed = base + 3.0
        if elem == "Stream":
            seg = unit_seg.get(sc)
            return st[seg].values[keep] if seg in st else None
        col = f"{sc}__{elem}"
        if col not in st:
            return None
        S = st[col].values[keep]
        if elem == "Catchment":
            return S / (A * (1 - fi)) * 1000
        if elem == "Impervious_Catchment":
            return S / (A * fi) * 1000
        if elem in LAYER:
            return S / (A * (1 - fi) * LAYER[elem])
        if elem == "Groundwater":
            return base + S / (P["p_gwpor"] * A * (1 - P["p_fr"])) - bed
        if elem == "Near_Stream_Aquifer":
            return base + S / (P["p_nsSy"] * A * P["p_fr"]) - bed
        return S                                     # reaches: storage

    units = sorted(hru)
    map_times = [x for x in tk if x >= now - 30 - 1e-6]
    mi = [int(np.argmin(np.abs(tk - x))) for x in map_times]
    mstate = {"now": now, "times": [round(x, 5) for x in map_times], "variables": {}}
    for e in ELEMENTS:
        for v in e["variables"]:
            key = f"{e['id']}:{v['id']}"
            mstate["variables"][key] = {}
            for sc in units:
                vals = var(sc, e["id"], v["id"])
                mstate["variables"][key][sc] = None if vals is None else [round(float(vals[i]), 5) for i in mi]
    (out / "outputs/map_state.json").write_text(json.dumps(mstate))

    for sc in units:
        series = {}
        r = np.loadtxt(Path(a.rain_dir) / f"precip_sc{int(sc[2:]):02d}.csv", delimiter=",", ndmin=2)
        day = np.floor(r[:, 1] - 1e-9)
        dd = pd.Series(r[:, 2] * 1000, index=day).groupby(level=0).sum()
        days = np.arange(np.floor(t_from), np.floor(t_end) + 1)
        dd = dd.reindex(days, fill_value=0.0)
        series["rain"] = {"label": "Rainfall", "unit": "mm/d", "t": [float(x) for x in days],
                          "v": [round(float(x), 3) for x in dd.values]}
        for e in ELEMENTS:
            for v in e["variables"]:
                vals = var(sc, e["id"], v["id"])
                if vals is None:
                    continue
                series[f"{e['id']}:{v['id']}"] = {"label": f"{e['label']}: {v['label']}", "unit": v["unit"],
                                                   "t": [round(float(x), 5) for x in tk],
                                                   "v": [round(float(x), 5) for x in vals]}
        (out / f"outputs/units/{sc}.json").write_text(json.dumps({"id": sc, "now": now, "series": series}))

    obs = pd.read_csv(a.obs)
    for gid, (name, qn, hn) in GAGES.items():
        series = {}
        for key, nm, scale, unit, lab in (("Q", qn, 1 / 86400, "m3/s", "Flow"), ("H", hn, 1, "m", "Stage")):
            m = obs[obs.obs == nm]
            m = m[(m.t >= t_from) & (m.t <= t_end)]
            dm = (m.v * scale).groupby(np.floor(m.t)).mean()
            series[f"{key}_model"] = {"label": f"{lab}, model", "unit": unit, "t": [float(x) for x in dm.index],
                                      "v": [round(float(x), 5) for x in dm.values]}
            f = Path(a.obs_dir) / f"{gid}_{'flow' if key == 'Q' else 'stage'}_cal.csv"
            if f.exists():
                o = np.loadtxt(f, delimiter=",", ndmin=2)
                o = o[(o[:, 0] >= t_from) & (o[:, 0] <= now)]
                do = pd.Series(o[:, 1] * scale, index=np.floor(o[:, 0])).groupby(level=0).mean()
                series[f"{key}_obs"] = {"label": f"{lab}, USGS", "unit": unit, "t": [float(x) for x in do.index],
                                        "v": [round(float(x), 5) for x in do.values]}
        (out / f"outputs/gages/{gid}.json").write_text(
            json.dumps({"id": gid, "name": name, "now": now, "flood_stage_m": None, "series": series}))

    (out / "outputs/status.json").write_text(json.dumps(
        {"issued_utc": a.now + "T00:00:00Z", "now": now, "forecast_end": t_end,
         "rain_source": "sample (calibration forcing replayed)", "stale": False}))
    cfg = {"title": "Rock Creek digital twin (sample data)",
           "layers": {"units": {"url": "gis/subcatchments.geojson", "id_field": "id", "label_field": "id"},
                      "reaches": {"url": "gis/reaches.geojson", "id_field": "id", "width_field": "drainage_area_km2"},
                      "points": {"url": "gis/control_points.geojson", "id_field": "id", "label_field": "name",
                                 "gage_filter_field": "calibration"},
                      "boundary": {"url": "gis/watershed_boundary.geojson"}},
           "outputs": "outputs/", "elements": ELEMENTS}
    (out / "viewer_config.json").write_text(json.dumps(cfg, indent=1))
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"wrote {out}: {len(units)} units, {len(GAGES)} gages, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
