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
