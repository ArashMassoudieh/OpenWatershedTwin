# floodmap_service

A small C++17 HTTP job service (Crow) that runs flood-delineation jobs for the digital twin. The twin POSTs a job (a
site plus inflow hydrographs for a few ensemble members); the service queues it, runs an external runner per job and
serves the job status and products. Contract: `docs/flood_service_design.md` ("Flood service API", "Job folder").

The service keeps no state outside its job folders, knows nothing about 2D models (that is the runner's job), and can
run on any machine with the runner's dependencies.

## Layout

| | |
|---|---|
| `src/main.cpp` | the service |
| `CMakeLists.txt` | build (Crow include path + standalone asio) |
| `service.example.json` | example config (runner `python3 pipeline/run_job.py`) |
| `sites/` | the service's site registry: one `<site>.json` per 2D domain (at least `label`, `grid_m`) |
| `pipeline/dummy_runner.py` | stdlib-only test runner |
| `pipeline/run_job.py` | the real pipeline (separate task, not here) |
| `test/service_test.json`, `test/sites/test_site.json` | test config (port 8399, dummy runner, token `test-token`) |

## Build

```bash
sudo apt install libasio-dev            # standalone asio (header-only)
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
      -DCROW_INCLUDE_DIR=/home/arash/Projects/crow/include   # default; folder holding crow.h (Crow 1.1.1)
cmake --build build -j
```

## Run

```bash
cp service.example.json service.json    # edit
./build/floodmap_service --config service.json
```
Without `--config` the service reads `service.json` next to the binary.

## Config

| key | default | meaning |
|---|---|---|
| `host` | `0.0.0.0` | bind address (use `127.0.0.1` behind nginx or an SSH tunnel) |
| `port` | `8300` | |
| `jobs_dir` | `jobs` | one folder per job |
| `sites_dir` | `sites` | `<site>.json` files; a POST must name one of them |
| `workdir` | `.` | the runner's working directory |
| `runner` | `["python3", "pipeline/run_job.py"]` | command; the absolute job folder is appended as the last argument |
| `max_concurrent` | `1` | runners at a time (the rest wait in a FIFO queue) |
| `token` | none | if set, POST and DELETE need header `X-Floodmap-Token: <token>` |
| `requeue_on_start` | `false` | on start, re-queue jobs left `queued`/`running` instead of marking them failed |
| `kill_grace_s` | `10` | SIGTERM -> SIGKILL delay on cancel/stop |

Relative paths are resolved against the config file's folder.

## API

All responses are JSON except product files.

| | |
|---|---|
| `GET /health` | `{"ok": true, "version", "queued", "running"}` |
| `GET /sites` | `{"sites": {"<site>": {"label", "grid_m"}}}` |
| `POST /jobs` | body `{"site", "issued_utc", "requester", "members": {"p50": {"t_utc": [...], "links": {"Q_inflow": [...], ...}}, ...}}` -> `202 {"job_id", "state": "queued"}`; `400 {"error"}` if invalid; `401` without the token |
| `GET /jobs` | `{"jobs": [{"job_id", "site", "state", "progress", "message", "created_utc", "issued_utc", "started_utc", "finished_utc"}]}`, newest first; optional `?site=` and `?state=` |
| `GET /jobs/<id>` | the job's `status.json` as written |
| `GET /jobs/<id>/files/<path>` | a file under `<job>/out/`, only if `<path>` is a string value (at any depth) of `status.json` "products" |
| `DELETE /jobs/<id>` | queued: `200`, state `failed` ("cancelled"); running: `202` "cancelling" (SIGTERM to the process group, SIGKILL after `kill_grace_s`, then state `failed` "cancelled"); finished: `409`; unknown: `404` |

Validation of POST: `site` names an existing `sites/<site>.json`; `members` is a non-empty object whose names are
`[A-Za-z0-9_.-]`; each member has a non-empty string array `t_utc` and a non-empty object `links` whose values are
number arrays of the same length as `t_utc`. The body is stored unchanged as `request.json`, so extra fields pass
through to the runner.

Job id: `<UTC yyyymmddThhmmZ>_<site>_<4 hex>`, e.g. `20250719T2135Z_sligo_core1m_ab12`.

```bash
B=http://127.0.0.1:8300; T="X-Floodmap-Token: $TOKEN"
curl -s $B/health
curl -s $B/sites
curl -s -X POST -H "$T" -H 'Content-Type: application/json' $B/jobs -d '{
  "site": "sligo_core1m", "issued_utc": "2025-07-19T21:35Z", "requester": "twin",
  "members": {"p50": {"t_utc": ["2025-07-19T21:00Z", "2025-07-19T21:15Z"], "links": {"Q_inflow": [1.5, 40.2]}}}}'
curl -s $B/jobs
curl -s $B/jobs/20250719T2135Z_sligo_core1m_ab12
curl -s -o p50.png $B/jobs/20250719T2135Z_sligo_core1m_ab12/files/p50/max_depth.png
curl -s -X DELETE -H "$T" $B/jobs/20250719T2135Z_sligo_core1m_ab12
```

## Job lifecycle

1. POST: the service creates `<jobs_dir>/<id>/` with `request.json`, `out/` and `status.json`
   `{"job_id", "site", "state": "queued", "progress": 0, "message", "created_utc", "issued_utc", "products": {}}`.
2. A worker takes it: `status.json` -> `running` (`started_utc`), then the runner starts in its own session/process
   group (`setsid`) with stdin `/dev/null`, stdout+stderr appended to `<job>/runner.log`, cwd `workdir`.
3. The runner owns `status.json` while it runs (write it atomically: temp file + rename). It sets `state: "done"`
   and lists the products (paths relative to `out/`) when finished.
4. Runner exits: if `status.json` is not `done`, the service sets `failed` with `"runner exited with code N"` or
   `"runner killed by signal N"`, and `finished_utc`.
5. Service start: jobs left `queued`/`running` are marked `failed` ("service restarted"), or re-queued with
   `requeue_on_start`. A clean stop (SIGINT/SIGTERM) cancels the running runners first (`failed`, "service stopped").

Old job folders are never deleted by the service; prune them with cron (e.g. `find jobs -maxdepth 1 -mtime +30`).

## Runner contract

`<runner...> <job_dir>`; read `request.json`, write `status.json` (`state`, `progress` 0-1, `message`, `products`)
and the products under `out/<member>/`. The dummy runner (`pipeline/dummy_runner.py`) shows the minimum; its test
switches in the request: `sleep` (seconds), `ignore_term`, `fail` (exit code), `evil_product`.

## Deployment

systemd unit (`/etc/systemd/system/floodmap.service`):
```ini
[Unit]
Description=Flood-map job service
After=network.target

[Service]
User=floodmap
WorkingDirectory=/opt/floodmap
ExecStart=/opt/floodmap/build/floodmap_service --config /opt/floodmap/service.json
Restart=on-failure
KillMode=mixed           # SIGTERM to the service only; it stops its runners' process groups itself
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
`systemctl daemon-reload && systemctl enable --now floodmap`; logs: `journalctl -u floodmap`.

Behind nginx (bind the service to `127.0.0.1`):
```nginx
location /floodmap/ {
    proxy_pass http://127.0.0.1:8300/;
    proxy_read_timeout 60s;
    client_max_body_size 50m;     # job bodies with long hydrographs
}
```
Or from the twin host through an SSH tunnel: `ssh -N -L 8300:127.0.0.1:8300 user@floodhost`, and keep the registry's
`service.url` at `http://127.0.0.1:8300`.

## Security

- Bind to `127.0.0.1` unless the port must be reachable; put TLS (nginx) in front of anything public.
- Set `token` whenever the port is reachable by others: POST and DELETE require it (constant-time compare). GETs are
  open (status and products); restrict them with nginx if needed.
- Names (site, member, job id) are restricted to `[A-Za-z0-9_.-]`; only files listed in "products" are served, after
  `canonical()` resolution (symlinks and `..`) checked to stay under `<job>/out/`.
- The runner runs as the service user with the service's environment: run the service as an unprivileged user.
- No request-size limit in the service itself: set one in nginx (`client_max_body_size`).
