#!/usr/bin/env python3
"""Bias of the twin's MRMS rainfall relative to AORC (the calibration rainfall) over their overlap.

Compares, per sub-catchment and hour, AORC (the calibration project's precip_hourly_mm.csv, mm in the hour ending at
the time stamp) with the MRMS backfill (rain_<id>.csv, depth over [start, end], already multiplied by mrms_scale,
which is divided out here). Reports the overall ratio (AORC / raw MRMS), by month and season, by sub-catchment,
by intensity class (hourly and daily basin rain), by event size, and the correlation, so that the operational
bias factor (mrms_feed.json mrms_scale) can be chosen.

  mrms_bias.py --aorc ".../model/forcing/precip_hourly_mm.csv" --mrms cache/mrms_history \
      --units watersheds/RockCreek/gis/subcatchments.geojson --from 2024-10-01 --to 2025-10-01 --out cache/mrms_bias
"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

EPOCH = pd.Timestamp("1899-12-30")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aorc", required=True)
    ap.add_argument("--mrms", required=True)
    ap.add_argument("--units", required=True)
    ap.add_argument("--from", dest="t0", default="2024-10-01")
    ap.add_argument("--to", dest="t1", default="2025-10-01")
    ap.add_argument("--scale", type=float, default=None, help="mrms_scale in the files (default: rain_status.json)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    mdir = Path(a.mrms)
    scale = a.scale or json.loads((mdir / "rain_status.json").read_text())["mrms_scale"]

    units = json.loads(Path(a.units).read_text())["features"]
    area = {f["properties"]["id"]: f["properties"]["area_km2"] for f in units}
    ids = sorted(area)
    t0, t1 = pd.Timestamp(a.t0), pd.Timestamp(a.t1)

    A = pd.read_csv(a.aorc, index_col=0, parse_dates=True)
    A.columns = [f"SC{int(c):02d}" for c in A.columns]
    A = A[(A.index > t0) & (A.index <= t1)][ids]

    M = {}
    for u in ids:
        r = np.loadtxt(mdir / f"rain_{u}.csv", delimiter=",", ndmin=2)
        end = EPOCH + pd.to_timedelta(np.round(r[:, 1] * 24), unit="h")
        M[u] = pd.Series(r[:, 2] * 1000 / scale, index=end)
    M = pd.DataFrame(M)
    M = M[(M.index > t0) & (M.index <= t1)]
    idx = A.index.intersection(M.index)
    A, M = A.loc[idx], M.loc[idx]
    w = np.array([area[u] for u in ids]) / sum(area.values())
    a_b, m_b = A.values @ w, M.values @ w                       # basin-mean hourly rain, mm
    hb = pd.DataFrame({"aorc": a_b, "mrms": m_b}, index=idx)
    db = hb.resample("1D").sum()
    res = {"hours": len(idx), "from": str(idx[0]), "to": str(idx[-1]), "mrms_scale_in_files": scale,
           "total_aorc_mm": round(a_b.sum(), 1), "total_mrms_mm": round(m_b.sum(), 1),
           "ratio_aorc_over_mrms": round(a_b.sum() / m_b.sum(), 3),
           "r_hourly": round(np.corrcoef(a_b, m_b)[0, 1], 3), "r_daily": round(np.corrcoef(db.aorc, db.mrms)[0, 1], 3)}

    mon = hb.resample("MS").sum()
    mon["ratio"] = mon.aorc / mon.mrms
    season = hb.groupby(hb.index.month.map(lambda m: {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
                                                     6: "JJA", 7: "JJA", 8: "JJA"}.get(m, "SON"))).sum()
    season["ratio"] = season.aorc / season.mrms
    per_unit = (A.sum() / M.sum()).rename("ratio")

    # intensity: hourly basin rain by MRMS class; daily totals by class
    hb_w = hb[(hb.aorc > 0.05) | (hb.mrms > 0.05)]
    hcls = pd.cut(hb_w.mrms, [0, 0.5, 2, 5, 10, 100], include_lowest=True)
    hint = hb_w.groupby(hcls, observed=True).agg(hours=("aorc", "size"), aorc=("aorc", "sum"), mrms=("mrms", "sum"))
    hint["ratio"] = hint.aorc / hint.mrms
    db_w = db[(db.aorc > 0.5) | (db.mrms > 0.5)]
    dcls = pd.cut(db_w.mrms, [0, 2, 5, 10, 25, 50, 500], include_lowest=True)
    dint = db_w.groupby(dcls, observed=True).agg(days=("aorc", "size"), aorc=("aorc", "sum"), mrms=("mrms", "sum"))
    dint["ratio"] = dint.aorc / dint.mrms

    # events: wet spells separated by >= 6 dry hours (basin MRMS or AORC > 0.05 mm/h)
    wet = ((hb.aorc > 0.05) | (hb.mrms > 0.05)).values
    ev, k, gap = np.zeros(len(hb), int), 0, 99
    for i, x in enumerate(wet):
        if x:
            if gap >= 6:
                k += 1
            ev[i] = k
            gap = 0
        else:
            gap += 1
    hb["event"] = ev
    E = hb[hb.event > 0].groupby("event").agg(start=("aorc", lambda s: s.index[0]), aorc=("aorc", "sum"),
                                                mrms=("mrms", "sum"))
    E = E[(E.aorc + E.mrms) > 2]
    ecls = pd.cut(E.mrms, [0, 5, 10, 25, 50, 500], include_lowest=True)
    eint = E.groupby(ecls, observed=True).agg(events=("aorc", "size"), aorc=("aorc", "sum"), mrms=("mrms", "sum"))
    eint["ratio"] = eint.aorc / eint.mrms
    big = E.sort_values("mrms", ascending=False).head(10).copy()
    big["ratio"] = big.aorc / big.mrms

    with open(out / "mrms_bias.txt", "w") as f:
        f.write(json.dumps(res, indent=1) + "\n\nBy month (mm)\n" + mon.round(2).to_string() +
                "\n\nBy season\n" + season.round(2).to_string() +
                "\n\nHourly basin rain by MRMS intensity (mm/h)\n" + hint.round(2).to_string() +
                "\n\nDaily basin rain by MRMS daily total (mm)\n" + dint.round(2).to_string() +
                "\n\nEvents (>= 6 h dry between) by MRMS event total (mm)\n" + eint.round(2).to_string() +
                "\n\nTen largest events\n" + big.round(2).to_string() +
                "\n\nPer sub-catchment ratio: " + per_unit.describe().round(3).to_string() + "\n")
    per_unit.round(3).to_csv(out / "mrms_bias_per_unit.csv")

    fig, ax = plt.subplots(1, 3, figsize=(14, 4.2))
    ax[0].bar(range(len(mon)), mon.ratio, color="#4292c6")
    ax[0].axhline(res["ratio_aorc_over_mrms"], color="k", lw=1, ls="--")
    ax[0].set_xticks(range(len(mon)), [d.strftime("%b\n%y") for d in mon.index], fontsize=7)
    ax[0].set_ylabel("AORC / MRMS")
    ax[0].set_title("Monthly ratio (dashed: overall)", fontsize=9)
    ax[1].loglog(db_w.mrms + 0.1, db_w.aorc + 0.1, ".", ms=4, color="#2171b5")
    lim = [0.1, max(db_w.max()) * 1.3]
    ax[1].plot(lim, lim, "k-", lw=0.8)
    ax[1].plot(lim, np.array(lim) * res["ratio_aorc_over_mrms"], "k--", lw=0.8)
    ax[1].set_xlabel("MRMS daily basin rain (mm)")
    ax[1].set_ylabel("AORC (mm)")
    ax[1].set_title(f"Daily, r = {res['r_daily']}", fontsize=9)
    ax[2].plot(E.mrms, E.aorc / E.mrms, ".", ms=5, color="#d94801")
    ax[2].set_xscale("log")
    ax[2].axhline(1, color="k", lw=0.8)
    ax[2].axhline(res["ratio_aorc_over_mrms"], color="k", lw=0.8, ls="--")
    ax[2].set_xlabel("MRMS event total (mm)")
    ax[2].set_ylabel("AORC / MRMS")
    ax[2].set_ylim(0, 3)
    ax[2].set_title("Event ratio vs event size", fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "fig_mrms_bias.png", dpi=110)
    print(open(out / "mrms_bias.txt").read())


if __name__ == "__main__":
    main()
