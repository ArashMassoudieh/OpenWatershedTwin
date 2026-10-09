# OpenWatershedTwin: handoff

Watershed-scale digital twin (forecast only, no data assimilation for now) built on OpenHydroQual (OHQ) and
OpenHydroTwin (OHTwin). First watershed: Rock Creek (DC/MD), 54 sub-catchments. Public repo
github.com/ArashMassoudieh/OpenWatershedTwin. The calibration of the Rock Creek model lives in a separate project
(`/home/arash/Dropbox/Rock Creek Model - OHQ`, its own session); this repo only consumes its model and parameters.

Background notes from the Rock Creek session (decisions, history, calibration results):

@/home/arash/.claude/projects/-home-arash-Dropbox-Rock-Creek-Model---OHQ/memory/rock-creek-ohq-model.md

## Working with the user (Arash Massoudieh)

- Confirms design decisions item by item; propose, wait, then implement. Explain what changes and why.
- Push to GitHub only when asked. OHQ changes stay backward compatible, documented, and must not break the Rock
  Creek or Turkey Branch models.
- `~/Projects/OpenHydroQual` (the main OHQ working copy) contains someone else's uncommitted change to
  `aquifolium/src/Utilities.cpp` (number parsing) plus two untracked files: never commit them and never build
  kernels or the generator from that working copy (build from the submodule here, see below).
- Do not rebuild a kernel or the OHQ-GA/OHQ-LM binaries while a GA/LM run uses them (the Rock Creek session runs GAs).
- Long sessions with many screenshots crashed Claude Code once: prefer reading files/logs over screenshots, keep
  tool outputs short.

## Layout

```
OpenHydroTwin/   submodule (github ArashMassoudieh/OHTwin), checked out on branch codegen-backend
OpenHydroQual/   submodule (github ArashMassoudieh/OpenHydroQual, master); jsoncpp is a NESTED submodule:
                 git submodule update --init --recursive
viewer/          Qt Widgets map viewer (desktop + WebAssembly); viewer/sample/ = sample outputs (NOT in git, regenerate:
                 README "Viewer"; kernel build/kernels/RockCreek_v9_out, model run cached in cache/sample_run_v9.npz)
tools/           mrms_feed.py (rain feed), twin_outputs.py (kernel output list from viewer_config.json; viewer sample
                 data from a kernel run), rockcreek_display_layers.py (combined-sewer mask, sliver removal),
                 make_sample_outputs.py (older state-based sample, superseded by twin_outputs.py)
docs/            plan.md, viewer_data.md (engine -> viewer data contract)
watersheds/RockCreek/
    gis/         GeoJSON (WGS84): subcatchments (id SC01..SC54, receiving_segment, downstream_unit, imperviousness),
                 reaches (segments), control_points (gages/dams, calibration flag), watershed_boundary
    forcing/     mrms_feed.json, mrms_weights.json (0.01 deg cell area weights per unit)
    deployments/ local test deployments (untracked): replay_test, iso_none, iso_rain, live/forcing (feed output)
build/           (ignored) ohtwin, ohqlib, codegen, kernels/RockCreek_v9, viewer-desktop, viewer-wasm, site
cache/           (ignored) mrms/ (hourly window cache .npz), mrms_history/ (backfill output), backfill log
```

## Done and verified

**Kernel backend in OHTwin** (branch `codegen-backend`, commit 36510d0, pushed, NOT merged to OHTwin main):
- `DTKernelModel` (loads a code-generated kernel through the model-independent `ohq_kernel_*` ABI,
  `OpenHydroQual/codegen/tools/ohq_kernel.h`; parameter overrides by name; state snapshots as JSON keyed by state
  name with the usual `_dt_*` keys), `DTForcing` (forcing map: one series per model source; providers `openmeteo`
  — batched multi-location, `precipitation` and `et0_fao_evapotranspiration`, past_days — and `csv`; weights,
  `scale`, coverage check), config blocks `solver` and `forcing` (see OHTwin README section "Code-generated kernel
  backend and forcing map"), `DTRunner::runKernelStages` (Advance + Forecast, existing `selected_output.csv` merge).
  Interpreter path unchanged and default; `viz_file` optional with codegen.
- Replay test (`watersheds/RockCreek/deployments/replay_test`): 60 daily cycles on the calibration forcing (54 rain +
  54 ET entries, csv provider) match a continuous run of the same kernel to 0.4 % RMS log flow (Turkey 1.5 %);
  about 1 s per simulated day.
- Kernel restart resets the time step to dt0: about 1.5 % event-timing noise per restart. Possible improvement:
  add the current dt to the kernel ABI (export/import) in OHQ codegen.
- Forcing replacement holds the last value beyond a supplied series: always supply the whole stage window.

**OHQ code-generator fix** (OHQ master 3f3f2b0, pushed): generated `setPrecipitation` now inserts the gap zeros of
`CPrecipitation::getflow` (sparse rain files gave 4-5x runoff before). Calibration kernels unaffected (they use
baked rain).

**MRMS feed** (`tools/mrms_feed.py`, commit bd61f40): NOAA MRMS MultiSensor_QPE_01H Pass2 (Pass1 for the latest hours,
re-fetched as Pass2) from the AWS bucket noaa-mrms-pds (from Nov 2020; Pass2 ~1-2 h behind, Pass1 ~20 min), grid
0.01 deg with origin 130W/55N (checked), Rock Creek window 38 x 25 cells; area-weighted per unit; Open-Meteo forecast
appended after the last MRMS hour; writes `rain_SCnn.csv` (`start,end,depth_m`, OHQ serials) + `rain_status.json`.
Check vs AORC 15 Aug-15 Sep 2021 (incl. Ida): hourly r 0.97 per unit, daily 0.99, AORC/MRMS totals 1.06, Ida 60 mm
both. `mrms_scale` = 1.08 (hourly product vs AORC, Oct 2024-Sep 2025, tools/mrms_bias.py; the earlier 1.15 came from the daily product).

**Viewer** (commit 3723037): `MapView` (own local projection, wheel zoom about cursor, drag pan, double-click zoom,
fit, hover tooltip, legend, labels by zoom), `ChartPanel` (QtCharts: rain on top, series, model vs USGS at gages,
forecast shading, now line, CSV export via QFileDialog::saveFileContent), `MainWindow` (element buttons from
`viewer_config.json`, variable box, time slider with play, right-click menus), `DataSource` (HTTP in the browser,
folder on desktop). Verified by off-screen screenshots on the desktop and by loading the WebAssembly build in a
browser (http.server on port 8765 serving build/site); click interaction in the browser not yet tested.

## Build commands

```sh
# OHTwin (engine)              -> OpenHydroTwin/build-qmake/bin/OHTwin
cd build/ohtwin && ~/Qt/6.8.2/gcc_64/bin/qmake ../../OpenHydroTwin/OHTwin.pro CONFIG+=release && make -j16
# OHQ core lib + code generator (from the CLEAN submodule; codegen CMake uses SYSTEM Qt 6.4 -> build ohqlib with qmake6)
cd build/ohqlib && qmake6 ../../OpenHydroQual/OHQLib/OHQLib.pro CONFIG+=release && make -j16
cmake -S OpenHydroQual/codegen -B build/codegen -DCMAKE_BUILD_TYPE=Release -DOHQLIB_DIR=$PWD/build/ohqlib
cmake --build build/codegen --target ohq_generate -j16
# a kernel
build/codegen/ohq_generate <model.ohq> OpenHydroQual/resources build/kernels/<Name> <Class> Storage --project shared
cmake -S build/kernels/<Name> -B build/kernels/<Name>/build -DCMAKE_BUILD_TYPE=Release && cmake --build ... -j16
# viewer, desktop (off-screen check: QT_QPA_PLATFORM=offscreen ./OWTViewer --base ../../viewer/sample
#                  --screenshot out.png --element Soil_2 --unit SC30 | --gage 01648000)
cd build/viewer-desktop && ~/Qt/6.8.2/gcc_64/bin/qmake ../../viewer/OWTViewer.pro && make -j16
# viewer, WebAssembly (emsdk 3.1.56 matches Qt 6.8.2)
source ~/emsdk/emsdk_env.sh && cd build/viewer-wasm && ~/Qt/6.8.2/wasm_singlethread/bin/qmake ../../viewer/OWTViewer.pro && make -j16
# site = index.html + OWTViewer.js/.wasm + qtloader.js + contents of a deployment web folder (or viewer/sample)
# MRMS feed
~/.venvs/rockcreek/bin/python tools/mrms_feed.py --config watersheds/RockCreek/forcing/mrms_feed.json
~/.venvs/rockcreek/bin/python tools/mrms_feed.py --config <cfg> --backfill 2024-10-01 2026-10-08 --no-forecast
```

Python: venv `~/.venvs/rockcreek` (geopandas, rasterio with GRIB2, requests, numpy, pandas).

## Rock Creek model for the twin

- Placeholder parameters: v9 (Riparian_Urban_HRU, near-stream aquifer). LM best set:
  p_Ksat 3.19747, p_Ksat_s 0.0144623, p_imp 0.777136, p_Kc 0.594368, p_Kc2 0.222728, p_gwKl 0.857675,
  p_gwpor 0.060568, p_nch 0.0402393, p_riser 0.846538, p_fr 0.0500814, p_nsSy 0.167966, p_nsK 0.998912.
  Will be replaced by later calibrations (attempt 11 GA running in the Rock Creek session).
- Model file: `/home/arash/Dropbox/Rock Creek Model - OHQ/model/calWY1819v9.ohq`; twin kernel (fixed generator):
  `build/kernels/RockCreek_v9`. Forcing sources: `P_SC01..54` (quantity `timeseries`), `ET_SCnn` and `ET2_SCnn`
  (quantity `ET_timeseries`, same reference-ET series, m/day). Kernel observations: Q/H at 01647850, 01648000,
  01648010 (+ 01648011 when present).
- AORC (calibration rain) ends 2025-10-01: history = AORC to then, MRMS after.

## Status (2026-10-09)

- Engine (OHTwin codegen-backend): DTViewerWriter writes the viewer files every cycle; runtime.catch_up /
  data_latency / check_interval / pre_cycle_command; csv forcing `files` lists (history + live window); Forecast
  starts from the Advance end state. Live deployment `watersheds/RockCreek/deployments/live` (config.json +
  forcing_map.json in git; web/, state/, forcing/, observations/ generated): cold start 2024-10-01, rain files
  [cache/mrms_history (backfill), forcing/ (live feed)], ET forcing/et_SCnn.csv, kernel
  build/kernels/RockCreek_v9_out (generated with --outputs watersheds/RockCreek/kernel_outputs.txt; v11 has the
  same structure as v9). Feeds: tools/rockcreek_feeds.sh = mrms_feed + et_feed (gridMET + Open-Meteo, ratio 1.00 on
  the overlap) + usgs_feed (NWIS IV; on 2026-10-09 Sherrill had no data for 30 days, Turkey flow = equipment
  malfunction). Test: live_test (cold start 2026-09-10, ignored by git) produced correct viewer files.
- Parameters: calibration attempt 11, Levenberg-Marquardt (final set; validation WY2020-25: main-stem flow NSE
  0.73/0.78, stage 0.82/0.80; Turkey Branch flow 0.72, not calibrated). Documentation: docs/RockCreek/
  rockcreek_model.pdf (built in the calibration project, docs/public/), linked from the viewer (config `links`).
- MRMS backfill 2024-10-01 .. 2026-10-08 (pid 3959101, output cache/mrms_history/) must finish before the live
  deployment's first (spin-up) cycle can run.

## Next steps (confirm with the user before each)

1. When the backfill is done: first live run (spin-up 2024-10-01 -> now in one catch-up cycle), check against USGS;
   (AORC/MRMS bias DONE 2026-10-09: tools/mrms_bias.py, overlap Oct 2024-Sep 2025, total 1.084, storms 1.02-1.08,
   light rain <0.5 mm/h 1.59 -> mrms_scale 1.08; cache/mrms_history rescaled from 1.15.)
2. DEPLOYED 2026-10-09: http://openhydrotwin.com/RockCreek/ (EC2 52.42.223.42, Ubuntu 24.04, 2 CPU, 3.7 GB, ~5 GB
   disk free; shared with the DrywellDT twins and GreenInfraIQ). deploy/deploy_rockcreek.sh: /home/ubuntu/owt/{app,repo,
   venv}, web /var/www/owt/RockCreek, unit owt@RockCreek (journalctl -u owt@RockCreek), nginx
   /etc/nginx/ohtwin-locations/RockCreek.conf (gzip). Installed python3.12-venv on the server. The LOCAL engine is
   stopped (the server runs the live twin); key /home/arash/Dropbox/AWS_/ArashLinux.pem. Plain HTTP (HTTPS = later,
   touches the shared server block).
3. (Browser right-click: works with popup() + fresh files; the reported freeze was most likely a cached old build.
   Requests now carry ?v=<ms> because the browser cache ignores Qt's AlwaysNetwork in WebAssembly.)
4. Viewer polish: smaller wasm (-Os), flood stage at Sherrill, DEM hillshade background.
5. Later: merge codegen-backend into OHTwin main, forecast skill tracking, ensemble rain, assimilation.
