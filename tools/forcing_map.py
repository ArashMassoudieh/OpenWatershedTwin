#!/usr/bin/env python3
"""The forcing map (forcing_map.json) of a live deployment: per unit, its rain (csv files: the backfilled history,
then the live feed) and its reference ET (the live feed), each feeding the model's sources by name.

  forcing_map.py --units watersheds/<name>/gis/subcatchments.geojson --out <deployment>/forcing_map.json \\
      --rain "../../../../cache/mrms_history_<name>/rain_{id}.csv" --rain "forcing/rain_{id}.csv" \\
      --et "forcing/et_{id}.csv" [--rain-source P_{id}] [--et-source ET_{id} --et-source ET2_{id}]

File paths are written as given ({id} = unit id); the engine resolves them relative to the deployment folder.
"""
import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--units", required=True)
    ap.add_argument("--id-field", default="id")
    ap.add_argument("--out", required=True)
    ap.add_argument("--rain", action="append", required=True, help="rain csv pattern, in order (repeat)")
    ap.add_argument("--et", required=True, help="reference-ET csv pattern")
    ap.add_argument("--rain-source", default="P_{id}")
    ap.add_argument("--et-source", action="append", help="default ET_{id} and ET2_{id}")
    a = ap.parse_args()
    ets = a.et_source or ["ET_{id}", "ET2_{id}"]
    ids = sorted(f["properties"][a.id_field] for f in json.loads(Path(a.units).read_text())["features"])
    entries = []
    for u in ids:
        entries.append({"name": f"rain {u}", "variable": "precipitation", "provider": "csv",
                        "files": [p.format(id=u) for p in a.rain],
                        "targets": [{"source": a.rain_source.format(id=u), "quantity": "timeseries"}]})
        entries.append({"name": f"ET0 {u}", "variable": "et0", "provider": "csv", "file": a.et.format(id=u),
                        "targets": [{"source": s.format(id=u), "quantity": "ET_timeseries"} for s in ets]})
    Path(a.out).write_text(json.dumps({"entries": entries}, indent=1) + "\n")
    print(f"{len(ids)} units, {len(entries)} entries -> {a.out}")


if __name__ == "__main__":
    main()
