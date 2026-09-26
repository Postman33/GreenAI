from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Point, Polygon, mapping

from src.visualization.prepare_scene import build_manifest, camera_records, scatter_points


def write_features(path: Path, features: list[dict]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in features), encoding="utf-8")


class VisualizerTests(unittest.TestCase):
    def test_camera_set_contains_matching_perspective_and_top_views(self) -> None:
        cameras = camera_records((0.0, 1.0), 60.0)
        self.assertEqual([item["name"] for item in cameras], ["overview", "pedestrian", "top"])
        self.assertEqual(cameras[-1]["kind"], "orthographic")

    def test_scatter_points_remain_inside_polygon_with_hole(self) -> None:
        polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                          holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]])
        points = scatter_points(polygon, 1.0)
        self.assertGreater(len(points), 70)
        self.assertTrue(all(polygon.covers(point) for point in points))

    def test_manifest_recentres_and_preserves_tree_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            constraints = root / "constraints.geojsonl"
            plan = root / "plan.geojsonl"
            scene = root / "scene.json"
            write_features(normalized, [{
                "type": "Feature", "properties": {"object_type": "existing_tree"},
                "geometry": mapping(Point(102, 102)),
            }])
            square = Polygon([(90, 90), (110, 90), (110, 110), (90, 110)])
            write_features(constraints, [
                {"type": "Feature", "properties": {"object_type": kind}, "geometry": mapping(square)}
                for kind in ("base_allowed_area", "road_area", "sidewalk_area", "buildings_in_work_area")
            ])
            write_features(plan, [{
                "type": "Feature",
                "properties": {"object_type": "proposed_planting", "plant_type": "tree",
                               "planting_id": "T-1", "symbol_radius_m": 2.0},
                "geometry": mapping(Point(100, 100)),
            }])
            manifest = build_manifest(normalized, constraints, plan, scene, None, 20.0)
            self.assertEqual(manifest["source_origin"], {"x": 100.0, "y": 100.0})
            self.assertEqual(manifest["proposed_trees"][0]["position"], [0.0, 0.0])
            self.assertTrue(scene.exists())


if __name__ == "__main__":
    unittest.main()
