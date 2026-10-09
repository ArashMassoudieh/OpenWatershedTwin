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
element series, USGS observations) with CSV export. What the map and charts show is set per watershed in
`watersheds/<name>/viewer_config.json` (docs/viewer_data.md).

Sample outputs for the viewer (not in git, about 20 MB) come from a Rock Creek kernel built with outputs
(`ohq_generate --outputs`, list from `tools/twin_outputs.py spec`) run on its calibration forcing:

```sh
tools/twin_outputs.py spec --config watersheds/RockCreek/viewer_config.json --out watersheds/RockCreek/kernel_outputs.txt
tools/twin_outputs.py sample --config watersheds/RockCreek/viewer_config.json --kernel <libRockCreekWY1819.so> \
    --params "<calibrated parameters>" --rain-dir <forcing dir> --obs-dir <obs dir> --now 2019-07-01 \
    --out viewer/sample --cache cache/sample_run.npz
qmake viewer/OWTViewer.pro && make && ./OWTViewer --base viewer/sample            # desktop
source ~/emsdk/emsdk_env.sh && ~/Qt/6.8.2/wasm_singlethread/bin/qmake viewer/OWTViewer.pro && make
# serve index.html (copied from viewer/index.html by the build), OWTViewer.js/.wasm, qtloader.js and the
# contents of the deployment's web/ folder (or viewer/sample) from one folder; open the folder's URL
```

## Status

Engine: OpenHydroTwin (branch `codegen-backend`) with the code-generated kernel, forcing map, catch-up cycling and
the viewer-file writer. Feeds (`tools/rockcreek_feeds.sh`, run by the engine before each cycle): rainfall
(`mrms_feed.py`: MRMS radar QPE, Open-Meteo forecast), reference ET (`et_feed.py`: gridMET, Open-Meteo after it)
and USGS observations (`usgs_feed.py`). Rock Creek live deployment: `watersheds/RockCreek/deployments/live`
(6-hourly cycles, 7-day forecast, cold start 2024-10-01; parameters: calibration attempt 11, Levenberg-Marquardt).
Model documentation: `docs/RockCreek/rockcreek_model.pdf`.

```sh
OpenHydroTwin/build-qmake/bin/OHTwin -d watersheds/RockCreek/deployments/live
```
