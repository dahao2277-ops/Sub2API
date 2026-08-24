from __future__ import annotations

import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("verify_gitleaks_classifications.py")
SPEC = importlib.util.spec_from_file_location("verify_gitleaks_classifications", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ManifestValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = MODULE_PATH.resolve().parents[1]
        cls.data = MODULE.load_manifest(
            cls.repository / "security" / "gitleaks-classifications.json"
        )

    def test_checked_in_manifest_and_exact_ignore_are_valid(self) -> None:
        counts = MODULE.validate_manifest(self.data, self.repository / ".gitleaksignore")
        self.assertEqual(counts["current_tree"], 46)
        self.assertEqual(counts["full_history"], 89)
        current_ids = {
            finding["finding_id"]
            for finding in self.data["findings"]
            if finding["source"] == "current_tree"
        }
        history_ids = {
            finding["finding_id"]
            for finding in self.data["findings"]
            if finding["source"] == "full_history"
        }
        self.assertEqual(current_ids, {f"CUR-{index:03d}" for index in range(1, 47)})
        self.assertEqual(history_ids, {f"HIST-{index:03d}" for index in range(1, 90)})

    def test_private_key_detections_are_only_exact_test_fixtures(self) -> None:
        private_keys = [
            finding for finding in self.data["findings"] if finding["rule_id"] == "private-key"
        ]
        self.assertEqual(len(private_keys), 6)
        self.assertTrue(
            all(finding["classification"] == "TEST_FIXTURE" for finding in private_keys)
        )
        self.assertTrue(all(finding["path"].endswith("_test.go") for finding in private_keys))
        ignored = {
            line.strip()
            for line in (self.repository / ".gitleaksignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        self.assertTrue(all(finding["fingerprint"] in ignored for finding in private_keys))

    def test_unknown_is_fail_closed(self) -> None:
        data = copy.deepcopy(self.data)
        data["findings"][0]["classification"] = "UNKNOWN"
        with self.assertRaises(MODULE.VerificationError):
            MODULE.validate_manifest(data, self.repository / ".gitleaksignore")

    def test_active_secret_is_fail_closed(self) -> None:
        data = copy.deepcopy(self.data)
        data["findings"][0]["classification"] = "REAL_ACTIVE_SECRET"
        with self.assertRaises(MODULE.VerificationError):
            MODULE.validate_manifest(data, self.repository / ".gitleaksignore")

    def test_broad_or_missing_ignore_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ignore = Path(directory) / ".gitleaksignore"
            ignore.write_text("backend/.*\n", encoding="utf-8")
            with self.assertRaises(MODULE.VerificationError):
                MODULE.validate_manifest(self.data, ignore)

    def test_raw_secret_fields_are_rejected(self) -> None:
        data = copy.deepcopy(self.data)
        data["findings"][0]["secret"] = "[REDACTED]"
        with self.assertRaises(MODULE.VerificationError):
            MODULE.validate_manifest(data, self.repository / ".gitleaksignore")


if __name__ == "__main__":
    unittest.main()
