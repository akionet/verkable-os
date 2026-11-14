"""@OBS-001-L1-01: shipped licence text, metadata and attribution agree."""

import hashlib
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Canonical https://www.apache.org/licenses/LICENSE-2.0.txt, checked 2026-11-14.
APACHE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


class LicenseTest(unittest.TestCase):
    def test_canonical_apache_license(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(license_text.encode()).hexdigest(), APACHE_SHA256)

    def test_package_metadata_and_distribution_files(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(set(project["license-files"]), {"LICENSE", "NOTICE"})

    def test_readme_and_notice(self) -> None:
        section = (ROOT / "README.md").read_text(encoding="utf-8").split("## Licence", 1)[1]
        self.assertIn("Apache-2.0", section)
        self.assertNotIn("Not yet chosen", section)
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("Copyright 2026 Akio Cox", notice)
        self.assertIn("Verkable OS", notice)
