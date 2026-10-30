#!/usr/bin/env python3
"""Validate tracked OpenWatershedTwin deployment configuration.

Dependency-free by design. Generated kernels, models and live data are reported
separately because they are commonly ignored or stored outside this repository.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlparse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}
DURATION = re.compile(r"^(\d+(?:\.\d+)?)(min|hr|day)$")
VERSION = re.compile(r"v(\d+[a-z0-9]*)", re.I)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    location: str
    message: str


def load_json(path: Path, out: list[Finding]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        out.append(Finding("ERROR", "MISSING_FILE", str(path), "Required file does not exist"))
    except json.JSONDecodeError as exc:
        out.append(Finding("ERROR", "INVALID_JSON", str(path), f"Line {exc.lineno}, column {exc.colno}: {exc.msg}"))
    return None


def duration_days(value: Any) -> float | None:
    match = DURATION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    return float(match.group(1)) * {"min": 1 / 1440, "hr": 1 / 24, "day": 1}[match.group(2)]


def reference(base: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def check_reference(base: Path, value: Any, location: str, code: str, out: list[Finding], runtime: bool = False) -> Path | None:
    path = reference(base, value)
    if path is None:
        out.append(Finding("ERROR", "INVALID_PATH", location, "Expected a non-empty path"))
    elif not path.exists():
        severity = "INFO" if runtime else "ERROR"
        label = "Runtime artifact is absent from this checkout" if runtime else "Referenced file does not exist"
        out.append(Finding(severity, code, location, f"{label}: {path}"))
    return path


def geojson_ids(path: Path, field: str, out: list[Finding]) -> tuple[set[str], list[dict[str, Any]]]:
    data = load_json(path, out)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        if data is not None:
            out.append(Finding("ERROR", "INVALID_GEOJSON", str(path), "Expected a GeoJSON FeatureCollection"))
        return set(), []
    features = [feature for feature in data["features"] if isinstance(feature, dict)]
    values = [str(feature.get("properties", {}).get(field)) for feature in features if feature.get("properties", {}).get(field) is not None]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        out.append(Finding("ERROR", "DUPLICATE_ID", str(path), f"Duplicate IDs: {', '.join(duplicates)}"))
    return set(values), features


def check_weights(path: Path, units: set[str], out: list[Finding]) -> None:
    data = load_json(path, out)
    weights = data.get("units") if isinstance(data, dict) else None
    if not isinstance(weights, dict):
        if data is not None:
            out.append(Finding("ERROR", "INVALID_WEIGHTS", str(path), "Expected an object named 'units'"))
        return
    missing, extra = sorted(units - set(weights)), sorted(set(weights) - units)
    if missing:
        out.append(Finding("ERROR", "MISSING_UNIT_WEIGHTS", str(path), f"Units without weights: {', '.join(missing)}"))
    if extra:
        out.append(Finding("ERROR", "UNKNOWN_WEIGHT_UNITS", str(path), f"Unknown units: {', '.join(extra)}"))
    bad: list[str] = []
    for unit, rows in weights.items():
        try:
            total = sum(float(row[2]) for row in rows)
        except (TypeError, ValueError, IndexError):
            out.append(Finding("ERROR", "INVALID_WEIGHT_ROWS", str(path), f"Malformed weights for {unit}"))
            continue
        if abs(total - 1) > 1e-4:
            bad.append(f"{unit}={total:.6f}")
    if bad:
        out.append(Finding("ERROR", "WEIGHTS_NOT_NORMALIZED", str(path), f"Weights do not sum to 1: {', '.join(bad)}"))


def check_forcing_map(path: Path, units: set[str], out: list[Finding]) -> None:
    data = load_json(path, out)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        if data is not None:
            out.append(Finding("ERROR", "INVALID_FORCING_MAP", str(path), "Expected an 'entries' list"))
        return
    coverage = {"precipitation": set(), "et0": set()}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("variable") not in coverage:
            continue
        for target in entry.get("targets", []):
            match = re.search(r"SC\d+$", str(target.get("source", ""))) if isinstance(target, dict) else None
            if match:
                coverage[entry["variable"]].add(match.group())
    for variable, covered in coverage.items():
        missing, extra = sorted(units - covered), sorted(covered - units)
        if missing:
            out.append(Finding("ERROR", "INCOMPLETE_FORCING_MAP", str(path), f"{variable} missing for: {', '.join(missing)}"))
        if extra:
            out.append(Finding("ERROR", "UNKNOWN_FORCING_UNITS", str(path), f"{variable} has unknown units: {', '.join(extra)}"))


def check_versions(config: dict[str, Any], path: Path, out: list[Finding]) -> None:
    deployment, solver = config.get("deployment", {}), config.get("solver", {})
    values = (deployment.get("model_file"), solver.get("library"), solver.get("parameter_set"))
    if not all(isinstance(value, str) for value in values):
        return
    found = [VERSION.findall(value) for value in values]
    if not all(found):
        return
    model, kernel, parameters = found[0][-1].lower(), found[1][-1].lower(), found[2][0].lower()
    if model == kernel and parameters != model:
        out.append(Finding("WARNING", "VERSION_AMBIGUITY", str(path), f"Model and kernel appear to be {model}, but parameter_set is labelled {parameters}; verify compatibility"))


def check_model_package(root: Path, out: list[Finding]) -> None:
    path = root / "model_package.json"
    package = load_json(path, out)
    if not isinstance(package, dict):
        return
    paths = [("model_file", package.get("model_file")), ("templates_dir", package.get("templates_dir"))]
    paths += [(f"observation_dirs[{i}]", value) for i, value in enumerate(package.get("observation_dirs", []))]
    paths += [(f"extra_tables.{key}", value) for key, value in package.get("extra_tables", {}).items()]
    for field, value in paths:
        if isinstance(value, str) and Path(value).is_absolute():
            out.append(Finding("WARNING", "NONPORTABLE_PATH", f"{path}:{field}", f"Machine-specific absolute path: {value}"))
    for field, value in package.get("gis", {}).items():
        if field == "extra_layers" and isinstance(value, dict):
            for name, layer in value.items():
                check_reference(root, layer, f"{path}:gis.extra_layers.{name}", "MISSING_GIS", out)
        elif isinstance(value, str):
            check_reference(root, value, f"{path}:gis.{field}", "MISSING_GIS", out)
    check_reference(root, package.get("parameters_from"), f"{path}:parameters_from", "MISSING_PARAMETERS_SOURCE", out)


def check_flood_sites(repo: Path, root: Path, out: list[Finding]) -> None:
    """Cross-check the trigger registry against flood-service site definitions."""
    path = root / "sites.json"
    if not path.exists():
        return
    registry = load_json(path, out)
    sites = registry.get("sites") if isinstance(registry, dict) else None
    if not isinstance(sites, dict) or not sites:
        out.append(Finding("ERROR", "INVALID_SITE_REGISTRY", str(path), "Expected a non-empty 'sites' object"))
        return

    short = registry.get("shortrange", {})
    for field in ("interval_min", "horizon_h", "processes"):
        value = short.get(field) if isinstance(short, dict) else None
        if not isinstance(value, (int, float)) or value <= 0:
            out.append(Finding("ERROR", "INVALID_SHORTRANGE", f"{path}:shortrange.{field}", "Expected a positive number"))

    for name, site in sites.items():
        location = f"{path}:sites.{name}"
        if not isinstance(site, dict):
            out.append(Finding("ERROR", "INVALID_FLOOD_SITE", location, "Expected an object"))
            continue
        trigger = site.get("trigger", {})
        links = site.get("links", {})
        link = trigger.get("link") if isinstance(trigger, dict) else None
        threshold = trigger.get("threshold_cms") if isinstance(trigger, dict) else None
        if not isinstance(link, str) or link not in links:
            out.append(Finding("ERROR", "UNKNOWN_TRIGGER_LINK", f"{location}.trigger.link", f"Trigger link is absent from sites.{name}.links: {link!r}"))
        if not isinstance(threshold, (int, float)) or threshold <= 0:
            out.append(Finding("ERROR", "INVALID_TRIGGER_THRESHOLD", f"{location}.trigger.threshold_cms", "Expected a positive discharge"))

        members = site.get("members")
        if (not isinstance(members, list) or not members or
                any(not isinstance(value, (int, float)) or not 0 <= value <= 100 for value in members) or
                members != sorted(set(members))):
            out.append(Finding("ERROR", "INVALID_TRIGGER_MEMBERS", f"{location}.members", "Expected unique, increasing percentiles from 0 to 100"))

        service = site.get("service", {})
        service_name = service.get("site") if isinstance(service, dict) else None
        service_path = repo / "services" / "floodmap" / "sites" / f"{service_name}.json"
        if not isinstance(service_name, str) or not service_name:
            out.append(Finding("ERROR", "MISSING_SERVICE_SITE", f"{location}.service.site", "Expected a flood-service site name"))
            continue
        definition = load_json(service_path, out)
        if not isinstance(definition, dict):
            continue

        window = definition.get("window", {})
        if isinstance(window, dict):
            if link != window.get("trigger_link"):
                out.append(Finding("ERROR", "TRIGGER_LINK_MISMATCH", str(service_path), f"Registry uses {link!r}; service uses {window.get('trigger_link')!r}"))
            service_threshold = window.get("threshold_cms")
            if (isinstance(threshold, (int, float)) and isinstance(service_threshold, (int, float)) and
                    abs(float(threshold) - float(service_threshold)) > 1e-9):
                out.append(Finding("ERROR", "TRIGGER_THRESHOLD_MISMATCH", str(service_path), f"Registry uses {threshold}; service uses {service_threshold}"))
        else:
            out.append(Finding("ERROR", "INVALID_SERVICE_WINDOW", str(service_path), "Expected a 'window' object"))

        url = service.get("url") if isinstance(service, dict) else None
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            out.append(Finding("ERROR", "INVALID_SERVICE_URL", f"{location}.service.url", "Expected an http(s) URL"))

        for field in ("data_root", "dwe_binary"):
            value = definition.get(field)
            if isinstance(value, str) and Path(value).is_absolute():
                out.append(Finding("WARNING", "NONPORTABLE_PATH", f"{service_path}:{field}", f"Machine-specific absolute path: {value}"))


def validate_watershed(repo: Path, name: str) -> list[Finding]:
    out: list[Finding] = []
    root = repo / "watersheds" / name
    live = root / "deployments" / "live"
    config_path = live / "config.json"
    config = load_json(config_path, out)
    if not isinstance(config, dict):
        return out
    units, _ = geojson_ids(root / "gis" / "subcatchments.geojson", "id", out)
    reaches, _ = geojson_ids(root / "gis" / "reaches.geojson", "id", out)
    _, controls = geojson_ids(root / "gis" / "control_points.geojson", "id", out)
    if units != reaches:
        out.append(Finding("ERROR", "GIS_UNIT_MISMATCH", str(root / "gis"), f"Subcatchment-only: {sorted(units-reaches)}; reach-only: {sorted(reaches-units)}"))

    deployment, solver = config.get("deployment", {}), config.get("solver", {})
    forcing, viewer, runtime = config.get("forcing", {}), config.get("viewer", {}), config.get("runtime", {})
    if deployment.get("name") != f"{name}_live":
        out.append(Finding("WARNING", "DEPLOYMENT_NAME", str(config_path), f"Expected deployment.name '{name}_live'"))
    check_reference(live, solver.get("library"), f"{config_path}:solver.library", "MISSING_KERNEL", out, runtime=True)
    if deployment.get("model_file"):
        check_reference(live, deployment.get("model_file"), f"{config_path}:deployment.model_file", "MISSING_MODEL", out, runtime=True)
    if isinstance(solver.get("ensemble"), dict):
        check_reference(live, solver["ensemble"].get("file"), f"{config_path}:solver.ensemble.file", "MISSING_ENSEMBLE", out)
    forcing_map = check_reference(live, forcing.get("file"), f"{config_path}:forcing.file", "MISSING_FORCING_MAP", out)
    if forcing_map and forcing_map.exists():
        check_forcing_map(forcing_map, units, out)
    check_reference(live, viewer.get("config"), f"{config_path}:viewer.config", "MISSING_VIEWER_CONFIG", out)

    horizon = duration_days(runtime.get("forecast_horizon"))
    for field in ("forecast_horizon", "interval", "data_latency", "check_interval"):
        if duration_days(runtime.get(field)) is None:
            out.append(Finding("ERROR", "INVALID_DURATION", f"{config_path}:runtime.{field}", "Use min, hr, or day suffix"))

    mrms_path = root / "forcing" / "mrms_feed.json"
    mrms = load_json(mrms_path, out)
    if isinstance(mrms, dict):
        check_reference(mrms_path.parent, mrms.get("units_geojson"), f"{mrms_path}:units_geojson", "MISSING_UNITS_GEOJSON", out)
        weights = check_reference(mrms_path.parent, mrms.get("weights_file"), f"{mrms_path}:weights_file", "MISSING_WEIGHTS", out)
        if weights and weights.exists():
            check_weights(weights, units, out)
        if not isinstance(mrms.get("mrms_scale"), (int, float)) or mrms["mrms_scale"] <= 0:
            out.append(Finding("ERROR", "INVALID_SCALE", f"{mrms_path}:mrms_scale", "Scale must be positive"))
        days = mrms.get("openmeteo", {}).get("forecast_days") if isinstance(mrms.get("openmeteo"), dict) else None
        if horizon is not None and (not isinstance(days, (int, float)) or days < horizon):
            out.append(Finding("ERROR", "FORECAST_HORIZON", str(mrms_path), f"Open-Meteo forecast_days must cover {horizon:g} days"))

    et_path = root / "forcing" / "et_feed.json"
    et = load_json(et_path, out)
    if isinstance(et, dict):
        check_reference(et_path.parent, et.get("units_geojson"), f"{et_path}:units_geojson", "MISSING_UNITS_GEOJSON", out)
        days = et.get("openmeteo", {}).get("forecast_days") if isinstance(et.get("openmeteo"), dict) else None
        if horizon is not None and (not isinstance(days, (int, float)) or days < horizon):
            out.append(Finding("ERROR", "FORECAST_HORIZON", str(et_path), f"Open-Meteo forecast_days must cover {horizon:g} days"))

    usgs_path = root / "forcing" / "usgs_feed.json"
    usgs = load_json(usgs_path, out)
    if isinstance(usgs, dict):
        gages = usgs.get("gages")
        control_gages = {str(f.get("properties", {}).get("id")) for f in controls if f.get("properties", {}).get("kind") == "gage"}
        if not isinstance(gages, dict) or not gages:
            out.append(Finding("ERROR", "INVALID_GAGES", str(usgs_path), "Expected a non-empty 'gages' object"))
        elif set(gages) - control_gages:
            out.append(Finding("ERROR", "UNKNOWN_GAGES", str(usgs_path), f"Absent from control points: {', '.join(sorted(set(gages)-control_gages))}"))

    check_versions(config, config_path, out)
    check_model_package(root, out)
    check_flood_sites(repo, root, out)
    return sorted(out, key=lambda finding: (ORDER[finding.severity], finding.code, finding.location))


def discover(repo: Path) -> list[str]:
    return sorted(path.name for path in (repo / "watersheds").iterdir() if (path / "deployments/live/config.json").is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--watershed", action="append")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    args = parser.parse_args(argv)
    results = {name: validate_watershed(args.repo.resolve(), name) for name in (args.watershed or discover(args.repo.resolve()))}
    if args.json:
        print(json.dumps({name: [asdict(f) for f in findings] for name, findings in results.items()}, indent=2))
    else:
        for name, findings in results.items():
            counts = {severity: sum(f.severity == severity for f in findings) for severity in ORDER}
            print(f"{name}: {counts['ERROR']} error(s), {counts['WARNING']} warning(s), {counts['INFO']} info")
            for finding in findings:
                print(f"  {finding.severity:<7} {finding.code}: {finding.message} [{finding.location}]")
    errors = any(f.severity == "ERROR" for findings in results.values() for f in findings)
    warnings = any(f.severity == "WARNING" for findings in results.values() for f in findings)
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
