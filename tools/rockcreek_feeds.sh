#!/bin/sh
# Rock Creek feeds, run by the live engine before each cycle (runtime.pre_cycle_command): rain (MRMS + Open-Meteo),
# reference ET (gridMET + Open-Meteo), USGS observations. A failing feed leaves its previous files in place.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${OWT_PYTHON:-$HOME/.venvs/rockcreek/bin/python}"
F="$ROOT/watersheds/RockCreek/forcing"
status=0
"$PY" "$ROOT/tools/mrms_feed.py" --config "$F/mrms_feed.json" || status=1
"$PY" "$ROOT/tools/et_feed.py" --config "$F/et_feed.json" || status=1
"$PY" "$ROOT/tools/usgs_feed.py" --config "$F/usgs_feed.json" || status=1
exit $status
