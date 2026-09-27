from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from shapely.geometry import LineString, MultiLineString, Point, box, shape

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.rules import plant_allow_zone
from tests.helpers import feature, write_jsonl


def rule(code: str, target: str, distance: float) -> dict:
    return {
        "rule_code": code,
        "plant_type": "tree",
        "target_object_type": target,
        "conditions": {"check": "min_distance", "min_distance_m": distance},
        "norm_reference": "TEST 1",
        "norm_document": {"code": "TEST"},
    }


def manual_rule(code: str, target: str) -> dict:
    return {
        "rule_code": code,
        "plant_type": "tree",
        "target_object_type": target,
        "conditions": {"check": "manual_review", "reason": "Confirm source legend"},
        "norm_reference": "TEST 1",
        "norm_document": {"code": "TEST"},
    }


class PlantAllowZoneTests(unittest.TestCase):
    def test_heat_protection_uses_pipe_and_full_chamber_without_consent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints = root / "constraints.jsonl"
            normalized = root / "normalized.jsonl"
            utilities = root / "utilities.jsonl"
            output = root / "zones.jsonl"
            report = root / "report.json"
            write_jsonl(constraints, [
                feature("base_allowed_area", box(0, 0, 20, 20)),
                feature("heat_chamber_full_footprints", box(2, 2, 4, 4)),
            ])
            write_jsonl(normalized, [feature("work_boundary", box(0, 0, 20, 20))])
            write_jsonl(utilities, [feature(
                "heat_pipe", LineString([(10, 0), (10, 20)]),
                decision="accepted", status="cleaned",
            )])
            one_metre = rule("SHRUB_HEAT_1", "heat_pipe", 1.0)
            three_metres = rule("SHRUB_HEAT_PROTECTION_3", "heat_pipe", 3.0)
            for item in (one_metre, three_metres):
                item["plant_type"] = "shrub"
            with patch.object(
                plant_allow_zone, "load_rules",
                return_value={"shrub": [one_metre, three_metres]},
            ), patch.object(plant_allow_zone, "load_plants", return_value={}):
                plant_allow_zone.build_plant_allow_zones(
                    constraints, normalized, output, report, "unused",
                    {"shrub"}, 1.0, utilities,
                )
            zone = shape(json.loads(output.read_text(encoding="utf-8"))["geometry"])
            self.assertFalse(zone.covers(Point(12, 10)))  # 2 m from line: consent needed
            self.assertFalse(zone.covers(Point(5.5, 3)))  # 1.5 m from chamber
            self.assertTrue(zone.covers(Point(14, 10)))
            self.assertTrue(zone.covers(Point(2, 10)))
            evaluations = json.loads(report.read_text(encoding="utf-8"))["plant_types"]["shrub"]["rules"]
            self.assertEqual([item["status"] for item in evaluations], ["applied", "applied"])
            self.assertIn("full_heat_chambers", evaluations[1]["geometry_source"])

    def test_load_rules_ignores_legacy_manual_rows(self) -> None:
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            (
                "TREE_BUILDING",
                "tree",
                "building",
                {"check": "min_distance", "min_distance_m": 5},
                "TEST 1",
                "TEST",
                "Test norm",
                "2026",
                None,
            ),
            (
                "TREE_SEWER_1_5",
                "tree",
                "sewer_pipe",
                {"check": "manual_review", "reason": "Inspect"},
                "TEST 1",
                "TEST",
                "Test norm",
                "2026",
                None,
            ),
        ]
        with patch.object(plant_allow_zone.psycopg, "connect", return_value=connection):
            loaded = plant_allow_zone.load_rules("unused", {"tree"})
        self.assertEqual([item["rule_code"] for item in loaded["tree"]], ["TREE_BUILDING"])

    def test_validate_distance_rule_rejects_bool_and_negative(self) -> None:
        with self.assertRaises(ValueError):
            plant_allow_zone.validate_distance_rule(
                {"rule_code": "A", "conditions": {"min_distance_m": True}}
            )
        with self.assertRaises(ValueError):
            plant_allow_zone.validate_distance_rule(
                {"rule_code": "B", "conditions": {"min_distance_m": -1}}
            )

    def test_apply_rules_subtracts_buffer(self) -> None:
        allowed, evaluations, warnings = plant_allow_zone.apply_rules(
            "tree",
            box(0, 0, 10, 10),
            {"building": LineString([(5, 0), (5, 10)])},
            [rule("TREE_BUILDING", "building", 1.0)],
            1.0,
            {},
            {"building": "normalized_raw_geometry"},
        )
        self.assertAlmostEqual(allowed.area, 80.0, places=5)
        self.assertEqual(evaluations[0]["status"], "applied")
        self.assertEqual(warnings, [])

    def test_raw_utility_rule_requires_cleaned_geometry(self) -> None:
        allowed, evaluations, warnings = plant_allow_zone.apply_rules(
            "tree",
            box(0, 0, 10, 10),
            {"water_pipe": LineString([(5, 0), (5, 10)])},
            [rule("TREE_WATER", "water_pipe", 2.0)],
            1.0,
            {},
            {"water_pipe": "normalized_raw_geometry"},
        )
        self.assertAlmostEqual(allowed.area, 100.0)
        self.assertEqual(evaluations[0]["status"], "manual_review")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(evaluations[0]["min_distance_m"], 2.0)

    def test_power_cable_rule_excludes_cleaned_geometry(self) -> None:
        allowed, evaluations, warnings = plant_allow_zone.apply_rules(
            "tree",
            box(0, 0, 10, 10),
            {"power_cable": LineString([(5, 0), (5, 10)])},
            [rule("TREE_POWER_CABLE_2", "power_cable", 2.0)],
            1.0,
            {},
            {"power_cable": "reconstructed_high_confidence_geometry"},
        )
        self.assertAlmostEqual(allowed.area, 60.0, places=5)
        self.assertEqual(evaluations[0]["status"], "applied")
        self.assertEqual(evaluations[0]["min_distance_m"], 2.0)
        self.assertEqual(warnings, [])

    def test_missing_geometry_keeps_required_distance_for_explanation(self) -> None:
        allowed, evaluations, _warnings = plant_allow_zone.apply_rules(
            "tree", box(0, 0, 10, 10), {},
            [rule("TREE_GAS_1_5", "gas_pipe", 1.5)], 1.0, {}, {},
        )
        self.assertAlmostEqual(allowed.area, 100.0)
        self.assertEqual(evaluations[0]["status"], "unavailable")
        self.assertEqual(evaluations[0]["min_distance_m"], 1.5)

    def test_reconstructed_gap_is_used_by_distance_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reconstructed = Path(directory) / "reconstructed.geojsonl"
            write_jsonl(
                reconstructed,
                [
                    feature(
                        "water_pipe",
                        MultiLineString(
                            [
                                [(1, 5), (4, 5)],
                                [(4, 5), (6, 5)],
                                [(6, 5), (9, 5)],
                            ]
                        ),
                        decision="accepted",
                        reason="accepted_with_reconstructed_gaps",
                        status="algorithmic_reconstruction",
                        source_part_count=2,
                        inferred_connection_count=1,
                    )
                ],
            )

            geometries, metadata = plant_allow_zone.load_utility_geometries(
                reconstructed
            )
            allowed, evaluations, warnings = plant_allow_zone.apply_rules(
                "tree",
                box(0, 0, 10, 10),
                geometries,
                [rule("TREE_WATER", "water_pipe", 0.5)],
                1.0,
                {},
                {"water_pipe": "reconstructed_high_confidence_geometry"},
                metadata,
            )

            self.assertFalse(allowed.covers(Point(5, 5)))
            self.assertEqual(evaluations[0]["status"], "applied")
            self.assertEqual(
                evaluations[0]["geometry_source"],
                "reconstructed_high_confidence_geometry",
            )
            self.assertEqual(
                evaluations[0]["source_geometry_metadata"][
                    "inferred_connection_count"
                ],
                1,
            )
            self.assertEqual(warnings, [])

    def test_build_plant_zones_without_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints = root / "constraints.jsonl"
            normalized = root / "normalized.jsonl"
            output = root / "zones.jsonl"
            report = root / "report.json"
            write_jsonl(constraints, [feature("base_allowed_area", box(0, 0, 10, 10))])
            write_jsonl(
                normalized,
                [
                    feature("building", LineString([(5, 0), (5, 10)])),
                    feature("sewer_pipe", LineString([(0, 5), (10, 5)])),
                ],
            )
            catalog = {
                "tree": [
                    {
                        "id": 1,
                        "name": "Test tree",
                        "plant_type": "tree",
                        "min_spacing_m": 3.0,
                        "selection_priority": 1,
                        "is_invasive": False,
                        "is_toxic": False,
                        "is_thorny": False,
                    }
                ]
            }
            with patch.object(
                plant_allow_zone,
                "load_rules",
                return_value={"tree": [rule("TREE_BUILDING", "building", 1.0)]},
            ), patch.object(plant_allow_zone, "load_plants", return_value=catalog):
                plant_allow_zone.build_plant_allow_zones(
                    constraints,
                    normalized,
                    output,
                    report,
                    "unused",
                    {"tree"},
                    1.0,
                )

            zone = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            report_data = json.loads(report.read_text(encoding="utf-8"))
            self.assertAlmostEqual(shape(zone["geometry"]).area, 80.0, places=5)
            self.assertEqual(zone["properties"]["verification_status"], "verified_by_available_rules")
            self.assertEqual(
                zone["properties"]["unchecked_utility_object_types"],
                ["sewer_pipe"],
            )
            self.assertEqual(
                report_data["plant_types"]["tree"]["verification_scope"],
                "configured_rules_only",
            )
            self.assertIn(
                "sewer_pipe", report_data["plant_types"]["tree"]["warnings"][0]
            )
            self.assertEqual(zone["properties"]["selectable_plants"][0]["name"], "Test tree")
            self.assertEqual(report_data["plant_catalog"]["tree"][0]["name"], "Test tree")

    def test_excludes_seasonal_only_plant_from_moscow_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints = root / "constraints.jsonl"
            normalized = root / "normalized.jsonl"
            output = root / "zones.jsonl"
            report = root / "report.json"
            write_jsonl(constraints, [feature("base_allowed_area", box(0, 0, 10, 10))])
            write_jsonl(normalized, [])
            catalog = {
                "herbaceous": [
                    {
                        "id": 1, "name": "Hardy perennial", "plant_type": "herbaceous",
                        "min_spacing_m": 0.5, "selection_priority": 1,
                        "climate_suitability": "recommended", "is_invasive": False,
                        "is_toxic": False, "is_thorny": False,
                    },
                    {
                        "id": 2, "name": "Summer-only plant", "plant_type": "herbaceous",
                        "min_spacing_m": 0.5, "selection_priority": 2,
                        "climate_suitability": "seasonal_only", "is_invasive": False,
                        "is_toxic": False, "is_thorny": False,
                    },
                ]
            }
            with patch.object(
                plant_allow_zone, "load_rules", return_value={"herbaceous": []}
            ), patch.object(plant_allow_zone, "load_plants", return_value=catalog):
                plant_allow_zone.build_plant_allow_zones(
                    constraints, normalized, output, report,
                    "unused", {"herbaceous"}, 1.0,
                )

            report_data = json.loads(report.read_text(encoding="utf-8"))
            plant_report = report_data["plant_types"]["herbaceous"]["plant_catalog"]
            self.assertEqual(
                [item["name"] for item in plant_report["selectable"]],
                ["Hardy perennial"],
            )
            self.assertEqual(
                [item["name"] for item in plant_report["excluded_not_winter_hardy"]],
                ["Summer-only plant"],
            )
            self.assertEqual(plant_report["target_hardiness_zone"], 4)

    def test_build_applies_reconstructed_sidewalk_setback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints = root / "constraints.jsonl"
            normalized = root / "normalized.jsonl"
            output = root / "zones.jsonl"
            report = root / "report.json"
            write_jsonl(constraints, [
                feature("base_allowed_area", box(0, 0, 10, 10)),
                feature("sidewalk_area", box(4, 0, 6, 10)),
            ])
            write_jsonl(normalized, [])
            with patch.object(
                plant_allow_zone,
                "load_rules",
                return_value={"tree": [rule("TREE_SIDEWALK", "sidewalk", 0.5)]},
            ), patch.object(plant_allow_zone, "load_plants", return_value={}):
                plant_allow_zone.build_plant_allow_zones(
                    constraints, normalized, output, report,
                    "unused", {"tree"}, 1.0,
                )
            zone = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            evaluation = json.loads(report.read_text(encoding="utf-8"))[
                "plant_types"
            ]["tree"]["rules"][0]
            self.assertAlmostEqual(shape(zone["geometry"]).area, 70.0)
            self.assertEqual(evaluation["status"], "applied")
            self.assertEqual(evaluation["geometry_source"], "reconstructed_sidewalk_area")

    def test_distant_sheet_sidewalk_does_not_pass_setback_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints = root / "constraints.jsonl"
            normalized = root / "normalized.jsonl"
            output = root / "zones.jsonl"
            report = root / "report.json"
            write_jsonl(constraints, [feature("base_allowed_area", box(0, 0, 10, 10))])
            write_jsonl(normalized, [
                feature("work_boundary", box(0, 0, 10, 10)),
                feature("sidewalk", box(1000, 1000, 1010, 1010)),
            ])
            with patch.object(
                plant_allow_zone, "load_rules",
                return_value={"tree": [rule("TREE_SIDEWALK", "sidewalk", 0.5)]},
            ), patch.object(plant_allow_zone, "load_plants", return_value={}):
                plant_allow_zone.build_plant_allow_zones(
                    constraints, normalized, output, report, "unused", {"tree"}, 1.0,
                )
            evaluation = json.loads(report.read_text(encoding="utf-8"))[
                "plant_types"
            ]["tree"]["rules"][0]
            self.assertNotEqual(evaluation["status"], "applied")
            self.assertAlmostEqual(
                shape(json.loads(output.read_text(encoding="utf-8"))["geometry"]).area,
                100.0,
            )

    def test_manual_utility_rule_reports_reconstructed_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints = root / "constraints.jsonl"
            normalized = root / "normalized.jsonl"
            reconstructed = root / "reconstructed.jsonl"
            output = root / "zones.jsonl"
            report = root / "report.json"
            write_jsonl(constraints, [feature("base_allowed_area", box(0, 0, 10, 10))])
            write_jsonl(
                normalized,
                [feature("overhead_power_line", LineString([(0, 0), (1, 0)]))],
            )
            write_jsonl(
                reconstructed,
                [
                    feature(
                        "overhead_power_line",
                        LineString([(2, 5), (8, 5)]),
                        decision="accepted",
                        reason="reconstructed_from_reciprocal_arrow_evidence",
                        status="algorithmic_reconstruction",
                        manual_review_required=True,
                        source_part_count=2,
                        inferred_connection_count=1,
                    )
                ],
            )
            catalog = {
                "tree": [
                    {
                        "id": 1,
                        "name": "Test tree",
                        "plant_type": "tree",
                        "min_spacing_m": 3.0,
                        "selection_priority": 1,
                        "is_invasive": False,
                        "is_toxic": False,
                        "is_thorny": False,
                    }
                ]
            }
            with patch.object(
                plant_allow_zone,
                "load_rules",
                return_value={"tree": [manual_rule("TREE_LEP", "overhead_power_line")]},
            ), patch.object(plant_allow_zone, "load_plants", return_value=catalog):
                plant_allow_zone.build_plant_allow_zones(
                    constraints,
                    normalized,
                    output,
                    report,
                    "unused",
                    {"tree"},
                    1.0,
                    reconstructed,
                )

            report_data = json.loads(report.read_text(encoding="utf-8"))
            evaluation = report_data["plant_types"]["tree"]["rules"][0]
            self.assertEqual(evaluation["status"], "manual_review")
            self.assertEqual(
                evaluation["geometry_source"],
                "reconstructed_review_geometry",
            )
            self.assertEqual(evaluation["source_geometry_type"], "LineString")


if __name__ == "__main__":
    unittest.main()
