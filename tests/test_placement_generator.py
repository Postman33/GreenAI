from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import MultiPoint, Point, box, mapping, shape

from src.planting import placement_generator


def write_jsonl(path: Path, features: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(feature, ensure_ascii=False) + "\n" for feature in features),
        encoding="utf-8",
    )


class PlacementGeneratorTests(unittest.TestCase):
    def test_sidewalk_check_uses_site_geometry_instead_of_distant_sheet_hatch(self) -> None:
        normalized = {
            "work_boundary": box(0, 0, 10, 10),
            "sidewalk": box(1000, 1000, 1010, 1010),
        }
        constraints = {"sidewalk_area": box(4, 0, 6, 10)}
        report = {"plant_types": {"tree": {"rules": [
            {"target_object_type": "sidewalk", "min_distance_m": 0.5},
        ]}}}
        placement_generator.prepare_sidewalk_for_checks(normalized, constraints, report, 1.0)
        self.assertAlmostEqual(normalized["sidewalk"].distance(Point(2, 5)), 2.0)
        self.assertLess(normalized["sidewalk"].area, 100)

    def test_layout_audit_replays_winner_and_explains_discarded_points(self) -> None:
        profile = placement_generator.PlantingProfile(
            plant_type="tree", species="Test tree", spacing_m=5.0,
            footprint_radius_m=2.5, symbol_radius_m=2.5,
            avoid_other_plantings_m=0.0, max_count=2,
            catalog_reference="test", selection_reasons=(),
        )
        trace: dict = {}
        points = placement_generator.best_component_layout(
            box(0, 0, 30, 20), profile, {"tree": profile}, [], 2, trace=trace,
        )
        self.assertEqual(len(points), 2)
        self.assertEqual(len(trace["variants"]), 96)
        self.assertEqual(trace["winner"]["accepted_count"], len(points))
        self.assertTrue(any(item["reason"] == "max_count"
                            for item in trace["winning_grid_rejections"]))
        self.assertEqual(trace["objective"], "maximum accepted count; first variant wins ties")

        audit: list[dict] = []
        packed = placement_generator.pack_candidates(
            [(0, 0), (1, 0), (6, 0)], profile, {"tree": profile}, [], 3, audit=audit,
        )
        self.assertEqual(packed, [(0, 0), (6, 0)])
        self.assertEqual(audit[0]["reason"], "spacing")
        self.assertAlmostEqual(audit[0]["actual_distance"], 1.0)
        self.assertAlmostEqual(audit[0]["required_distance"], 5.0)

    def test_linear_layout_keeps_one_row_phase_across_an_obstacle(self) -> None:
        profile = placement_generator.PlantingProfile(
            plant_type="tree", species="Test tree", spacing_m=5.0,
            footprint_radius_m=2.5, symbol_radius_m=2.5,
            avoid_other_plantings_m=0.0, max_count=100,
            catalog_reference="test", selection_reasons=(),
        )
        band = box(0, 0, 50, 3)
        safe = band.difference(box(20, -1, 25, 4))
        points = placement_generator.best_linear_layout(
            safe, band, profile, {"tree": profile}, [], 100
        )
        self.assertIsNotNone(points)
        self.assertGreaterEqual(len(points), 7)
        self.assertEqual(len({round(y, 7) for _x, y in points}), 1)
        self.assertTrue(all(safe.covers(Point(x, y)) for x, y in points))
        self.assertTrue(all(not 20 < x < 25 for x, _y in points))
        self.assertIsNone(
            placement_generator.best_linear_layout(
                box(0, 0, 20, 20), box(0, 0, 20, 20),
                profile, {"tree": profile}, [], 100,
            )
        )
        bent = box(0, 0, 5, 35).union(box(0, 0, 30, 5))
        self.assertIsNone(placement_generator.linear_reference(bent, 5.0))

    def test_generates_spaced_points_and_protects_existing_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zones = root / "zones.geojsonl"
            zone_report = root / "zones_report.json"
            normalized = root / "normalized.geojsonl"
            constraints = root / "constraints.geojsonl"
            utilities = root / "utilities.geojsonl"
            config = root / "config.json"
            output = root / "plan.geojsonl"
            report = root / "plan_report.json"
            write_jsonl(zones, [{
                "type": "Feature",
                "properties": {
                    "object_type": "plant_allow_zone",
                    "plant_type": "tree",
                    "verification_status": "verified_by_available_rules",
                },
                "geometry": mapping(box(0, 0, 20, 20)),
            }])
            write_jsonl(normalized, [{
                "type": "Feature",
                "properties": {"object_type": "existing_tree"},
                "geometry": mapping(MultiPoint([(10, 10)])),
            }])
            write_jsonl(constraints, [{
                "type": "Feature",
                "properties": {"object_type": "base_allowed_area"},
                "geometry": mapping(box(0, 0, 20, 20)),
            }])
            utilities.write_text("", encoding="utf-8")
            zone_report.write_text(json.dumps({
                "dxf_units_per_meter": 1.0,
                "plant_types": {"tree": {"rules": []}},
            }), encoding="utf-8")
            config.write_text(json.dumps({
                "dxfUnitsPerMeter": 1.0,
                "existingTreeCanopyRadiusM": 2.5,
                "existingTreeClearanceM": 2.0,
                "maxPlacementsPerType": 100,
                "plantingProfiles": [{
                    "plantType": "tree",
                    "species": "Test tree",
                    "geometryKind": "point",
                    "spacingM": 6.0,
                    "footprintRadiusM": 2.5,
                    "symbolRadiusM": 2.5,
                    "avoidOtherPlantingsM": 2.0,
                    "catalogReference": "catalog:test",
                    "selectionReasons": ["test"],
                }],
            }), encoding="utf-8")

            result = placement_generator.generate_plan(
                zones, zone_report, normalized, constraints, utilities, config, output, report
            )

            features = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            points = [shape(item["geometry"]) for item in features]
            existing = shape(mapping(MultiPoint([(10, 10)])))
            self.assertGreater(len(points), 0)
            self.assertEqual(result["failed_check_count"], 0)
            self.assertEqual(result["unique_id_count"], len(features))
            for point in points:
                self.assertGreaterEqual(point.distance(existing), 2.0 - 1e-7)
                self.assertTrue(box(0, 0, 20, 20).buffer(-2.5).covers(point))
            for index, point in enumerate(points):
                for other in points[index + 1:]:
                    self.assertGreaterEqual(point.distance(other), 6.0 - 1e-7)


if __name__ == "__main__":
    unittest.main()
