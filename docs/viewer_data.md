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

`elements` drives the element buttons. Each variable's key in the output files is `<element id>:<variable id>`
(e.g. `Soil_1:moisture`). `min`/`max` fix the colour scale (otherwise the viewer uses the data range);
`palette` is one of `moisture`, `depth`, `flow`, `head`.

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

History and forecast in one series per variable; the viewer shades `t > now` as forecast.

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
