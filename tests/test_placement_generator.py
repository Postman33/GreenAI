from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import MultiPoint, box, mapping, shape

from src import placement_generator


def write_jsonl(path: Path, features: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(feature, ensure_ascii=False) + "\n" for feature in features),
        encoding="utf-8",
    )


class PlacementGeneratorTests(unittest.TestCase):
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
