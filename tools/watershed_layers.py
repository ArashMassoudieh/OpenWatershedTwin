#!/usr/bin/env python3
"""Map layers of a watershed twin (watersheds/<name>/gis/*.geojson, WGS84) from a calibration project's GIS and
its OpenHydroQual model.

  subcatchments.geojson  id (e.g. SC01), sc_id, area_km2, downstream_unit, receiving_segment (the segment its
                         reaches drain to, from the model's Reach_link), gw_segment (unconfined_groundwater_to_stream
                         target), ds_segment (target of the stream link out of the receiving segment: a channel link,
                         not a storm-drain inlet), impervious_connected (model impervious_fraction), and the
                         optional attributes (--attrs)
  reaches.geojson        id = unit id, segments (the receiving segment, the unit's sewer or groundwater segment and
                         channel segments below it that no other unit drains to, e.g. a gage segment and a tail
                         reach), drainage_area_km2, length_m, slope_bed
  watershed_boundary.geojson   dissolved and simplified (raster-stair outlines are smoothed by --simplify)
  control_points.geojson       from a CSV id,kind,name,lat,lon[,da_mi2][,model_name]; gages listed in
                               --calibration get calibration = true (kind "dam" for ponds and reservoirs)

Every property of a unit is a placeholder in viewer_config.json sources ({unit}, {segment}, {ds_segment}, ...).

  watershed_layers.py --units <subcatchments.gpkg> --reaches <reaches.gpkg> --boundary <boundary.gpkg> \\
      --model <model.ohq> --points <control_points.csv> --calibration 01647850 --out watersheds/<name>/gis \\
      [--id-field sc_id] [--id-format SC{:02d}] [--attrs <attributes.gpkg>:TIA=impervious_total,elev_mean_m=mean_elevation_m]
"""
import argparse
import json
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point


def model_topology(path):
    """unit -> receiving segment, unit -> groundwater segment, segment -> downstream target, unit -> impervious"""
    recv, gw, ds, imp, btype = {}, {}, {}, {}, {}
    for line in Path(path).read_text().splitlines():
        if line.startswith("create block"):
            t = re.search(r"type=([^,]+)", line)
            m = re.search(r"name=([^,]+)", line)
            if t and m:
                btype[m.group(1)] = t.group(1)
        if line.startswith("create composite"):
            m = re.search(r"name=([^,]+)", line)
            f = re.search(r"impervious_fraction=([-+\d.eE]+)", line)
            if m and f:
                imp[m.group(1)] = float(f.group(1))
        if not line.startswith("create link"):
            continue
        kv = dict(x.split("=", 1) for x in line.split(";", 1)[1].split(",") if "=" in x)
        t, a, b = kv.get("type", ""), kv.get("from"), kv.get("to")
        if t == "Reach_link":
            recv[a] = b
        elif t == "unconfined_groundwater_to_stream":
            gw[a] = b
        elif "channel" in t.lower() and "inlet" not in t.lower():
            ds.setdefault(a, b)
    return recv, gw, ds, imp, btype


def to_geojson(gdf, path, precision=6):
    gdf = gdf.to_crs(4326)
    path.write_text(gdf.to_json(drop_id=True, to_wgs84=True))
    js = json.loads(path.read_text())

    def rnd(c):
        return [rnd(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(v, precision) for v in c]
    for f in js["features"]:
        f["geometry"]["coordinates"] = rnd(f["geometry"]["coordinates"])
    path.write_text(json.dumps(js, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--units", required=True)
    ap.add_argument("--reaches", required=True)
    ap.add_argument("--boundary", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--points", required=True)
    ap.add_argument("--calibration", default="", help="comma list of calibration gage ids")
    ap.add_argument("--out", required=True)
    ap.add_argument("--id-field", default="sc_id")
    ap.add_argument("--id-format", default="SC{:02d}")
    ap.add_argument("--attrs", default="", help="file:col=name,col=name (joined on the id field)")
    ap.add_argument("--simplify", type=float, default=15.0, help="m, boundary and unit outlines")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    recv, gw, ds, imp, btype = model_topology(a.model)

    def unit_segments(uid):
        """receiving segment, the unit's own sewer/groundwater segment, and channel blocks below the receiving
        segment that no unit drains to (gage segment, tail reach)"""
        r = recv.get(uid, "")
        out = [r] if r else []
        g = gw.get(uid, "")
        if g and g not in out and g not in recv.values():
            out.append(g)
        nxt = ds.get(r)
        while nxt and nxt not in recv.values() and ("Channel" in btype.get(nxt, "") or "Segment" in btype.get(nxt, "")) \
                and nxt not in out:
            out.append(nxt)
            nxt = ds.get(nxt)
        return ",".join(out)

    u = gpd.read_file(a.units)
    uid = {int(r[a.id_field]): a.id_format.format(int(r[a.id_field])) for _, r in u.iterrows()}
    u["id"] = u[a.id_field].astype(int).map(uid)
    if "ds_id" in u:
        u["downstream_unit"] = u["ds_id"].map(lambda d: uid.get(int(d), "") if pd.notna(d) else "")
    u["receiving_segment"] = u["id"].map(lambda i: recv.get(i, ""))
    u["gw_segment"] = u["id"].map(lambda i: gw.get(i, ""))
    u["ds_segment"] = u["receiving_segment"].map(lambda s: ds.get(s, ""))
    u["impervious_connected"] = u["id"].map(lambda i: round(imp.get(i, float("nan")), 4))
    u["area_km2"] = (u.geometry.area / 1e6).round(4)
    if a.attrs:
        f, cols = a.attrs.split(":", 1)
        at = gpd.read_file(f) if f.endswith(".gpkg") else pd.read_csv(f)
        for spec in cols.split(","):
            col, name = spec.split("=")
            u[name] = u[a.id_field].map(dict(zip(at[a.id_field], at[col].round(4))))
    missing = [i for i in u["id"] if not recv.get(i)]
    if missing:
        print("warning: no Reach_link in the model for", missing)
    u["geometry"] = u.geometry.simplify(a.simplify / 2, preserve_topology=True)
    keep = ["id", a.id_field, "area_km2", "downstream_unit", "receiving_segment", "ds_segment", "gw_segment",
            "impervious_connected"] + [c.split("=")[1] for c in (a.attrs.split(":", 1)[1].split(",") if a.attrs else [])]
    to_geojson(u[[c for c in keep if c in u] + ["geometry"]].sort_values("id"), out / "subcatchments.geojson")

    r = gpd.read_file(a.reaches)
    r["id"] = r[a.id_field].astype(int).map(uid)
    r["segments"] = r["id"].map(unit_segments)
    da = "da_dn_km2" if "da_dn_km2" in r else ("da_km2" if "da_km2" in r else None)
    r["drainage_area_km2"] = r[da].round(4) if da else None
    cols = ["id", a.id_field, "segments", "drainage_area_km2"] + [c for c in ("length_m", "slope_bed") if c in r]
    to_geojson(r[cols + ["geometry"]].sort_values("id"), out / "reaches.geojson")

    b = gpd.read_file(a.boundary)
    b = gpd.GeoDataFrame({"name": ["watershed"]}, geometry=[b.union_all().buffer(0).simplify(a.simplify)], crs=b.crs)
    to_geojson(b, out / "watershed_boundary.geojson")

    p = pd.read_csv(a.points, dtype={"id": str})
    cal = {x.strip() for x in a.calibration.split(",") if x.strip()}
    p["calibration"] = p["id"].isin(cal)
    p["model_segment"] = None
    pg = gpd.GeoDataFrame(p.drop(columns=["lat", "lon"]), geometry=[Point(x, y) for x, y in zip(p.lon, p.lat)],
                          crs=4326)
    to_geojson(pg, out / "control_points.geojson", precision=7)
    print(f"{len(u)} units, {len(r)} reaches, {len(p)} points -> {out}")
    print(u[["id", "receiving_segment", "ds_segment", "gw_segment", "downstream_unit"]].to_string(index=False))


if __name__ == "__main__":
    main()
