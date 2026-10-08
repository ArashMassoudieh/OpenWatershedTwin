# Plan

Forecast only (no data assimilation in this phase), from a calibrated OpenHydroQual model.

## Engine (OpenHydroTwin, C++)
1. Kernel backend: load a code-generated kernel (.so), start or restart at any time from a saved state
   (`initialize_at`, `import_state` / `export_state`), feed forcing (`set_precipitation`, `set_series`), and run past
   the end date the kernel was compiled with. Checked against the interpreter on an existing twin.
2. Forcing map in the twin configuration instead of the single "Rain" source: each model source (for Rock Creek
   `P_SCnn`, and `ET_SCnn` / `ET2_SCnn` sharing one series per unit) gets a series built from area-weighted grid
   cells or a point; several sources may share a series.
3. Rainfall for the recent past from NOAA MRMS radar QPE (gauge-corrected), with a bias factor to the calibration
   forcing (AORC; MRMS is about 15 % drier at Rock Creek); forecast rainfall and reference ET from Open-Meteo, with
   bias factors from an overlap period.
4. Cycle every 6 hours, 7-day forecast: advance over the recent past and save the state, then forecast.
5. Viewer outputs (static JSON under `outputs/`): map state (per unit and element, now and forecast steps), one file
   per sub-catchment (history and forecast of every element), one per gage (model, USGS observations, flood stage),
   status.

## Viewer (Qt Widgets, WebAssembly)
Map drawn from the GeoJSON layers with its own projection (local flat projection about the watershed centre): zoom
and pan, sub-catchment polygons coloured by the selected element and variable, reaches by flow, gages, labels by
zoom level, hover tooltips. Buttons for the Urban HRU elements (Catchment, Impervious_Catchment, Reach, Soil_1-3,
Groundwater, Near_Stream_Aquifer) plus streams and gages. Right-click on a unit, reach or gage: history, forecast or
both, in a chart panel (rain bars, forecast shading, "now" line, CSV export). Time slider through the forecast.

## Rock Creek
Operational model: calibration attempt 9 parameters as a placeholder (to be replaced by later calibrations), with
outputs for every element of every unit; long run from 2012 to the present for the history and the initial state.

## Deployment
openhydrotwin.com (AWS), path `/RockCreek/`, nginx serving the outputs and the viewer, systemd timer.
