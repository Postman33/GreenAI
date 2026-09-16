from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shapely.geometry import LineString, box, shape

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src import plant_allow_zone
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


class PlantAllowZoneTests(unittest.TestCase):
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
                [feature("building", LineString([(5, 0), (5, 10)]))],
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
            self.assertAlmostEqual(shape(zone["geometry"]).area, 80.0, places=5)
            self.assertEqual(zone["properties"]["verification_status"], "verified_by_available_rules")
            self.assertEqual(zone["properties"]["selectable_plants"][0]["name"], "Test tree")


if __name__ == "__main__":
    unittest.main()
