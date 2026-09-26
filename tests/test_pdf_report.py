from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from scripts.generate_pdf_report import build_pdf


class PdfReportTests(unittest.TestCase):
    def test_builds_rejection_table_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / "decisions.geojsonl"
            plan_report = root / "plan.json"
            zone_report = root / "zones.json"
            verification = root / "verification.json"
            output = root / "report.pdf"
            rejected = {
                "type": "Feature",
                "properties": {
                    "candidate_id": "R-T-0042",
                    "plant_type": "tree",
                    "species": "Test tree",
                    "status": "rejected",
                    "rejection_reasons": ["Too close to an existing tree."],
                    "checks": [{
                        "code": "EXISTING_TREE_CLEARANCE",
                        "status": "failed",
                        "actual_distance_m": 2.0,
                        "required_distance_m": 5.0,
                        "explanation": "Too close to an existing tree.",
                    }],
                },
                "geometry": {"type": "Point", "coordinates": [10, 20]},
            }
            decisions.write_text(
                json.dumps(rejected, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            plan_report.write_text(json.dumps({
                "status": "passed",
                "summary": {
                    "auto_tree": {
                        "plant_type": "tree", "species": "Test tree",
                        "accepted_count": 3, "diagnostic_rejected_count": 1,
                    }
                },
                "point_placement_count": 3,
                "area_placement_count": 0,
                "manual_review_count": 0,
            }), encoding="utf-8")
            zone_report.write_text(json.dumps({
                "dxf_units_per_meter": 1,
                "plant_types": {
                    "tree": {
                        "allowed_area_in_dxf_square_units": 100,
                        "verification_status": "verified_by_available_rules",
                        "rules": [{"rule_code": "TREE_BUILDING_5", "status": "applied"}],
                    }
                },
            }), encoding="utf-8")
            verification.write_text(
                json.dumps({"status": "passed"}), encoding="utf-8"
            )

            actual = build_pdf(
                decisions, plan_report, zone_report, verification, output
            )

            self.assertTrue(actual.is_file())
            self.assertGreater(actual.stat().st_size, 1_000)
            reader = PdfReader(actual)
            self.assertGreaterEqual(len(reader.pages), 3)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("R-T-0042", text)
            self.assertIn("EXISTING_TREE_CLEARANCE", text)


if __name__ == "__main__":
    unittest.main()
