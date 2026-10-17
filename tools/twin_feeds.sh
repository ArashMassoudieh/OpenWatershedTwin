#!/bin/sh
# Feeds of a watershed twin, run by the live engine before each cycle (runtime.pre_cycle_command):
#   sh tools/twin_feeds.sh <Watershed>     (configs in watersheds/<Watershed>/forcing/)
# rain (MRMS + Open-Meteo), reference ET (gridMET + Open-Meteo), USGS observations. A failing feed leaves its
# previous files in place.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${OWT_PYTHON:-$HOME/.venvs/rockcreek/bin/python}"
[ -n "$1" ] || { echo "usage: twin_feeds.sh <Watershed>" >&2; exit 2; }
F="$ROOT/watersheds/$1/forcing"
status=0
"$PY" "$ROOT/tools/mrms_feed.py" --config "$F/mrms_feed.json" || status=1
"$PY" "$ROOT/tools/et_feed.py" --config "$F/et_feed.json" || status=1
"$PY" "$ROOT/tools/usgs_feed.py" --config "$F/usgs_feed.json" || status=1
exit $status
