#!/bin/bash
# =============================================================================
# OpenWatershedTwin — Rock Creek live twin, AWS deploy
#
# Usage:
#   deploy/deploy_rockcreek.sh [--with-state] [--fresh] [--no-venv]
#
#   --with-state  copy this machine's state/ (kernel snapshot + viewer history) and web/ outputs to the server
#                 (first deploy, or to replace the server's state with the local one). Stop the local engine
#                 first so the copy is consistent.
#   --fresh       wipe state/ on the server: the first cycle cold-starts at runtime.start_datetime and spins
#                 up to the present (about 5 minutes)
#   --no-venv     skip creating/updating the Python environment of the feeds (faster redeploys)
#   Without either state flag the server keeps the state it has.
#
# Page:  http://openhydrotwin.com/RockCreek/
#
# Isolation (the host also runs the DrywellDT twins and GreenInfraIQ): everything this script writes is in
#   /home/ubuntu/owt/            app/ (engine + Qt libs), repo/ (the parts of OpenWatershedTwin the twin uses),
#                                venv/ (Python for the feeds)
#   /var/www/owt/RockCreek/      the web folder (viewer + the engine's output files)
#   /etc/systemd/system/owt@.service              unit owt@RockCreek (never drywelldt@ or drywelldt-jm@)
#   /etc/nginx/ohtwin-locations/RockCreek.conf    included by the openhydrotwin server block (already has the
#                                                 `include ohtwin-locations/*.conf` line); nginx -t, rolled back
#                                                 on failure
# =============================================================================
set -e

EC2_USER="ubuntu"
EC2_HOST="52.42.223.42"                 # Elastic IP of openhydrotwin.com
PEM_FILE="${PEM_FILE:-/home/arash/Dropbox/AWS_/ArashLinux.pem}"
WATERSHED="RockCreek"
PAGE="RockCreek"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QT_LIB="/home/arash/Qt/6.8.2/gcc_64/lib"
QT_TLS="/home/arash/Qt/6.8.2/gcc_64/plugins/tls/libqopensslbackend.so"
OHTWIN="${REPO}/OpenHydroTwin/build-qmake/bin/OHTwin"
KERNEL_REL="build/kernels/RockCreek_v9_out/build/libRockCreekWY1819.so"
WASM_BUILD="${REPO}/build/viewer-wasm"
DEP_REL="watersheds/${WATERSHED}/deployments/live"

R_ROOT="/home/ubuntu/owt"
R_APP="${R_ROOT}/app"
R_REPO="${R_ROOT}/repo"
R_VENV="${R_ROOT}/venv"
R_WEB="/var/www/owt/${PAGE}"
NGINX_CONF="/etc/nginx/ohtwin-locations/${PAGE}.conf"

WITH_STATE=0; FRESH=0; VENV=1
for a in "$@"; do
    case "$a" in
        --with-state) WITH_STATE=1 ;;
        --fresh) FRESH=1 ;;
        --no-venv) VENV=0 ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown option $a" >&2; exit 1 ;;
    esac
done
[[ $WITH_STATE == 1 && $FRESH == 1 ]] && { echo "--with-state and --fresh exclude each other" >&2; exit 1; }

SSH=(ssh -o BatchMode=yes -i "${PEM_FILE}" "${EC2_USER}@${EC2_HOST}")
RSYNC=(rsync -az --info=stats1 -e "ssh -o BatchMode=yes -i ${PEM_FILE}")
log()  { echo -e "\033[0;32m[owt]\033[0m $1"; }
err()  { echo -e "\033[0;31m[owt]\033[0m $1" >&2; exit 1; }

# --- 0. pre-flight ---------------------------------------------------------------------------------------------
log "Pre-flight"
[[ -f "${PEM_FILE}" ]] || err "SSH key not found: ${PEM_FILE}"
for f in "${OHTWIN}" "${REPO}/${KERNEL_REL}" "${WASM_BUILD}/index.html" "${WASM_BUILD}/OWTViewer.js" \
         "${WASM_BUILD}/OWTViewer.wasm" "${WASM_BUILD}/qtloader.js" "${REPO}/${DEP_REL}/config.json"; do
    [[ -f "$f" ]] || err "missing: $f"
done
[[ -d "${REPO}/cache/mrms_history" ]] || err "missing the rain history cache/mrms_history (MRMS backfill)"
python3 - "${REPO}/${DEP_REL}/config.json" << 'PY' || err "config.json check failed"
import json, sys
c = json.load(open(sys.argv[1]))
bad = []
if c.get("solver", {}).get("backend") != "codegen": bad.append("solver.backend is not codegen")
if "assimilation" in c: bad.append("has an assimilation block")
if not c.get("runtime", {}).get("catch_up"): bad.append("runtime.catch_up is not set")
if not c.get("viewer"): bad.append("no viewer block")
print("\n".join(bad)); sys.exit(1 if bad else 0)
PY
if [[ $WITH_STATE == 1 ]] && pgrep -f "OHTwin -d .*${DEP_REL}" > /dev/null; then
    err "the local engine is running on ${DEP_REL}; stop it before copying its state (--with-state)"
fi

# --- 1. stage --------------------------------------------------------------------------------------------------
log "Staging"
B="$(mktemp -d /tmp/owt_${WATERSHED}_XXXX)"
mkdir -p "$B/app/bin" "$B/app/lib" "$B/app/plugins/tls" "$B/repo"
cp "${OHTWIN}" "$B/app/bin/"
for l in libQt6Core.so.6 libQt6Network.so.6 libicui18n.so.73 libicuuc.so.73 libicudata.so.73; do cp "${QT_LIB}/$l" "$B/app/lib/"; done
cp "${QT_TLS}" "$B/app/plugins/tls/"
cat > "$B/app/bin/run_owt.sh" << 'W'
#!/bin/bash
# systemd: run_owt.sh <watershed>
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LD_LIBRARY_PATH="${DIR}/../lib:${LD_LIBRARY_PATH}"
export QT_PLUGIN_PATH="${DIR}/../plugins"
# line-buffered, so the engine's progress reaches the journal as it happens (stdout is not a terminal here)
exec stdbuf -oL -eL "${DIR}/OHTwin" --deployment "/home/ubuntu/owt/repo/watersheds/$1/deployments/live"
W
chmod +x "$B/app/bin/run_owt.sh"
cat > "$B/owt@.service" << U
[Unit]
Description=OpenWatershedTwin live twin (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/owt/repo/watersheds/%i/deployments/live
Environment=OWT_PYTHON=${R_VENV}/bin/python
ExecStart=/home/ubuntu/owt/app/bin/run_owt.sh %i
Restart=on-failure
RestartSec=30
Nice=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=owt-%i

[Install]
WantedBy=multi-user.target
U
cat > "$B/${PAGE}.conf" << N
# OpenWatershedTwin ${WATERSHED} — generated by deploy/deploy_rockcreek.sh
location = /${PAGE} { return 301 /${PAGE}/; }
location /${PAGE}/ {
    alias ${R_WEB}/;
    index index.html;
    gzip on;
    gzip_comp_level 5;
    gzip_min_length 1024;
    gzip_types application/json application/wasm application/javascript text/javascript application/octet-stream image/svg+xml;
}
N
# the parts of the repository the twin uses, at the same relative paths (the configs use relative paths)
( cd "${REPO}" && tar -cf - \
    tools/mrms_feed.py tools/et_feed.py tools/usgs_feed.py tools/rockcreek_feeds.sh \
    "watersheds/${WATERSHED}/viewer_config.json" "watersheds/${WATERSHED}/kernel_outputs.txt" \
    "watersheds/${WATERSHED}/gis" "watersheds/${WATERSHED}/forcing" \
    "${DEP_REL}/forcing_map.json" "${KERNEL_REL}" cache/mrms_history ) | tar -xf - -C "$B/repo"
# server copy of config.json: web folder under /var/www, no model file (the kernel carries the model)
python3 - "${REPO}/${DEP_REL}/config.json" "$B/repo/${DEP_REL}/config.json" "${R_WEB}" << 'PY'
import json, sys
c = json.load(open(sys.argv[1]))
c["viewer"]["web_dir"] = sys.argv[3]
c["deployment"].pop("model_file", None)
c["deployment"]["name"] = c["deployment"]["name"] + "_aws"
json.dump(c, open(sys.argv[2], "w"), indent=1)
PY
cat > "$B/requirements.txt" << 'Q'
numpy
pandas
geopandas
shapely
pyproj
rasterio
requests
xarray
netCDF4
h5netcdf
scipy
pygridmet
Q

# --- 2. server: folders, stop only this twin ---------------------------------------------------------------------
log "Preparing the server (owt paths only)"
"${SSH[@]}" bash -s -- "${WATERSHED}" "${R_WEB}" << 'R'
set -e
mkdir -p /home/ubuntu/owt/app /home/ubuntu/owt/repo
sudo mkdir -p "$2" /etc/nginx/ohtwin-locations
sudo chown -R ubuntu:ubuntu /var/www/owt
sudo systemctl stop "owt@$1.service" 2>/dev/null || true
R

# --- 3. push ---------------------------------------------------------------------------------------------------
log "Pushing engine, repository parts and rain history"
"${RSYNC[@]}" "$B/app/" "${EC2_USER}@${EC2_HOST}:${R_APP}/"
"${RSYNC[@]}" "$B/repo/" "${EC2_USER}@${EC2_HOST}:${R_REPO}/"
"${RSYNC[@]}" "$B/requirements.txt" "$B/owt@.service" "$B/${PAGE}.conf" "${EC2_USER}@${EC2_HOST}:${R_ROOT}/"
# recent MRMS hours, so the first feed run does not download the whole 30-day window again
MONTHS="$(date -u +%Y%m) $(date -u -d '-1 month' +%Y%m)"
for m in $MONTHS; do
    [[ -d "${REPO}/cache/mrms/$m" ]] && "${RSYNC[@]}" "${REPO}/cache/mrms/$m" "${EC2_USER}@${EC2_HOST}:${R_REPO}/cache/mrms/"
done
[[ -d "${REPO}/cache/gridmet" ]] && "${RSYNC[@]}" "${REPO}/cache/gridmet" "${EC2_USER}@${EC2_HOST}:${R_REPO}/cache/"
# viewer
"${RSYNC[@]}" "${WASM_BUILD}/index.html" "${WASM_BUILD}/OWTViewer.js" "${WASM_BUILD}/OWTViewer.wasm" \
    "${WASM_BUILD}/qtloader.js" "${EC2_USER}@${EC2_HOST}:${R_WEB}/"
if [[ $WITH_STATE == 1 ]]; then
    log "Copying the local state and outputs"
    "${RSYNC[@]}" --delete "${REPO}/${DEP_REL}/state/" "${EC2_USER}@${EC2_HOST}:${R_REPO}/${DEP_REL}/state/"
    for d in forcing observations; do
        "${RSYNC[@]}" "${REPO}/${DEP_REL}/$d/" "${EC2_USER}@${EC2_HOST}:${R_REPO}/${DEP_REL}/$d/"
    done
    "${RSYNC[@]}" "${REPO}/${DEP_REL}/web/" "${EC2_USER}@${EC2_HOST}:${R_WEB}/"
fi
if [[ $FRESH == 1 ]]; then
    "${SSH[@]}" "rm -rf ${R_REPO}/${DEP_REL}/state ${R_REPO}/${DEP_REL}/outputs ${R_REPO}/${DEP_REL}/snapshots"
fi
rm -rf "$B"

# --- 4. Python environment of the feeds ----------------------------------------------------------------------------
if [[ $VENV == 1 ]]; then
    log "Python environment for the feeds (first time: a few minutes)"
    "${SSH[@]}" bash -s << 'R'
set -e
[[ -x /home/ubuntu/owt/venv/bin/python ]] || python3 -m venv /home/ubuntu/owt/venv
/home/ubuntu/owt/venv/bin/pip install -q --upgrade pip
/home/ubuntu/owt/venv/bin/pip install -q -r /home/ubuntu/owt/requirements.txt
/home/ubuntu/owt/venv/bin/python -c "import rasterio, geopandas, pygridmet, xarray; from osgeo import gdal" 2>/dev/null || true
/home/ubuntu/owt/venv/bin/python -c "import rasterio, geopandas, pygridmet, xarray; from rasterio.env import GDALVersion; import rasterio.drivers as d; print('feeds env ok, GDAL', rasterio.__gdal_version__)"
R
fi

# --- 5. check the engine can load on the server ---------------------------------------------------------------------
log "Checking the engine's libraries on the server"
"${SSH[@]}" "LD_LIBRARY_PATH=${R_APP}/lib ldd ${R_APP}/bin/OHTwin | grep 'not found' && exit 1 || echo 'all libraries found'"

# --- 6. systemd + nginx ------------------------------------------------------------------------------------------
log "Installing the service and the nginx location"
"${SSH[@]}" bash -s -- "${WATERSHED}" "${PAGE}" << 'R'
set -e
W="$1"; P="$2"; C="/etc/nginx/ohtwin-locations/$P.conf"
sudo cp /home/ubuntu/owt/owt@.service /etc/systemd/system/owt@.service
sudo systemctl daemon-reload
BAK=""
[[ -f "$C" ]] && { BAK="$C.bak.$(date +%Y%m%d%H%M%S)"; sudo cp "$C" "$BAK"; }
sudo cp "/home/ubuntu/owt/$P.conf" "$C"
if ! sudo nginx -t 2>/tmp/owt_nginx_test.log; then
    cat /tmp/owt_nginx_test.log
    if [[ -n "$BAK" ]]; then sudo mv "$BAK" "$C"; else sudo rm -f "$C"; fi
    echo "nginx -t failed: rolled back" >&2; exit 1
fi
sudo systemctl reload nginx
sudo systemctl enable "owt@$W.service" >/dev/null 2>&1
sudo systemctl restart "owt@$W.service"
sleep 5
systemctl --no-pager --lines=0 status "owt@$W.service" | head -4
echo "--- other twins:"
systemctl list-units --type=service --state=running --no-legend | grep -E "drywelldt" | awk '{print $1, $3, $4}'
R

log "Done.  Page: http://openhydrotwin.com/${PAGE}/"
log "Log:   ssh -i \"${PEM_FILE}\" ${EC2_USER}@${EC2_HOST} 'journalctl -u owt@${WATERSHED} -f'"
