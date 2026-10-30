#!/usr/bin/env python3
"""Dummy flood-map runner for testing floodmap_service (Python stdlib only).

Usage: dummy_runner.py <job_dir>

Reads <job_dir>/request.json, writes <job_dir>/status.json (running -> progress -> done) and, per member,
out/<member>/impacts.json and out/<member>/max_depth.png (a tiny valid PNG), listed in status "products".

Test switches in the request body (ignored by the real pipeline):
  "sleep": seconds      run that long before finishing (to test DELETE)
  "ignore_term": true   ignore SIGTERM (to test the SIGKILL escalation)
  "fail": code          exit with that code without writing "done"
  "evil_product": true  also list "../request.json" as a product (the service must refuse to serve it)
"""
import json
import os
import signal
import struct
import sys
import time
import zlib
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def tiny_png(w=4, h=4, rgb=(30, 90, 200)):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    job = os.path.abspath(sys.argv[1])
    req = json.load(open(os.path.join(job, "request.json")))
    spath = os.path.join(job, "status.json")
    status = json.load(open(spath)) if os.path.exists(spath) else {}
    status.update(state="running", progress=0.0, message="dummy runner started", products={})
    status.setdefault("started_utc", now())
    write_json(spath, status)
    print("dummy runner:", job, "site", req.get("site"), "members", list(req["members"]), flush=True)

    if req.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "fail" in req:
        print("dummy runner: failing on request", flush=True)
        sys.exit(int(req["fail"]))

    sleep = float(req.get("sleep", 0))
    steps = 5
    for k in range(steps):
        time.sleep(sleep / steps if sleep > 0 else 0.2)
        status.update(progress=round((k + 1) / (steps + 1), 3), message=f"step {k + 1}/{steps}")
        write_json(spath, status)

    products = {}
    for m, mem in req["members"].items():
        d = os.path.join(job, "out", m)
        os.makedirs(d, exist_ok=True)
        peaks = {ln: max(v) for ln, v in mem["links"].items()}
        write_json(os.path.join(d, "impacts.json"), {"member": m, "peak_inflow_cms": peaks,
                                                     "n_steps": len(mem["t_utc"]), "dummy": True})
        with open(os.path.join(d, "max_depth.png"), "wb") as f:
            f.write(tiny_png())
        products[m] = {"max_depth_png": f"{m}/max_depth.png", "impacts": f"{m}/impacts.json"}
    if req.get("evil_product"):
        products["evil"] = {"secret": "../request.json"}
    status.update(state="done", progress=1.0, message="dummy run complete", finished_utc=now(), products=products)
    write_json(spath, status)
    print("dummy runner: done", flush=True)


if __name__ == "__main__":
    main()
