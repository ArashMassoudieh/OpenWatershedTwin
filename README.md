# OpenWatershedTwin

A watershed-scale digital twin built on [OpenHydroQual](https://github.com/ArashMassoudieh/OpenHydroQual)
(simulation engine, templates, code generation) and [OpenHydroTwin](https://github.com/ArashMassoudieh/OHTwin)
(cycle runner: advance, forecast, state snapshots, weather ingestion). It runs a calibrated, code-generated
OpenHydroQual kernel of a watershed on a schedule, drives it with radar rainfall for the recent past and weather
forecasts for the days ahead, and publishes the results to a browser map viewer.

The first watershed is Rock Creek (DC/MD), 54 sub-catchments modelled with Urban HRUs; the layout is meant
for further watersheds under `watersheds/`.

## Layout

```
OpenHydroTwin/        submodule: twin engine (kernel backend and forcing map are added there)
OpenHydroQual/        submodule: OpenHydroQual (templates, codegen); OpenHydroTwin/resources links to it
viewer/               map viewer: Qt Widgets compiled to WebAssembly (zoom, pan, layers, element buttons,
                      right-click time series of history and forecast)
deploy/               server configuration (nginx, systemd) and deploy script
docs/                 design notes
watersheds/<name>/
    gis/              GeoJSON layers (WGS84): sub-catchments, reaches, control points, boundary
    model/            operational model (.ohq), parameter set, kernel build script
    forcing/          rainfall (MRMS) and forecast (Open-Meteo) weights and bias factors
    config/           twin configuration (cycle, forecast horizon, forcing map, outputs)
```

Clone with submodules:

```sh
git clone --recurse-submodules https://github.com/ArashMassoudieh/OpenWatershedTwin.git
```

## Viewer

`viewer/` is the map viewer: Qt Widgets, built for the desktop or for WebAssembly (see `viewer/OWTViewer.pro`). It
reads the twin's static output files over HTTP (`docs/viewer_data.md`): sub-catchment polygons coloured by the
selected HRU element and variable, streams, gages; zoom (wheel, double-click), pan (drag), fit; a time slider through
the recent past and the forecast; right-click on a sub-catchment or a gage for history and forecast charts (rainfall,
element series, USGS observations) with CSV export. `viewer/sample/` holds sample outputs made from a Rock Creek model
run (`tools/make_sample_outputs.py`):

```sh
qmake viewer/OWTViewer.pro && make && ./OWTViewer --base viewer/sample            # desktop
source ~/emsdk/emsdk_env.sh && ~/Qt/6.8.2/wasm_singlethread/bin/qmake viewer/OWTViewer.pro && make
# serve OWTViewer.html/.js/.wasm, qtloader.js and the contents of viewer/sample from one folder
```

## Status

Engine: code-generated kernel backend and forcing map in OpenHydroTwin (branch `codegen-backend`); MRMS rainfall
feed (`tools/mrms_feed.py`). Viewer: first version, on sample data. See `docs/plan.md`.
