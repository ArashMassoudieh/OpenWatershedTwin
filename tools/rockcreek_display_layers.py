#!/usr/bin/env python3
"""Rock Creek display layers (map only; the model areas are unchanged).

- combined_sewer_area.geojson: the DC Water combined-sewer area inside the full (untrimmed) sub-catchments.
  15_css_trim.py in the calibration project subtracts it from the sub-catchments (that runoff goes to Blue
  Plains), which leaves SC52 at 8 % of its area in 91 pieces and holes in SC53/SC54; drawn as a mask the
  map explains itself.
- subcatchments.geojson: pieces smaller than --min-piece m2 dropped (slivers left by the trim).

  rockcreek_display_layers.py --calib "/home/arash/Dropbox/Rock Creek Model - OHQ"
"""
import argparse
from pathlib import Path

import geopandas as gpd
from shapely.geometry import MultiPolygon

ROOT = Path(__file__).resolve().parents[1]
GIS = ROOT / "watersheds/RockCreek/gis"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calib", required=True)
    ap.add_argument("--min-piece", type=float, default=1000.0)
    a = ap.parse_args()
    calib = Path(a.calib)
    full = gpd.read_file(calib / "gis/subcatchments.gpkg")
    css = gpd.read_file(calib / "data/raw/dcwater/css_area.geojson").to_crs(full.crs)
    mask = css.union_all().intersection(full.union_all())
    out = gpd.GeoDataFrame({"name": ["Combined sewer area"], "note": ["drains to Blue Plains; not modelled"]},
                           geometry=[mask.simplify(2.0)], crs=full.crs).to_crs(4326)
    out.to_file(GIS / "combined_sewer_area.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    print(f"combined sewer area in the watershed: {mask.area / 1e6:.2f} km2")

    sc = gpd.read_file(GIS / "subcatchments.geojson")
    utm = sc.to_crs(full.crs)
    dropped = 0
    for i, g in enumerate(utm.geometry):
        parts = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
        keep = [p for p in parts if p.area >= a.min_piece] or [max(parts, key=lambda p: p.area)]
        dropped += len(parts) - len(keep)
        utm.loc[utm.index[i], "geometry"] = MultiPolygon(keep) if len(keep) > 1 else keep[0]
    utm.to_crs(4326).to_file(GIS / "subcatchments.geojson", driver="GeoJSON", COORDINATE_PRECISION=6)
    print(f"dropped {dropped} pieces < {a.min_piece:.0f} m2 from the units layer")


if __name__ == "__main__":
    main()
