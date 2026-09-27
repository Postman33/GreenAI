from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from shapely.geometry import LineString, Point, Polygon, box, mapping

from src.visualization.prepare_scene import (
    build_manifest, camera_records, choose_focuses, place_pedestrian_camera,
    context_buildings, frame_overview_camera, polygon_records, scatter_points, triangle_records,
)
from src.visualization.plant_prompt import after_prompt, scene_plant_summary
from scripts import render_photorealistic_gallery as photos


def write_features(path: Path, features: list[dict]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in features), encoding="utf-8")


class VisualizerTests(unittest.TestCase):
    def test_building_context_uses_whole_footprints_across_work_boundary(self) -> None:
        full = box(5, -20, 20, 20)
        sliver = box(5, -10, 5.000001, 10)
        result, audit = context_buildings({"building": full},
                                         {"buildings_in_work_area": sliver}, box(-10, -10, 10, 10))
        self.assertTrue(result.equals(full))
        self.assertEqual(audit["source"], "normalized_building")
        self.assertEqual(audit["whole_footprints"], 1)
        self.assertEqual(audit["height_source"], "illustrative_default")

    def test_building_context_does_not_extrude_degenerate_fallback(self) -> None:
        sliver = box(0, 0, 0.000001, 10)
        result, audit = context_buildings({}, {"buildings_in_work_area": sliver}, box(-20, -20, 20, 20))
        self.assertTrue(result.is_empty)
        self.assertEqual(audit["omitted_degenerate_footprints"], 1)

    def test_small_building_has_explicit_illustrative_height(self) -> None:
        small = polygon_records(box(0, 0, 5, 6), Point(0, 0), 12)[0]
        large = polygon_records(box(0, 0, 20, 30), Point(0, 0), 12)[0]
        self.assertEqual(small["height"], 3.0)
        self.assertEqual(small["height_source"], "illustrative_small_footprint")
        self.assertEqual(large["height"], 12)

    def test_pedestrian_camera_separates_new_tree_crowns(self) -> None:
        cameras = camera_records((1.0, 0.0), 45)
        trees = [{"position": [x, 0], "crown_radius": 2} for x in (0, 6)]
        place_pedestrian_camera(cameras, Point(0, 0), box(-40, -20, 40, -3),
                                Polygon(), 45, proposed_trees=trees)
        eye = Point(cameras[1]["position"][:2])
        angles = [math.atan2(-eye.y, x - eye.x) for x in (0, 6)]
        separation = abs(math.atan2(math.sin(angles[0] - angles[1]), math.cos(angles[0] - angles[1])))
        widths = sum(math.atan2(2, eye.distance(Point(x, 0))) for x in (0, 6))
        self.assertGreaterEqual(separation, widths)
        self.assertEqual(cameras[1]["target"][:2], [3, 0])

    def test_overview_camera_faces_bed_from_the_road_side(self) -> None:
        cameras = camera_records((0, 1), 45)
        frame_overview_camera(cameras, Point(0, 0), box(-17, -4, 17, 4),
                              box(-50, -30, 50, -5), 45, [{"height": 5.5}])
        camera = cameras[0]
        self.assertLess(camera["position"][1], 0)
        self.assertEqual(camera["target"][:2], [0, 0])
        self.assertGreater(camera["lens_mm"], 0)
        self.assertLessEqual(camera["lens_mm"], 55)

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
                               "planting_id": "T-1", "symbol_radius_m": 2.0,
                               "footprint_radius_m": 3.0},
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
            self.assertEqual(manifest["proposed_trees"][0]["crown_radius"], 3.0)
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

    def test_photo_pair_reuses_before_reference_and_cache_after_gallery_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            place = root / "place_01"
            renders = place / "renders"
            renders.mkdir(parents=True)
            for name in ("overview_before", "overview_after", "overview_plant_mask"):
                Image.new("RGB", (2, 2), "green").save(renders / f"{name}.png")
            (place / "scene.json").write_text(json.dumps({
                "plant_mask_legend": [{"plant_type": "tree", "species": "Липа мелколистная", "color": "#00BFFF"}],
                "proposed_trees": [{"species": "Липа мелколистная"}],
            }), encoding="utf-8")
            index = root / "gallery_index.json"
            index.write_text(json.dumps({"places": [{"id": "place_01", "scene": "old/scene.json", "renders": "old/renders"}]}), encoding="utf-8")
            requests = []

            def generate(**kwargs):
                requests.append(kwargs)
                output = kwargs["output_stem"].with_suffix(".png")
                Image.new("RGB", (2, 2), "blue").save(output)
                return output, 0.01

            with patch("sys.argv", ["photos", "--gallery-index", str(index)]), \
                    patch.object(photos, "load_api_key", return_value="test-placeholder"), \
                    patch.object(photos, "generate_image", side_effect=generate):
                photos.main()
                photos.main()
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1]["references"][:2],
                             [renders / "overview_after.png", renders / "overview_plant_mask.png"])
            self.assertEqual(requests[1]["references"][2], requests[0]["output_stem"].with_suffix(".png"))
            self.assertIn("Reference 3", requests[1]["prompt"])


if __name__ == "__main__":
    unittest.main()
