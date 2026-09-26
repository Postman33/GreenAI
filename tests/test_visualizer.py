from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon, box, mapping

from src.visualization.prepare_scene import (
    build_manifest, camera_records, choose_focuses, place_pedestrian_camera,
    scatter_points, triangle_records,
)
from src.visualization.plant_prompt import after_prompt, scene_plant_summary


def write_features(path: Path, features: list[dict]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in features), encoding="utf-8")


class VisualizerTests(unittest.TestCase):
    def test_pedestrian_camera_moves_out_of_building_onto_visible_road(self) -> None:
        focus = Point(0, 0)
        cameras = camera_records((0.0, 1.0), 20)
        desired = cameras[1]["position"]
        building = box(desired[0] - 2, desired[1] - 2,
                       desired[0] + 2, desired[1] + 2)
        road = box(-30, -30, 30, 30)
        place_pedestrian_camera(cameras, focus, road, building, 20)
        eye = Point(*cameras[1]["position"][:2])
        self.assertEqual(cameras[1]["placement"], "road_with_clear_view")
        self.assertTrue(road.buffer(-0.75).covers(eye))
        self.assertFalse(LineString([eye, focus]).intersects(building.buffer(0.5)))

    def test_gallery_focuses_cover_distinct_planted_places(self) -> None:
        plan = [
            {"plant_type": "tree", "geometry": Point(x, 0)}
            for x in (0, 2, 100, 102, 200, 202)
        ]
        focuses = choose_focuses(plan, radius=20, count=3)
        self.assertEqual(len(focuses), 3)
        self.assertTrue(all(a.distance(b) >= 40 for i, a in enumerate(focuses)
                            for b in focuses[i + 1:]))
        self.assertTrue(all(any(focus.distance(item["geometry"]) <= 20 for item in plan)
                            for focus in focuses))

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

    def test_surface_triangles_preserve_holes_and_area(self) -> None:
        polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                          holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]])
        triangles = triangle_records(polygon, Point(0, 0), 0.1)
        pieces = [Polygon([(vertex[0], vertex[1]) for vertex in triangle]) for triangle in triangles]
        self.assertAlmostEqual(sum(piece.area for piece in pieces), polygon.area)
        self.assertTrue(all(polygon.covers(piece) for piece in pieces))

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
            }, {
                "type": "Feature",
                "properties": {"object_type": "proposed_planting", "plant_type": "shrub",
                               "planting_id": "S-1", "symbol_radius_m": 0.5},
                "geometry": mapping(Point(101, 102)),
            }])
            manifest = build_manifest(normalized, constraints, plan, scene, None, 20.0)
            self.assertEqual(manifest["source_origin"], {"x": 100.0, "y": 100.0})
            self.assertEqual(manifest["proposed_trees"][0]["position"], [0.0, 0.0])
            self.assertEqual(manifest["shrubs"][0]["position"], [1.0, 2.0])
            self.assertEqual(manifest["counts"]["proposed_shrub_instances"], 1)
            self.assertTrue(scene.exists())

    def test_species_mask_and_shrub_footprint_drive_photo_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            constraints = root / "constraints.geojsonl"
            plan = root / "plan.geojsonl"
            normalized.write_text("", encoding="utf-8")
            write_features(constraints, [{
                "type": "Feature", "properties": {"object_type": "base_allowed_area"},
                "geometry": mapping(box(-20, -20, 20, 20)),
            }])
            write_features(plan, [{
                "type": "Feature", "properties": {
                    "plant_type": "shrub", "planting_id": "S-1", "species": "Дерен белый",
                    "symbol_radius_m": 0.5, "footprint_radius_m": 0.75,
                }, "geometry": mapping(Point(0, 0)),
            }, {
                "type": "Feature", "properties": {
                    "plant_type": "tree", "planting_id": "T-1", "species": "Липа мелколистная",
                    "symbol_radius_m": 2.0,
                }, "geometry": mapping(Point(3, 0)),
            }, {
                "type": "Feature", "properties": {
                    "plant_type": "herbaceous", "planting_id": "H-1",
                    "species": "Газонная травосмесь",
                }, "geometry": mapping(box(-5, -5, -2, -2)),
            }])
            manifest = build_manifest(normalized, constraints, plan, root / "scene.json",
                                      None, 15.0)
            shrub = manifest["shrubs"][0]
            self.assertEqual(shrub["radius"], 0.75)
            self.assertEqual(shrub["species"], "Дерен белый")
            self.assertNotEqual(shrub["mask_color"], manifest["proposed_trees"][0]["mask_color"])
            self.assertTrue(any(surface.get("species") == "Газонная травосмесь" and
                                surface.get("mask_color") for surface in manifest["surfaces"]))
            summary = scene_plant_summary(manifest)
            self.assertEqual(next(item for item in summary if item["plant_type"] == "shrub")
                             ["hardiness_zone_min"], 2)
            prompt = after_prompt("overview", manifest)
            self.assertIn("Дерен белый", prompt)
            self.assertIn("Cornus alba", prompt)
            self.assertIn("Газонная травосмесь", prompt)
            self.assertIn("continuous", prompt)
            self.assertIn(shrub["mask_color"], prompt)


if __name__ == "__main__":
    unittest.main()
