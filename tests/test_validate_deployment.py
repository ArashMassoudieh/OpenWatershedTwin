import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "tools/validate_deployment.py"
SPEC = importlib.util.spec_from_file_location("validate_deployment", PATH)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


class ValidatorTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(validator.duration_days("24hr"), 1)
        self.assertEqual(validator.duration_days("7day"), 7)
        self.assertAlmostEqual(validator.duration_days("60min"), 1 / 24)
        self.assertIsNone(validator.duration_days("seven days"))

    def test_bad_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weights.json"
            path.write_text(json.dumps({"units": {"SC01": [[0, 0, 0.4]]}}))
            findings = []
            validator.check_weights(path, {"SC01", "SC02"}, findings)
            codes = {finding.code for finding in findings}
            self.assertIn("MISSING_UNIT_WEIGHTS", codes)
            self.assertIn("WEIGHTS_NOT_NORMALIZED", codes)

    def test_version_ambiguity(self):
        config = {
            "deployment": {"model_file": "/models/calWY1819v9.ohq"},
            "solver": {"library": "/kernels/RockCreek_v9_out/lib.so", "parameter_set": "v11 LM"},
        }
        findings = []
        validator.check_versions(config, Path("config.json"), findings)
        self.assertIn("VERSION_AMBIGUITY", {finding.code for finding in findings})

    def test_flood_site_cross_check(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            root = repo / "watersheds" / "Example"
            service_dir = repo / "services" / "floodmap" / "sites"
            root.mkdir(parents=True)
            service_dir.mkdir(parents=True)
            (root / "sites.json").write_text(json.dumps({
                "shortrange": {"interval_min": 15, "horizon_h": 24, "processes": 2},
                "sites": {"gage": {
                    "trigger": {"link": "Q_gage", "threshold_cms": 10.0},
                    "members": [50, 90, 95],
                    "links": {"Q_gage": ["R1:flow"]},
                    "service": {"url": "http://127.0.0.1:8300", "site": "example"},
                }},
            }))
            (service_dir / "example.json").write_text(json.dumps({
                "window": {"trigger_link": "Q_other", "threshold_cms": 11.0},
            }))
            findings = []
            validator.check_flood_sites(repo, root, findings)
            codes = {finding.code for finding in findings}
            self.assertIn("TRIGGER_LINK_MISMATCH", codes)
            self.assertIn("TRIGGER_THRESHOLD_MISMATCH", codes)

    def test_flood_site_rejects_bad_members(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            root = repo / "watersheds" / "Example"
            root.mkdir(parents=True)
            (root / "sites.json").write_text(json.dumps({
                "shortrange": {"interval_min": 15, "horizon_h": 24, "processes": 2},
                "sites": {"gage": {
                    "trigger": {"link": "Q_gage", "threshold_cms": 10.0},
                    "members": [95, 50, 50],
                    "links": {"Q_gage": ["R1:flow"]},
                    "service": {"url": "http://127.0.0.1:8300"},
                }},
            }))
            findings = []
            validator.check_flood_sites(repo, root, findings)
            codes = {finding.code for finding in findings}
            self.assertIn("INVALID_TRIGGER_MEMBERS", codes)
            self.assertIn("MISSING_SERVICE_SITE", codes)

    def test_repository_has_no_structural_errors(self):
        repo = Path(__file__).resolve().parents[1]
        errors = [
            finding
            for watershed in validator.discover(repo)
            for finding in validator.validate_watershed(repo, watershed)
            if finding.severity == "ERROR"
        ]
        self.assertEqual(errors, [], "\n".join(map(str, errors)))


if __name__ == "__main__":
    unittest.main()
