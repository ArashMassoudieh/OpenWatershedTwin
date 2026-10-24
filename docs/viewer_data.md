# Viewer data contract

The map viewer is a static client: it fetches JSON files over HTTP from the twin's web folder (or reads them from a
local folder on the desktop) and never talks to the engine directly. Times are OpenHydroQual day serials (days
since 1899-12-30, UTC); the viewer converts them to dates.

```
<site>/
  viewer_config.json          what to load and what the element buttons offer
  gis/*.geojson               layers (WGS84)
  outputs/status.json         cycle bookkeeping
  outputs/map_state.json      values per unit and element variable at a few time steps (map colouring, slider)
  outputs/units/<id>.json     full series for one unit (loaded on right-click)
  outputs/gages/<id>.json     model and observed series at a gage
```

## viewer_config.json

```json
{
  "title": "Rock Creek digital twin",
  "layers": {
    "units":    {"url": "gis/subcatchments.geojson", "id_field": "id", "label_field": "id"},
    "reaches":  {"url": "gis/reaches.geojson", "id_field": "id", "width_field": "drainage_area_km2"},
    "points":   {"url": "gis/control_points.geojson", "id_field": "id", "label_field": "name",
                 "gage_filter_field": "calibration"},
    "boundary": {"url": "gis/watershed_boundary.geojson"}
  },
  "outputs": "outputs/",
  "elements": [
    {"id": "Soil_1", "label": "Soil 1",
     "variables": [{"id": "moisture", "label": "Moisture content", "unit": "-", "min": 0.05, "max": 0.45,
                    "palette": "moisture"}]}
  ]
}
```

The watershed's `viewer_config.json` (e.g. `watersheds/RockCreek/viewer_config.json`) is read by both sides: the
engine computes what it lists and copies it next to the outputs; the viewer draws from it. Full example there.

`elements` drives the element buttons. Each variable's key in the output files is `<element id>:<variable id>`
(e.g. `Soil_1:moisture`). `min`/`max` fix the colour scale (otherwise the viewer uses the data range);
`palette` is one of `moisture`, `depth`, `flow`, `head`. An element with `"map_layer": "reaches"` colours the stream
lines instead of the sub-catchments (its values are keyed by reach id; default `"units"`).

Variables, engine side:
- `source`: the model quantity, `"<object>:<quantity>"`, with `{unit}` (unit id) and `{segment}` (the unit's
  `receiving_segment` in the units layer) and `*` globs (first match), e.g. `"{unit}__Soil_1:theta"`,
  `"{segment}-*:flow"` (the segment's outgoing link).
- `divide_by` (optional): a second quantity the value is divided by (per-area fluxes: `"{unit}__Catchment:area"`).
- `scale` (optional, default 1): factor after the division (m3/d -> m3/s: 1.1574074e-5; m/d -> mm/d: 1000; a
  sink reported negative: -1000).
- `tools/twin_outputs.py spec` turns these into the kernel's `--outputs` list.

Variables, viewer side: `"map": true` lists the variable in the map's variable box (keep these few; if no
variable of an element is marked, all are listed); every variable is available in the charts (context menu:
the map variable, all variables of the element, all elements). `"log": true` gives a logarithmic colour scale.

`rain_source` (engine): the model rain source of a unit, e.g. `"P_{unit}"`; its forcing entry gives the rainfall
chart (mm/h). `gages` (engine and viewer): per gage `id`, `name` and `series` (`id` `Q`/`H`, `label`, `unit`,
`source`, `scale` as for variables, `observed`: a file `{gage}_flow.csv` in the observations folder, rows
`t,value` in display units). `subtitle`, `credit` (page shell `viewer/index.html`): shown with the title and a drawing of the watershed while the
WebAssembly viewer loads; `credit` may contain links. `links` (viewer): toolbar buttons that open a URL, e.g. the model documentation:
`{"label": "Model documentation", "tooltip": "...", "url": "https://..."}`.

`layers.masks` (optional): areas drawn hatched with a legend entry, e.g. the combined-sewer area that does not
drain to the stream. `output_times`: history window (days) and the chart and map time steps (hours).

## status.json

```json
{"issued_utc": "2026-10-09T06:00:00Z", "now": 46304.25, "forecast_end": 46311.25,
 "rain_source": "MRMS Pass2 to 2026-10-09T05:00Z, Open-Meteo after", "stale": false}
```

## map_state.json

```json
{
  "now": 46304.25,
  "times": [46274.25, ..., 46304.25, ..., 46311.25],
  "variables": {
    "Soil_1:moisture": {"SC01": [0.21, ...], "SC02": [...]},
    "Reach:storage":   {...}
  }
}
```

One value per entry of `times` for every unit; `null` where undefined. The slider steps through `times`; the
initial position is the entry nearest `now`.

## units/<id>.json

```json
{
  "id": "SC01", "now": 46304.25,
  "series": {
    "rain":            {"label": "Rainfall", "unit": "mm/d", "t": [...], "v": [...]},
    "Soil_1:moisture": {"label": "Soil 1 moisture content", "unit": "-", "t": [...], "v": [...]}
  }
}
```

A unit file may instead carry one time axis for all its series (`"t"` next to `"series"`); a series without its own
`t` uses it (what the engine writes).

History and forecast in one series per variable; the viewer shades `t > now` as forecast.

Optional band: a series may carry `"lo"` and `"hi"` arrays (same length as `v`, `null` where undefined), e.g. the
2.5 and 97.5 percentiles of an ensemble of runs with posterior parameter sets (engine: `solver.ensemble`). The
viewer's "95% intervals" box (unchecked by default, disabled when no series shown has a band) shades between them;
the CSV export then adds `lo,hi` columns. Gage model series (`Q_model`, `H_model`) may carry a band the same way.
`status.json` then has `"band": {"members": 200, "quantiles": [0.025, 0.975], "label": "..."}`, and
`viewer_config.json` may set the checkbox text: `"intervals": {"label": "95% intervals", "tooltip": "..."}`.
Every property of a unit in the units layer is a placeholder in `source` (`{ds_segment}`, `{gw_segment}`, ...).

## gages/<id>.json

```json
{
  "id": "01648000", "name": "Rock Creek at Sherrill Dr", "now": 46304.25,
  "flood_stage_m": null,
  "series": {
    "Q_model": {"label": "Flow, model", "unit": "m3/s", "t": [...], "v": [...]},
    "Q_obs":   {"label": "Flow, USGS",  "unit": "m3/s", "t": [...], "v": [...]},
    "H_model": {...}, "H_obs": {...}
  }
}
```

## Flood alerts (outputs/alerts/)

Written by the flood trigger (`tools/flood_trigger.py`, docs/flood_service_design.md, "Alert file"); the viewer only
reads them.

```
outputs/alerts/index.json            {"updated_utc": "...", "alerts": ["sligo_maple", ...]}: the sites whose state is not "none"
outputs/alerts/<site>.json           one site's alert (contract in docs/flood_service_design.md, "Alert file")
outputs/alerts/<site>/<issued>/...   its products (max-depth PNGs, animations)
```

- The alert list lives in `alerts/index.json` because the engine rewrites `status.json` every cycle. If
  `index.json` is missing, the viewer falls back to `"alerts"` in `status.json`.
- The viewer reads the list at load and with the 15-min refresh; while any alert is active it polls `index.json` and
  every listed `<site>.json` every 2 min, and refreshes the open popup when the file changes. With no active alert
  nothing changes in the viewer.
- Banner: one coloured bar per active site above the map (`watch` amber; `triggered`, `mapping`, `mapped` red) with
  the site label, state, the forecast peak of the trigger statistic and its time (America/New_York, EDT/EST) and
  the lead time from the issue time. Clicking it, or the marker drawn at the centre of `viewer.bbox_lonlat`
  (optional in the alert file: `"viewer": {"label", "bbox_lonlat": [lon0, lat0, lon1, lat1]}`, as in the site
  registry), opens the popup.
- Popup (`FloodAlertDialog`): header (label, issue time, `statistic`, `threshold_cms`, optional `trigger_label`, the
  members' peaks and lead times, `job` state, `progress` and `message`); one tab per entry of `maps` (`p50`, `p90`,
  `p95`, ...: the PNG, `source` `library` = pre-built library map or `2d` = 2D simulation, `stage_ft`, `impacts`);
  the forecast band `forecast.band` (`t_utc`, `q5`, `q25`, `q50`, `q75`, `q95` in m3/s); buttons for `demo_url`,
  top-level `"animations": [{"label", "url"}]` and per-member `"animation": "<path>"`.
- Paths in the alert file (`png`, animations) are relative to the outputs folder (e.g.
  `alerts/sligo_maple/20250719T2135Z/p50_max_depth.png`); absolute http(s) URLs are used as they are.
- `impacts`: numbers with `_ft`, `_m` or `_mi` are lengths (shown in ft, with miles above a quarter mile),
  `_acres` / `_ha` areas (shown in acres), lists (e.g. `cross_streets`: names, or objects with `name`,
  `length_ft`/`length_m`, `max_depth_ft`) one row per item, other numbers are counts (`parking_lots`,
  `parks_playgrounds`, `buildings`).
- Display units: flows in cfs (m3/s x 35.3147), stages and lengths in ft.
