#!/usr/bin/env python3
"""Short-range ensemble of a watershed twin for the flood trigger (docs/flood_service_design.md).

Every cycle (15 min), each of the posterior members (sites.json "shortrange.members_file") advances from its saved
kernel state with the rain observed since the last cycle, saves its new state, and forecasts HORIZON hours ahead;
the flows of the registry links (sites.json "sites.*.links") are written for every member. The tool talks to the
code-generated kernel directly (ctypes, ohq_kernel ABI: initialize_at, import_state/export_state, set_precipitation,
set_series, run_to, outputs), in the order the engine uses (OHTwin DTKernelModel), so it needs no engine change and
runs beside the engine's 6-hourly cycle with its own member states (sites.json "shortrange.state_dir").

Rain (sites.json "shortrange.forcing.rain", lowest priority first): each source is a folder of per-unit files
(start, end, depth_m; days since 1899-12-30 UTC), a publication latency and a scale. At a cycle time T a source
contributes only the intervals that ended at least `latency_min` before T (what had been published); a source marked
"history_only" (e.g. AORC) is used only before the first interval of any higher-priority source. Higher-priority
sources replace lower ones where they have data. The members' states advance only to T_obs, the end of the last
quarter hour the first ("primary") source had published; after T_obs the forecast rain ("forecast" source, e.g. the
Open-Meteo tail of the live feed) or none. ET: sites.json "shortrange.forcing.et" (daily ET0 per unit, the sources
ET_<unit> and ET2_<unit>).

Usage:
  shortrange_ensemble.py init  <watershed_dir> --at <utc> [--spinup-years 2.75]   # spin up every member to <utc>
  shortrange_ensemble.py cycle <watershed_dir> [--now <utc>]                      # one cycle (default: now)
Outputs (in <watershed_dir>): <state_dir>/member_NNN.npz; <out_dir>/<T_obs>/members.npz and latest.json.
"""
import argparse
import ctypes as C
import json
import os
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

EPOCH = pd.Timestamp("1899-12-30", tz="UTC")
ser = lambda t: (pd.Timestamp(t) - EPOCH).total_seconds() / 86400.0
from_ser = lambda d: EPOCH + pd.to_timedelta(d, unit="D")
DT_OUT = 1 / 96                      # 15-min output grid (days)
HISTORY_D = 0.5                      # link history kept per member (days) and written with each forecast


# ---------------------------------------------------------------------------------------------- configuration
def load(wdir: Path):
    reg = json.loads((wdir / "sites.json").read_text())
    sr = reg["shortrange"]
    cfg = json.loads((wdir / "deployments/live/config.json").read_text())["solver"]
    lib = (wdir / "deployments/live" / cfg["library"]).resolve()
    links = {}
    for s in reg["sites"].values():
        links.update(s["links"])
    mem = pd.read_csv(wdir / sr["members_file"], comment="#")
    return reg, sr, cfg, lib, links, mem


# ---------------------------------------------------------------------------------------------- forcing
def read_rows(path: Path, scale=1.0):
    if not path.exists():
        return np.zeros((0, 3))
    a = pd.read_csv(path, header=None).to_numpy(dtype=float)
    a = a[np.isfinite(a[:, -1])]                     # rain: (start, end, depth); ET: (t, value)
    a[:, -1] *= scale
    return a


def rain_rows(sr, unit: str, n: int, T: float, t_from: float, t_to: float):
    """(rows [start, end, depth_m] covering [t_from, t_to] without gaps, T_obs) for one unit at cycle time T."""
    srcs = sr["forcing"]["rain"]
    layers = []
    for s in srcs:
        a = read_rows(Path(s["dir"]) / s["pattern"].format(unit=unit, n=n), s.get("scale", 1.0))
        a = a[a[:, 1] <= T - s.get("latency_min", 0) / 1440 + 1e-6]
        layers.append((s, a))
    # history_only: before the first interval of any higher-priority source
    rows = np.zeros((0, 3))
    for k, (s, a) in enumerate(layers):
        if s.get("history_only"):
            starts = [b[0, 0] for _, b in layers[k + 1:] if len(b)]
            if starts:
                a = a[a[:, 1] <= min(starts) + 1e-6]
        if len(a) and len(rows):
            rows = rows[(rows[:, 1] <= a[0, 0] + 1e-6) | (rows[:, 0] >= a[-1, 1] - 1e-6)]
        rows = np.vstack([rows, a]) if len(a) else rows
    rows = rows[np.argsort(rows[:, 0])] if len(rows) else rows
    prim = layers[-1][1]                                    # the highest-priority source defines T_obs
    t_obs = float(prim[-1, 1]) if len(prim) else (float(rows[-1, 1]) if len(rows) else T)
    rows = rows[rows[:, 1] <= t_obs + 1e-6]
    fc = sr["forcing"].get("forecast")
    if fc:
        f = read_rows(Path(fc["dir"]) / fc["pattern"].format(unit=unit, n=n), fc.get("scale", 1.0))
        f = f[f[:, 0] >= t_obs - 1e-6]
        rows = np.vstack([rows, f]) if len(f) else rows
    rows = rows[(rows[:, 1] > t_from - 1) & (rows[:, 0] < t_to + 1)]
    # fill gaps (and the end) with zero-rain bins, so the kernel never interpolates across a gap
    out, t = [], (rows[0, 0] if len(rows) else t_from - 1)
    for r in rows:
        if r[0] > t + 1e-6:
            out.append((t, r[0], 0.0))
        out.append(tuple(r)); t = max(t, r[1])
    if t < t_to + 1:
        out.append((t, t_to + 1, 0.0))
    return np.array(out), t_obs


def et_rows(sr, unit, n, t_from, t_to):
    e = sr["forcing"]["et"]
    a = read_rows(Path(e["dir"]) / e["pattern"].format(unit=unit, n=n))
    return a[(a[:, 0] > t_from - 2) & (a[:, 0] < t_to + 2)]


def forcing_bundle(sr, units, T, t_from, t_to):
    rain, tobs = {}, []
    for k, u in enumerate(units, start=1):
        rain[u], to = rain_rows(sr, u, k, T, t_from, t_to)
        tobs.append(to)
    et = {u: et_rows(sr, u, k, t_from, t_to) for k, u in enumerate(units, start=1)}
    return rain, et, min(tobs)


# ---------------------------------------------------------------------------------------------- kernel (worker)
_K = {}


def _init(lib, prefix):
    L = C.CDLL(str(lib))
    f = lambda n: getattr(L, prefix + n)
    for n in ["output_name", "parameter_name", "state_name"]:
        f(n).restype = C.c_char_p
    f("initialize_at").argtypes = [C.c_void_p, C.c_double, C.c_double]
    f("run_to").argtypes = [C.c_void_p, C.c_double]
    f("set_parameter").argtypes = [C.c_void_p, C.c_int, C.c_double]
    f("create").restype = C.c_void_p
    for n in ["apply_parameters", "destroy", "solution_failed"]:
        f(n).argtypes = [C.c_void_p]
    f("outputs").argtypes = [C.c_void_p, C.POINTER(C.c_double)]
    dp = C.POINTER(C.c_double)
    f("set_precipitation").argtypes = [C.c_void_p, C.c_char_p, C.c_char_p, dp, dp, dp, C.c_int]
    f("set_series").argtypes = [C.c_void_p, C.c_char_p, C.c_char_p, dp, dp, C.c_int]
    f("export_state").argtypes = [C.c_void_p, dp, dp, C.c_void_p, dp]
    f("import_state").argtypes = [C.c_void_p, C.c_double, dp, dp, C.c_void_p, dp]
    _K.update(f=f, names=[f("output_name")(i).decode() for i in range(f("n_outputs")())],
              pnames=[f("parameter_name")(i).decode() for i in range(f("n_parameters")())],
              snames=[f("state_name")(i).decode() for i in range(f("n_states")())])


def _member_run(job):
    """job: (member index, params, t_start, state or None, t_obs, t_end, rain, et, links, dt0). Returns the link
    series (t, values) from t_start to t_end and the state at t_obs."""
    k, params, t0, state, t_obs, t_end, rain, et, links, dt0 = job
    f, names = _K["f"], _K["names"]
    arr = lambda a: np.ascontiguousarray(a, dtype=float).ctypes.data_as(C.POINTER(C.c_double))
    h = f("create")()
    for i, nm in enumerate(_K["pnames"]):
        if nm in params:
            f("set_parameter")(h, i, params[nm])
    f("apply_parameters")(h)
    f("initialize_at")(h, t0, dt0)
    if state is not None:
        st = np.ascontiguousarray(state, dtype=float)
        f("import_state")(h, t0, arr(st), None, None, None)
    for u, r in rain.items():
        ok = f("set_precipitation")(h, f"P_{u}".encode(), b"timeseries", arr(r[:, 0]), arr(r[:, 1]), arr(r[:, 2]), len(r))
        assert ok, u
    for u, e in et.items():
        for src in (f"ET_{u}", f"ET2_{u}"):
            f("set_series")(h, src.encode(), b"ET_timeseries", arr(e[:, 0]), arr(e[:, 1]), len(e))
    idx = [[names.index(n) for n in v] for v in links.values()]
    buf = (C.c_double * len(names))()
    rows, saved, ok = [], None, True
    grid = np.arange(t0, t_end + 1e-9, DT_OUT)
    for tk in grid:
        if ok and tk > t0:
            f("run_to")(h, tk)
            ok = not f("solution_failed")(h)
        if ok:
            f("outputs")(h, buf)
        rows.append([sum(buf[i] for i in ix) / 86400.0 if ok else np.nan for ix in idx])
        if saved is None and tk >= t_obs - 1e-9:
            s = np.zeros(len(_K["snames"]))
            f("export_state")(h, s.ctypes.data_as(C.POINTER(C.c_double)), None, None, None)
            saved = s.copy()
    f("destroy")(h)
    return k, grid, np.array(rows), saved, ok


def _spinup(job):
    k, params, t0, t_at, rain, et, links, dt0 = job
    return _member_run((k, params, t0, None, t_at, t_at, rain, et, links, dt0))


# ---------------------------------------------------------------------------------------------- commands
def cmd_init(wdir, at, spinup_years):
    reg, sr, cfg, lib, links, mem = load(wdir)
    T = ser(pd.Timestamp(at))
    t0 = T - spinup_years * 365.25
    units = [f"SC{n:02d}" for n in range(1, sr.get("n_units", 27) + 1)]
    rain, et, _ = forcing_bundle(sr, units, T, t0, T)
    pcols = [c for c in mem.columns if c.startswith("p_")]
    jobs = [(k, {c: float(mem.at[k, c]) for c in pcols}, t0, T, rain, et, links, cfg["dt0"]) for k in range(len(mem))]
    sd = wdir / sr["state_dir"]; sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    with Pool(sr.get("processes", 4), initializer=_init, initargs=(lib, sr.get("prefix", "SligoTwin_"))) as pool:
        for k, grid, rows, state, ok in pool.imap_unordered(_spinup, jobs):
            np.savez_compressed(sd / f"member_{k:03d}.npz", t=T, state=state, ok=ok, hist_t=grid[-1:], hist=rows[-1:])
    print(f"init: {len(mem)} members spun up from {from_ser(t0):%Y-%m-%d} to {from_ser(T):%Y-%m-%d %H:%M} UTC in {time.time() - t_start:.0f} s")


def cmd_cycle(wdir, now):
    reg, sr, cfg, lib, links, mem = load(wdir)
    T = ser(pd.Timestamp(now) if now else pd.Timestamp.now(tz="UTC"))
    sd, od = wdir / sr["state_dir"], wdir / sr["out_dir"]
    files = sorted(sd.glob("member_*.npz"))
    assert files, f"no member states in {sd}: run init first"
    st = {int(f.stem.split("_")[1]): np.load(f) for f in files}
    t_prev = min(float(z["t"]) for z in st.values())
    units = [f"SC{n:02d}" for n in range(1, sr.get("n_units", 27) + 1)]
    t_end = T + sr.get("horizon_h", 24) / 24
    rain, et, t_obs = forcing_bundle(sr, units, T, t_prev, t_end)
    t_obs = np.floor(t_obs * 96 + 1e-6) / 96
    if t_obs <= t_prev + 1e-9:
        print(f"cycle {from_ser(T):%Y-%m-%d %H:%M}Z: no new observed rain since {from_ser(t_prev):%H:%M}; nothing to do")
        return
    pcols = [c for c in mem.columns if c.startswith("p_")]
    jobs = [(k, {c: float(mem.at[k, c]) for c in pcols}, float(st[k]["t"]), st[k]["state"], t_obs, t_obs + sr.get("horizon_h", 24) / 24,
             rain, et, links, cfg["dt0"]) for k in sorted(st)]
    t_start = time.time()
    res = {}
    with Pool(sr.get("processes", 4), initializer=_init, initargs=(lib, sr.get("prefix", "SligoTwin_"))) as pool:
        for k, grid, rows, state, ok in pool.imap_unordered(_member_run, jobs):
            res[k] = (grid, rows, state, ok)
    # new states and the rolling link history; forecast output = history + forecast
    times = np.arange(t_obs - HISTORY_D, t_obs + sr.get("horizon_h", 24) / 24 + 1e-9, DT_OUT)
    Q = np.full((len(st), len(times), len(links)), np.nan)
    for k, (grid, rows, state, ok) in res.items():
        z = st[k]
        ht, hv = z["hist_t"], z["hist"]
        keep = grid <= t_obs + 1e-9
        ht2 = np.concatenate([ht, grid[keep]]); hv2 = np.concatenate([hv, rows[keep]]) if len(hv) else rows[keep]
        sel = ht2 >= t_obs - HISTORY_D - 1e-9
        ht2, hv2 = ht2[sel], hv2[sel]
        _, uix = np.unique(np.round(ht2 * 96).astype(int), return_index=True)
        ht2, hv2 = ht2[uix], hv2[uix]
        if ok and state is not None:
            np.savez_compressed(sd / f"member_{k:03d}.npz", t=t_obs, state=state, ok=ok, hist_t=ht2, hist=hv2)
        allt = np.concatenate([ht2, grid[~keep]]); allv = np.concatenate([hv2, rows[~keep]])
        for j in range(len(links)):
            Q[k, :, j] = np.interp(times, allt, allv[:, j], left=np.nan, right=np.nan)
    stamp = from_ser(t_obs).strftime("%Y%m%dT%H%MZ")
    out = od / stamp; out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / "members.npz", t=times, Q=Q, links=np.array(list(links)), members=np.array(sorted(st)),
                        t_obs=t_obs, cycle=T)
    meta = dict(cycle_utc=from_ser(T).isoformat(), t_obs_utc=from_ser(t_obs).isoformat(), dir=str(out.relative_to(wdir)),
                members=len(st), failed=int(sum(not r[3] for r in res.values())), seconds=round(time.time() - t_start, 1))
    (od / "latest.json").write_text(json.dumps(meta, indent=1))
    print(f"cycle {from_ser(T):%Y-%m-%d %H:%M}Z: states -> {from_ser(t_obs):%H:%M}Z, {len(st)} members, {meta['seconds']} s, "
          f"failed {meta['failed']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["init", "cycle"])
    ap.add_argument("watershed_dir")
    ap.add_argument("--at")
    ap.add_argument("--now")
    ap.add_argument("--spinup-years", type=float, default=2.75)
    a = ap.parse_args()
    wdir = Path(a.watershed_dir).resolve()
    if a.cmd == "init":
        cmd_init(wdir, a.at, a.spinup_years)
    else:
        cmd_cycle(wdir, a.now)


if __name__ == "__main__":
    main()
