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
            plan = root / "planting_plan.geojsonl"
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
            plantings = [
                {
                    "type": "Feature", "id": "T-0001",
                    "properties": {
                        "planting_id": "T-0001", "plant_type": "tree",
                        "species": "Test tree", "status": "manual_review",
                        "layout_style": "linear",
                        "species_selection": {"source": "preset_profile"},
                        "checks": [
                            {
                                "code": "TREE_BUILDING_5", "target": "building", "status": "passed",
                                "actual_distance_m": 6.2, "required_distance_m": 5.0,
                                "norm_reference": "СП 42.13330.2026, таблица 6.3",
                            },
                            {
                                "code": "TREE_GAS_1_5", "status": "manual_review",
                                "norm_reference": "СП 42.13330.2026, таблица 6.3",
                            },
                        ],
                    },
                    "geometry": {"type": "Point", "coordinates": [12, 24]},
                },
                {
                    "type": "Feature", "id": "H-0001",
                    "properties": {
                        "planting_id": "H-0001", "plant_type": "herbaceous",
                        "species": "Test grass", "status": "accepted",
                        "dxf_units_per_meter": 1,
                        "layout_style": "safe_zone_cover",
                        "species_selection": {"source": "explicit_request"},
                        "checks": [{
                            "code": "HERBACEOUS_NPA_CLASSIFICATION", "status": "passed",
                            "norm_reference": "Постановление Правительства Москвы № 743-ПП, Правила, п. 2.1.13",
                        }],
                    },
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]},
                },
            ]
            plan.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n"
                                    for item in plantings), encoding="utf-8")
            plan_report.write_text(json.dumps({
                "status": "passed",
                "feature_count": 2,
                "summary": {
                    "auto_tree": {
                        "plant_type": "tree", "species": "Test tree",
                        "accepted_count": 3, "diagnostic_rejected_count": 1,
                    }
                },
                "point_placement_count": 1,
                "area_placement_count": 1,
                "manual_review_count": 1,
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
                decisions, plan_report, zone_report, verification, output,
                plan_path=plan,
            )

            self.assertTrue(actual.is_file())
            self.assertGreater(actual.stat().st_size, 1_000)
            reader = PdfReader(actual)
            self.assertGreaterEqual(len(reader.pages), 3)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("R-T-0042", text)
            self.assertIn("EXISTING_TREE_CLEARANCE", text)
            self.assertIn("T-0001", text)
            self.assertIn("H-0001", text)
            self.assertIn("таблица 6.3", text)
            self.assertIn("п. 2.1.13", " ".join(text.split()))
            self.assertIn("6.20 / 5.00", text)
            self.assertIn("Требуется ручная проверка: газопровод (TREE_GAS_1_5)", text)
            links = [annotation.get_object().get("/A", {}).get("/URI")
                     for page in reader.pages for annotation in page.get("/Annots", [])]
            self.assertIn("https://protect.gost.ru/sp/details/f6917ab4-63d8-4ecb-9794-0b4990ba3b99", links)
            self.assertIn("https://www.mos.ru/upload/documents/files/7389/Postanovlenie743-PP.pdf", links)

            plantings[1]["properties"]["checks"][0]["norm_reference"] = "project catalog"
            plan.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n"
                                    for item in plantings), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "H-0001 has no NPA reference"):
                build_pdf(decisions, plan_report, zone_report, verification,
                          root / "invalid.pdf", plan_path=plan)


if __name__ == "__main__":
    unittest.main()
