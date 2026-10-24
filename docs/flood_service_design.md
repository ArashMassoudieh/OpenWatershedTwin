# Forecast-triggered flood delineation: design (2026-10-24)

The twin runs a 200-member forecast continuously. When a member statistic at a registered site's trigger reach exceeds
the site's flow threshold, the twin sends a job to a separate flood-delineation microservice, which runs the 2D model
for the selected ensemble members and returns flood maps. The viewer shows them in a popup. First site: Sligo Creek at
Maple Ave (USGS 01650800 / NWS TKPM2). Background: Sligo Creek `flood2d/` (hindcasts, trigger replays, ensemble).

## Decisions (user, 2026-10-24)

| | |
|---|---|
| Ensemble cycle | every 15 min, rain from MRMS RadarOnly_QPE_15M (~5 min latency) then the forecast rain; 200 posterior members |
| Trigger | a flow threshold per site, set in the site registry |
| 2D runs | a separate microservice (C++, Crow, `~/Projects/crow`), receiving and answering HTTP requests; it can run on another server; general for many sites |
| Demo | published on another page; its URL is set in the site registry |

## Defaults chosen here (change any of them)

| | |
|---|---|
| Members mapped | the members at the 50th, 90th and 95th percentile of the forecast peak at the trigger reach (Rock Creek paper: rank by peak, never the pointwise envelope), each with one AR(1) structural-error series started from the residual observed at the gauge at the issue time |
| Trigger statistic | the site's `trigger.statistic`, default the ensemble median (p50) of the forecast peak flow (the paper's "central forecast") |
| Forecast horizon of the 15-min cycle | 24 h (flash floods); the 6-hourly 7-day forecast for the viewer stays as it is |
| Instant maps | while the 2D runs compute, the popup shows the pre-built 1 m library map for each member's forecast peak stage (`flood2d/library_1m`) |
| Repeat triggers | a new job for a site only if the selected members' peaks changed by > 10 % or the last job is > 1 h old |

## Components

1. **Twin engine (OHTwin, `OpenHydroTwin/`, branch codegen-backend).** A short-range cycle (`runtime.short_range`):
   every 15 min, each member advances from its saved state with the latest rain and forecasts 24 h. New: for the
   links named by the site registry, every member's flows are written each cycle to
   `outputs/members/<cycle>/<site>.csv` (time, member, one column per link). Cost: a member run is ~0.25 s per
   simulated day on one core, so 200 members x (15 min + 24 h) is ~1 min per cycle on the server's spare CPU.
2. **Rain feed.** `tools/mrms_feed.py` gains the RadarOnly_QPE_15M product at 15-min resolution
   (`flood2d/scripts/06c_fetch_mrms15.py` is the prototype) plus the Open-Meteo 15-min forecast after it.
3. **Trigger (`tools/flood_trigger.py`, run after each short-range cycle).** Reads the member flows, applies the AR(1)
   residual per member (members' `s_Q`, `tau_Q`), ranks members by peak at the site's trigger link, evaluates the
   threshold on `trigger.statistic`, writes `web/outputs/alerts/<site>.json` (forecast summary, band, percentile
   members, library maps) and, when triggered, POSTs a job to the flood service; it then polls the job and copies
   the returned products into `web/outputs/alerts/<site>/<issued>/`.
4. **Flood service (`services/floodmap/`, Crow).** REST API:
   - `POST /jobs` {site, issued, members: {p50: {link: [t, Q]...}, ...}} -> {job_id}
   - `GET /jobs/<id>` -> {state: queued|running|done|failed, progress, products}
   - `GET /jobs/<id>/files/<name>` -> a product (PNG, GeoTIFF, CSV, JSON, MP4)
   - `GET /sites` -> the sites the service can run (its own registry of 2D domains)
   A job queue with a configurable number of concurrent 2D runs; each job runs the site's pipeline
   (`run_job.py`: boundary hydrographs -> 2D-DWE (hot start from the site's base-flow library) -> products:
   max-depth PNG and GeoTIFF, arrival, impacts JSON, optional animation). The service keeps no state outside its
   job folders; it can run on any machine with 2D-DWE, Python and the site's terrain files.
5. **Site registry (`watersheds/<W>/sites.json`, shared by the twin and the service).** Per site:
   `name, label, trigger {link, threshold_cms, statistic, label}, links {edge: {link: fraction}, points: {...}},
   members [50, 90, 95], service {url, site}, library {index}, domain {grid_tag, bbox, dem, manning, sources},
   views, impacts {features, wet_depth_m}, demo_url, viewer {label, zoom_bbox}`.
6. **Viewer popup (`viewer/`).** `FloodAlertDialog` (QDialog): opened from an alert banner when
   `outputs/status.json` lists an active alert; tabs for the 50/90/95th-percentile maps (PNG fetched with a new
   `DataSource::getBytes`), the forecast band at the trigger reach, the impact list in US units, the lead time, and
   links to the animations and the demo page. Maps are rendered by the service, so the viewer needs no raster code.
7. **Demo.** A replay of 19 Jul 2025 through the same pipeline (archived 15-min radar, issue times every 15 min
   17:00-18:30 EDT) published at the registry's `demo_url`.

Decided 2026-10-24 as well:
- Service host: Home Linux for the demo; the target is an AWS node or another server. Only the registry's
  `service.url` changes.
- Build and test everything locally first. Deploy to the shared AWS host only with the user's go-ahead.
- The defaults above are accepted.
- The 15-min ensemble is a Python tool (`tools/shortrange_ensemble.py`) on the kernel's state export/import
  (`ohq_kernel_export_state` / `import_state`). It keeps member states in `state/shortrange/`, apart from the engine's
  6-hourly ensemble, so the engine needs no C++ change.

## Interfaces (contracts between the components)

### Site registry: `watersheds/<W>/sites.json`
```json
{"watershed": "SligoCreek",
 "sites": {"sligo_maple": {
   "label": "Sligo Creek at Maple Ave (USGS 01650800, NWS TKPM2)",
   "trigger": {"link": "G15-R19:flow", "threshold_cms": 50.43, "statistic": "p50",
               "label": "NWS TKPM2 minor stage 5.5 ft (1,781 cfs)"},
   "members": [50, 90, 95],
   "error_model": {"gauge_link": "G15-R19:flow", "usgs_site": "01650800"},
   "links": {"Q_inflow": ["R11-R13:flow"], "Q_sc13_local": ["SC13-R13-perv:flow", "SC13-R13-imp:flow", "SC13-R13-gw:flow"], "...": []},
   "service": {"url": "http://127.0.0.1:8300", "site": "sligo_core1m"},
   "library": "library_1m/index.csv",
   "demo_url": "http://openhydrotwin.com/SligoCreek-demo/",
   "viewer": {"label": "Maple Ave / Sligo Creek Parkway", "bbox_lonlat": [-77.012, 38.981, -76.999, 38.999]}}}}
```

### Flood service API (Crow, default port 8300)
- `GET /health` -> `{"ok": true, "version", "queued", "running"}`
- `GET /sites` -> `{"sites": {"<site>": {"label", "grid_m"}}}` (from the service's own `sites/` registry)
- `POST /jobs` with a JSON body
  `{"site": "<service site>", "issued_utc": "...", "requester": "...",
    "members": {"p50": {"t_utc": [...], "links": {"Q_inflow": [...], ...}}, "p90": {...}, "p95": {...}}}`
  -> `202 {"job_id"}`. The id is time based, e.g. `20250719T2135Z_sligo_core1m_ab12`.
- `GET /jobs` -> the list. `GET /jobs/<id>` -> the contents of the job's `status.json`.
- `GET /jobs/<id>/files/<path>` -> a file under the job folder's `out/`. Only names in `status.json` "products" are
  served; no path traversal.
- `DELETE /jobs/<id>` -> cancel (kill the runner's process group).
- The service writes `request.json` in a new job folder under its `jobs_dir` and runs
  `<runner> <job_dir>` (configurable, default `python3 pipeline/run_job.py`) with at most `max_concurrent` at a time
  (queue). The runner owns `status.json`.

### Job folder (written by the runner)
- `request.json`: the POSTed body.
- `status.json`: `{"state": "queued|running|done|failed", "progress": 0-1, "message", "started_utc",
  "finished_utc", "products": {"p50": {"max_depth_png": "p50/max_depth.png", "max_depth_tif": "...",
  "impacts": "p50/impacts.json"}, ...}}`.
- `out/<member>/...`: the products.

### Alert file (twin side): `web/outputs/alerts/<site>.json`, polled by the viewer
```json
{"site": "sligo_maple", "label": "...", "issued_utc": "...", "state": "none|watch|triggered|mapping|mapped",
 "statistic": "p50", "threshold_cms": 50.43,
 "forecast": {"peak_cms": {"p50": ..., "p90": ..., "p95": ...}, "peak_time_utc": {...},
              "band": {"t_utc": [...], "q5": [...], "q25": [...], "q50": [...], "q75": [...], "q95": [...]}},
 "maps": {"p50": {"source": "library|2d", "png": "alerts/sligo_maple/<issued>/p50_max_depth.png", "stage_ft": ...,
                  "impacts": {"Sligo Creek Parkway_ft": ..., "cross_streets": [...], "parking_lots": ..., ...}}, ...},
 "job": {"id", "state", "progress"}, "demo_url": "..."}
```
Optional alert keys the viewer uses: `trigger_label`, `viewer {label, bbox_lonlat}` (the map marker), `animations`
(and a per-member `animation`). Paths (`png`, animations) are relative to `web/outputs/`, or absolute http(s) URLs.
The list of active alerts is `web/outputs/alerts/index.json` `{"updated_utc", "alerts": ["sligo_maple", ...]}` (sites whose
state is not "none"). It is a separate file because the engine rewrites `status.json` every cycle. The viewer falls
back to a `status.json` "alerts" key.
