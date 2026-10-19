#!/usr/bin/env python3
"""Build the downloadable model package of a watershed twin (the viewer's "Download model" button).

    python3 tools/model_package.py watersheds/RockCreek/model_package.json [--out build/packages/RockCreek_model.zip]

The package is self-contained: the OpenHydroQual model with the calibrated parameter values written in and all paths
relative, the templates it loads, its forcing and observation files, a GeoPackage with every model property of each
sub-catchment, channel segment, reservoir and gage joined to its geometry, a data dictionary built from the template
metadata (descriptions and units), and a README. Spec file keys (paths relative to the spec file unless absolute):

  name, title, model_file             the .ohq the package is built from (structure and forcing as run)
  system_values                       {quantity: value} overrides of `setvalue; object=system` lines (e.g. output names)
  parameters_from                     deployment config.json whose solver.parameters / parameter_set are written in
  templates_dir                       OpenHydroQual resources/ the kernel was generated from
  observation_dirs                    folders with the observation CSVs; files named <site>_<var>_<period>.csv are
                                      merged across periods (_cal, _val, _all) into observations/<site>_<var>.csv
  gis: {subcatchments, reaches, control_points, boundary, extra_layers{name: path}}
  extra_tables: {name: csv}           copied into the GeoPackage as attribute tables (e.g. source attributes)
  readme: {summary, units, run, documentation_url, viewer_url, license, citations[]}
"""
import argparse
import csv
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

EPOCH = pd.Timestamp("1899-12-30")


# ---- .ohq script ---------------------------------------------------------------------------------------------------
class Line:
    """One command line of an OHQ script: 'keyword; key=value,key=value'. Values never contain commas."""
    def __init__(self, text):
        self.text = text.rstrip("\n")
        self.dirty = False
        head, _, rest = self.text.partition(";")
        self.sep = ", " if ", " in rest else ","
        self.lead = " " if rest.startswith(" ") else ""
        self.keyword = head.strip()
        self.items = []                                         # [key, value] in order; ('', '') for empty fields
        if rest.strip():
            for part in rest.split(","):
                k, eq, v = part.partition("=")
                self.items.append([k.strip(), v.strip()] if eq else [part.strip(), None])

    def get(self, key, default=None):
        return next((v for k, v in self.items if k == key), default)

    def set(self, key, value):
        self.dirty = True
        for it in self.items:
            if it[0] == key:
                it[1] = value
                return
        self.items.append([key, value])

    def render(self):
        """The original text unless a value changed; then the same separators as the original."""
        if not self.dirty:
            return self.text
        eq = " = " if " = " in self.text else "="
        return self.keyword + ";" + self.lead + self.sep.join(k if v is None else f"{k}{eq}{v}" for k, v in self.items)


def fmt(v):
    return repr(float(v)) if not isinstance(v, str) else v


def build_model(model_file, params, files, system_values=None):
    """Return (ohq text, objects{name: {type, kind, props}}, links[], parameter rows). `files` collects
    {archive path: source path} for every file the model references."""
    lines = [Line(t) for t in Path(model_file).read_text().splitlines() if t.strip()]
    by_name = {}
    for ln in lines:
        if ln.keyword in ("create block", "create composite", "create source", "create link", "create parameter",
                          "create observation"):
            by_name[ln.get("name")] = ln
    bound = []                                                   # (object, quantity, parameter)
    for ln in lines:
        if ln.keyword == "setvalue" and ln.get("object") == "system" and ln.get("quantity") in (system_values or {}):
            ln.set("value", str(system_values[ln.get("quantity")]))
        if ln.keyword == "setasparameter":
            bound.append((ln.get("object"), ln.get("quantity"), ln.get("parametername")))
        kw = ln.keyword
        if kw in ("loadtemplate", "addtemplate"):
            src = Path(ln.get("filename"))
            files[f"templates/{src.name}"] = src
            ln.set("filename", f"templates/{src.name}")
        for key in ("timeseries", "ET_timeseries"):
            if ln.get(key):
                src = Path(ln.get(key))
                files[f"forcing/{src.name}"] = src
                ln.set(key, f"forcing/{src.name}")
        if kw == "create observation" and ln.get("observed_data"):
            src = Path(ln.get("observed_data"))
            m = re.match(r"(.+?)_(cal|val|all)$", src.stem)
            name = (m.group(1) if m else src.stem) + src.suffix
            files[f"observations/{name}"] = ("merge", src)
            ln.set("observed_data", f"observations/{name}")
    # calibrated values: the parameter blocks and every quantity bound to them
    prow = []
    for name, value in params.items():
        p = by_name.get(name)
        if p is None:
            sys.exit(f"parameter {name} is not in the model")
        p.set("value", fmt(value))
    for obj, qty, par in bound:
        if par in params and obj in by_name:
            by_name[obj].set(qty, fmt(params[par]))
    for name, ln in by_name.items():
        if ln.keyword == "create parameter":
            targets = sorted({q for o, q, p in bound if p == name})
            prow.append({"parameter": name, "value": float(ln.get("value") or "nan"),
                         "lower_bound": float(ln.get("low")), "upper_bound": float(ln.get("high")),
                         "prior": ln.get("prior_distribution"), "applies_to": ";".join(targets),
                         "n_objects": sum(1 for o, q, p in bound if p == name)})
    objects = {}
    for name, ln in by_name.items():
        if ln.keyword in ("create parameter",):
            continue
        props = {k: v for k, v in ln.items if k not in ("type", "name") and v is not None and k}
        objects[name] = {"kind": ln.keyword.split()[1], "type": ln.get("type"), "props": props}
    return "\n".join(ln.render() for ln in lines) + "\n", objects, prow


# ---- template metadata (description and SI unit of every quantity) -----------------------------------------------
def template_meta(templates):
    meta = {}
    for f in templates:
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        for tname, t in d.items():
            if not isinstance(t, dict):
                continue
            for q, spec in t.items():
                if isinstance(spec, dict) and "description" in spec and "type" in spec:
                    unit = spec.get("unit", "").split(";")[0].replace("~^", "^").replace("~", "")
                    meta[(tname, q)] = (spec["description"].replace("~", ""), unit)
    return meta


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def table(objects, names, prefix_cols=None):
    rows = []
    for n in names:
        o = objects[n]
        rows.append({"model_name": n, "model_type": o["type"], **{k: num(v) for k, v in o["props"].items()}})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec")
    ap.add_argument("--out")
    a = ap.parse_args()
    spec_path = Path(a.spec).resolve()
    base = spec_path.parent
    spec = json.loads(spec_path.read_text())
    P = lambda p: (base / p).resolve() if not Path(p).is_absolute() else Path(p)
    repo = Path(__file__).resolve().parents[1]
    out = Path(a.out) if a.out else repo / "build" / "packages" / f"{spec['name']}_model.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    dep = json.loads(P(spec["parameters_from"]).read_text())
    params = dep["solver"]["parameters"]
    pset = dep["solver"].get("parameter_set", "")
    files = {}
    ohq, objects, prow = build_model(P(spec["model_file"]), params, files, spec.get("system_values"))
    # templates from the resources the kernel was generated from (not the paths the .ohq was written with)
    tdir = P(spec["templates_dir"])
    for k in [k for k in files if k.startswith("templates/")]:
        files[k] = tdir / Path(k).name
        if not files[k].exists():
            sys.exit(f"template missing: {files[k]}")
    meta = template_meta([v for k, v in files.items() if k.startswith("templates/")])

    # ---- GeoPackage --------------------------------------------------------------------------------------------
    G = spec["gis"]
    tmp = out.with_suffix(".tmp.gpkg")
    if tmp.exists():
        tmp.unlink()
    dictionary = []

    def describe(layer, df, type_of):
        for c in df.columns:
            if c == "geometry":
                continue
            t = type_of(c)
            d, u = meta.get((t, c), ("", "")) if t else ("", "")
            dictionary.append({"layer": layer, "field": c, "description": d, "unit": u})

    sc = gpd.read_file(P(G["subcatchments"]))
    hru = table(objects, [n for n in sc["id"] if n in objects]).add_prefix("")
    sc = sc.merge(hru, left_on="id", right_on="model_name", how="left")
    hru_type = hru["model_type"].iloc[0]
    sc.to_file(tmp, layer="subcatchments", driver="GPKG")
    describe("subcatchments", sc, lambda c: hru_type)

    rc = gpd.read_file(P(G["reaches"]))
    rows = []
    for _, r in rc.iterrows():
        segs = str(r["segments"]).split(",")
        for k, s in enumerate(segs):
            o = objects.get(s)
            if o is None:
                continue
            down = [l for l in objects.values() if l["kind"] == "link" and l["props"].get("from") == s]
            rows.append({"unit_id": r["id"], "segment": s, "segment_role": "gage segment" if s.startswith("G") else
                         "storm sewer" if "sewer" in o["type"].lower() else "channel", "segment_type": o["type"], "downstream": down[0]["props"].get("to") if down else "",
                         **{k2: num(v) for k2, v in o["props"].items()},
                         "drainage_area_km2": r.get("drainage_area_km2"), "geometry": r.geometry})
    seg = gpd.GeoDataFrame(rows, crs=rc.crs)
    seg_types = dict(zip(seg.segment, seg.segment_type))
    seg.to_file(tmp, layer="channel_segments", driver="GPKG")
    describe("channel_segments", seg, lambda c: seg["segment_type"].iloc[0])

    cp = gpd.read_file(P(G["control_points"]))
    obs = {n: o for n, o in objects.items() if o["kind"] == "observation"}
    cp["observed_in_model"] = cp["id"].map(lambda i: ";".join(sorted(n for n in obs if n.endswith(i))))
    cp["model_object"] = cp["id"].map(lambda i: next((o["props"].get("object") for n, o in obs.items()
                                                      if n.endswith(i) and n.startswith("H_")), ""))
    gages = cp[cp["kind"] == "gage"].copy()
    gages.to_file(tmp, layer="gages", driver="GPKG")
    dams = cp[cp["kind"] == "dam"].copy()
    ponds = [n for n, o in objects.items() if o["type"] == "Pond"]
    if len(dams) and ponds:
        key = lambda s: re.sub(r"[^a-z]", "", s.lower())
        pmap = {key(n): n for n in ponds}
        # a "model_name" column of the control points names the pond directly; else match by name
        given = dams["model_name"] if "model_name" in dams else pd.Series("", index=dams.index)
        dams["model_name"] = [g if isinstance(g, str) and g in ponds else pmap.get(key(n), "")
                              for g, n in zip(given, dams["name"])]
        pt = table(objects, ponds)
        for n in ponds:                                         # outlet structures as columns
            for ln_name, o in objects.items():
                if o["kind"] == "link" and o["props"].get("from") == n:
                    tag = "principal_spillway" if o["type"] == "Riser_Spillway" else \
                          "emergency_spillway" if o["type"] == "Emergency_Spillway" else o["type"]
                    for k2, v in o["props"].items():
                        if k2 not in ("from",):
                            pt.loc[pt.model_name == n, f"{tag}_{k2}"] = num(v)
        dams = dams.merge(pt, on="model_name", how="left")
        dams.to_file(tmp, layer="reservoirs", driver="GPKG")
        describe("reservoirs", dams, lambda c: "Pond" if c in objects[ponds[0]]["props"] else None)
    gpd.read_file(P(G["boundary"])).to_file(tmp, layer="watershed_boundary", driver="GPKG")
    for lname, path in G.get("extra_layers", {}).items():
        gpd.read_file(P(path)).to_file(tmp, layer=lname, driver="GPKG")

    # attribute tables: links, sources, parameters, extra tables
    import sqlite3
    links = pd.DataFrame([{"model_name": n, "link_type": o["type"], **o["props"]}
                          for n, o in objects.items() if o["kind"] == "link"])
    sources = pd.DataFrame([{"model_name": n, "source_type": o["type"], **o["props"]}
                            for n, o in objects.items() if o["kind"] == "source"])
    other = pd.DataFrame([{"model_name": n, "block_type": o["type"], **o["props"]}
                          for n, o in objects.items() if o["kind"] == "block" and o["type"] not in
                          ("Pond",) and n not in seg_types])
    tables = {"links": links, "forcing_sources": sources, "calibrated_parameters": pd.DataFrame(prow),
              "observations": pd.DataFrame([{"model_name": n, **o["props"]} for n, o in obs.items()]),
              "boundary_blocks": other}
    for tname, path in spec.get("extra_tables", {}).items():
        tables[tname] = pd.read_csv(P(path), dtype={c: str for c in ("site", "id")})
    con = sqlite3.connect(tmp)
    for tname, df in tables.items():
        df.to_sql(tname, con, index=False)
        con.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier) VALUES (?, 'attributes', ?)",
                    (tname, tname))
    con.commit()
    con.close()
    for t in ("links", "boundary_blocks"):
        for c in tables[t].columns:
            dictionary.append({"layer": t, "field": c, "description": "", "unit": ""})
    for _, r in links.drop_duplicates("link_type").iterrows():
        for c in links.columns:
            d, u = meta.get((r["link_type"], c), ("", ""))
            if d:
                dictionary.append({"layer": f"links ({r['link_type']})", "field": c, "description": d, "unit": u})
    dd = pd.DataFrame(dictionary).drop_duplicates(["layer", "field"], keep="last")
    dd = dd[~((dd.layer.isin(["links", "boundary_blocks"])) & (dd.description == ""))]

    # ---- README --------------------------------------------------------------------------------------------------
    R = spec["readme"]
    t0, t1 = (float(next(l.split("value=")[1] for l in ohq.splitlines() if f"quantity={q}," in l))
              for q in ("simulation_start_time", "simulation_end_time"))
    day = lambda x: (EPOCH + pd.to_timedelta(x, unit="D")).strftime("%Y-%m-%d")
    pr = "\n".join(f"| {p['parameter']} | {p['value']:.6g} | {p['lower_bound']:g} – {p['upper_bound']:g} | "
                   f"{p['applies_to']} ({p['n_objects']} objects) |" for p in prow)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    model_name = f"{spec['name']}_model.ohq"
    readme = f"""# {spec['title']}

{R['summary']}

Package built {built}. Parameter set: {pset}. Simulation period in the model file: {day(t0)} to {day(t1)}.
Live twin: {R.get('viewer_url', '')} · Model documentation: {R.get('documentation_url', '')}

## Contents

| Path | What |
|---|---|
| `{model_name}` | OpenHydroQual model script with the calibrated parameter values written in; all paths relative to this folder |
| `templates/` | the OpenHydroQual templates the model loads (the versions the model was run with) |
| `forcing/` | per-sub-catchment forcing files referenced by the model |
| `observations/` | observed series the model is compared with (referenced by its observation objects) |
| `{spec['name']}_model.gpkg` | GeoPackage: every model property of each sub-catchment, channel segment, reservoir and gage joined to its geometry, plus attribute tables (links, forcing sources, calibrated parameters, observations, source data) |
| `data_dictionary.csv` | description and unit of each field, from the OpenHydroQual template metadata |
| `LICENSE.txt` | {R['license']} |

## Units and conventions

{R['units']}

## Running the model

{R['run']}

## Calibrated parameters

Each parameter below replaces the listed quantity in every object it is bound to (`setasparameter` lines in the
model file). The values are already written into the objects, so the GeoPackage shows the values the model uses.

| Parameter | Value | Calibration bounds | Applies to |
|---|---|---|---|
{pr}

## GeoPackage layers

- `subcatchments`: one hydrologic response unit per polygon (OpenHydroQual composite `{hru_type}`); `id` is the
  model object name.
- `channel_segments`: one row per channel segment (the gage segments are separate short segments at the stream gages,
  sharing the line of their unit); `downstream` is the next segment.
- `gages`, `reservoirs`, `watershed_boundary` and any extra layers (see below).
- Attribute tables: `links` (every connection in the model: channel routing, groundwater exchange, spillways),
  `forcing_sources`, `calibrated_parameters`, `observations`, `boundary_blocks`{''.join(', `' + t + '`' for t in spec.get('extra_tables', {}))}.

## License and citation

{R['license']}. When you use this model, please cite:

{chr(10).join('- ' + c for c in R['citations'])}
"""
    # ---- zip --------------------------------------------------------------------------------------------------------
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        root = f"{spec['name']}_model/"
        z.writestr(root + model_name, ohq)
        z.writestr(root + "README.md", readme)
        z.writestr(root + "LICENSE.txt", R["license_text"])
        z.writestr(root + "data_dictionary.csv", dd.to_csv(index=False))
        z.write(tmp, root + f"{spec['name']}_model.gpkg")
        n_obs = 0
        for arc, src in sorted(files.items(), key=lambda kv: kv[0]):
            if isinstance(src, tuple):                         # observation series merged over periods
                stem = re.match(r"(.+?)_(cal|val|all)$", src[1].stem).group(1) if re.match(r"(.+?)_(cal|val|all)$", src[1].stem) else src[1].stem
                parts = []
                for d in spec["observation_dirs"]:
                    parts += [pd.read_csv(f, header=None) for f in sorted(P(d).glob(f"{stem}_*.csv"))
                              if re.fullmatch(rf"{re.escape(stem)}_(cal|val|all)", f.stem)]
                if not parts:                                   # no period files: the referenced file itself
                    parts = [pd.read_csv(src[1], header=None)]
                df = pd.concat(parts).drop_duplicates(0).sort_values(0)
                df = df[(df[0] >= t0) & (df[0] <= t1)]
                z.writestr(root + arc, df.to_csv(index=False, header=False, float_format="%.6g"))
                n_obs += 1
            else:
                z.write(src, root + arc)
    tmp.unlink()
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB; {sum(1 for k in files if k.startswith('forcing/'))} forcing "
          f"files, {n_obs} observation series, {len(sc)} sub-catchments, {len(seg)} channel segments, "
          f"{len(prow)} parameters")


if __name__ == "__main__":
    main()
