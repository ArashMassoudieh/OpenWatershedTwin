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

## Status

Planning and scaffolding; see `docs/plan.md`.
