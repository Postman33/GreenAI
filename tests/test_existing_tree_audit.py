from __future__ import annotations

import unittest

from shapely.geometry import MultiPoint, Point, box

from src.rules.existing_tree_audit import audit_existing_trees


class ExistingTreeAuditTests(unittest.TestCase):
    def test_uses_clean_utility_geometry_and_project_units(self) -> None:
        for units in (1.0, 1000.0):
            with self.subTest(units=units):
                normalized = {
                    "work_boundary": box(-2 * units, -2 * units, 20 * units, 20 * units),
                    "existing_tree": MultiPoint([
                        (0, 0), (10 * units, 10 * units), (30 * units, 30 * units),
                    ]),
                    "water_pipe": Point(100 * units, 100 * units),
                }
                utilities = {"water_pipe": Point(1 * units, 0)}
                zone_report = {
                    "dxf_units_per_meter": units,
                    "plant_types": {"tree": {"rules": [
                        {
                            "rule_code": "TREE_WATER_2", "target_object_type": "water_pipe",
                            "check": "min_distance", "status": "applied", "min_distance_m": 2,
                            "norm_reference": "SP test, table 1",
                            "geometry_source": "reconstructed_high_confidence_geometry",
                        },
                        {
                            "rule_code": "TREE_WATER_REVIEW_3", "target_object_type": "water_pipe",
                            "check": "min_distance", "status": "applied", "min_distance_m": 3,
                            "norm_reference": "SP test, table 2",
                            "geometry_source": "reconstructed_high_confidence_geometry",
                        },
                    ]}}
                }
                features, report = audit_existing_trees(normalized, {}, utilities, zone_report)
                self.assertEqual(report["screened_tree_count"], 2)
                self.assertEqual(report["outside_work_boundary_count"], 1)
                self.assertEqual(report["conflict_tree_count"], 1)
                self.assertEqual(report["conflicts_by_target"]["water_pipe"], 1)
                self.assertEqual(report["conflicts_by_rule"]["TREE_WATER_2"], 1)
                self.assertEqual(report["conflicts_by_rule"]["TREE_WATER_REVIEW_3"], 1)
                conflict = next(item for item in features if item["properties"]["status"] == "conflict")
                self.assertEqual(conflict["properties"]["existing_tree_id"], "ET-0001")
                check = conflict["properties"]["checks"][0]
                self.assertAlmostEqual(check["actual_distance_m"], 1)
                self.assertEqual(check["nearest_target_point"], [units, 0.0])
                self.assertEqual(check["geometry_source"], "reconstructed_high_confidence_geometry")
                self.assertEqual(features[1]["properties"]["status"], "clear")

    def test_reports_unavailable_rule_without_declaring_all_trees_clear(self) -> None:
        normalized = {
            "work_boundary": box(-1, -1, 10, 10),
            "existing_tree": MultiPoint([(0, 0), (8, 8)]),
        }
        zone_report = {"plant_types": {"tree": {"rules": [
            {"rule_code": "TREE_GAS_1_5", "target_object_type": "gas_pipe",
             "check": "min_distance", "status": "unavailable", "min_distance_m": 1.5},
        ]}}}
        features, report = audit_existing_trees(normalized, {}, {}, zone_report)
        self.assertEqual(report["conflict_tree_count"], 0)
        self.assertEqual(report["incomplete_tree_count"], 2)
        self.assertEqual(report["unavailable_rule_codes"], ["TREE_GAS_1_5"])
        self.assertTrue(all(item["properties"]["status"] == "incomplete" for item in features))


if __name__ == "__main__":
    unittest.main()
