from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, Point, shape

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.geometry import normalizer
from tests.helpers import raw_polyline, write_jsonl


class NormalizerTests(unittest.TestCase):
    def test_small_open_work_boundary_gap_closes_only_for_boundary(self) -> None:
        points = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0.5]]
        record = {"object_type": "work_boundary", "geometry": {"kind": "polyline", "points": points, "closed": False}}
        outline = normalizer.primitive_to_geometry(record, 0.1)
        self.assertTrue(outline.is_ring)
        self.assertAlmostEqual(normalizer.normalize_group("work_boundary", [outline]).area, 10000)

        record["object_type"] = "road_edge"
        self.assertFalse(normalizer.primitive_to_geometry(record, 0.1).is_ring)
        record["object_type"] = "work_boundary"
        record["geometry"]["points"][-1] = [0, 5]
        self.assertFalse(normalizer.primitive_to_geometry(record, 0.1).is_ring)

    def test_arc_wraps_through_zero_degrees(self) -> None:
        points = normalizer.arc_points([0, 0], 10, 350, 10, 0.2)
        self.assertGreater(len(points), 8)
        self.assertAlmostEqual(points[0][0], 10 * math.cos(math.radians(350)), places=6)
        self.assertAlmostEqual(points[-1][0], 10 * math.cos(math.radians(10)), places=6)

    def test_primitive_conversion_and_grouping(self) -> None:
        line = normalizer.primitive_to_geometry(
            {"geometry": {"kind": "line", "start": [0, 0], "end": [2, 0]}},
            0.1,
        )
        point = normalizer.primitive_to_geometry(
            {"geometry": {"kind": "insert_point", "location": [1, 2]}},
            0.1,
        )
        polygon = normalizer.normalize_group(
            "work_boundary",
            [LineString([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])],
        )
        self.assertEqual(line.length, 2)
        self.assertEqual(point, Point(1, 2))
        self.assertAlmostEqual(polygon.area, 4)

    def test_work_boundary_with_self_crossing_closed_outline(self) -> None:
        outline = LineString([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
        polygon = normalizer.normalize_group("work_boundary", [outline])
        self.assertIsNotNone(polygon)
        self.assertAlmostEqual(polygon.area, 2.0)

    def test_building_polygonizer_closes_endpoint_chain(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (10, 10)]),
            LineString([(10, 10), (0, 10)]),
            LineString([(0, 10), (0, 0.05)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)
        self.assertIsNotNone(polygon)
        self.assertAlmostEqual(polygon.area, 100.0)
        self.assertEqual(diagnostics["repaired_chain_count"], 1)
        self.assertAlmostEqual(
            diagnostics["repaired_total_length_in_dxf_units"], 0.05
        )

    def test_building_polygonizer_rejects_long_missing_side(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (10, 10)]),
            LineString([(10, 10), (0, 10)]),
            LineString([(0, 10), (0, 5)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)
        self.assertIsNone(polygon)
        self.assertEqual(diagnostics["repaired_chain_count"], 0)
        self.assertEqual(diagnostics["rejected_open_chain_count"], 1)

    def test_building_polygonizer_restores_orthogonal_missing_wall(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (10, 10)]),
            LineString([(10, 10), (0, 10)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)

        self.assertIsNotNone(polygon)
        self.assertAlmostEqual(polygon.area, 100.0)
        self.assertEqual(diagnostics["orthogonal_missing_wall_count"], 1)
        self.assertEqual(
            diagnostics["repaired_chains"][0]["mode"],
            "orthogonal_missing_wall",
        )

    def test_building_polygonizer_repairs_small_relative_gap(self) -> None:
        lines = [
            LineString([(0.5, 0), (100, 0), (100, 100), (0, 100), (0, 0)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)

        self.assertIsNotNone(polygon)
        self.assertAlmostEqual(polygon.area, 10000.0)
        self.assertEqual(
            diagnostics["repaired_chains"][0]["mode"],
            "small_relative_gap",
        )

    def test_building_polygonizer_rejects_straight_chain(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (20, 0)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)
        self.assertIsNone(polygon)
        self.assertEqual(diagnostics["repaired_chain_count"], 0)
        self.assertEqual(diagnostics["rejected_open_chain_count"], 1)

    def test_right_angle_does_not_invent_missing_rectangle(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(0, 0), (0, 5)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)
        self.assertIsNone(polygon)
        self.assertEqual(diagnostics["repaired_chain_count"], 0)

    def test_non_right_open_angle_remains_unresolved(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(0, 0), (8, 5)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)
        self.assertIsNone(polygon)
        self.assertEqual(diagnostics["repaired_chain_count"], 0)

    def test_building_polygonizer_does_not_close_branched_component(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (10, 10)]),
            LineString([(10, 0), (15, 0)]),
        ]
        polygon, diagnostics = normalizer.polygonal_geometry_with_endpoint_closure(lines)
        self.assertIsNone(polygon)
        self.assertEqual(diagnostics["branched_component_count"], 1)

    def test_normalize_writes_geojson_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.jsonl"
            output = root / "normalized.geojsonl"
            report = root / "report.json"
            write_jsonl(
                source,
                [
                    raw_polyline(
                        "work_boundary",
                        [(0, 0), (10, 0), (10, 10), (0, 10)],
                        closed=True,
                        layer="BOUNDARY",
                    ),
                    raw_polyline(
                        "building",
                        [(2, 2), (4, 2), (4, 4), (2, 4)],
                        closed=True,
                        layer="BUILDING",
                    ),
                    {
                        "object_type": "existing_tree",
                        "source_layer": "TREES",
                        "geometry": {"kind": "circle", "center": [3, 4], "radius": 1},
                    },
                    {
                        "object_type": "utility_well",
                        "source_layer": "Колодцы",
                        "geometry": {
                            "kind": "arc",
                            "center": [6, 6],
                            "radius": 0.5,
                            "start_angle": 0.0,
                            "end_angle": 359.999995,
                        },
                    },
                ],
            )
            normalizer.normalize(source, output, report, 0.1, None)
            features = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            summary = json.loads(report.read_text(encoding="utf-8"))

            self.assertEqual(
                {item["properties"]["object_type"] for item in features},
                {
                    "work_boundary",
                    "building",
                    "building_linework",
                    "existing_tree",
                    "utility_well",
                    "utility_well_footprint",
                },
            )
            building = next(
                item for item in features
                if item["properties"]["object_type"] == "building"
            )
            self.assertAlmostEqual(shape(building["geometry"]).area, 4.0)
            self.assertEqual(summary["object_types"]["work_boundary"]["status"], "ok")
            self.assertEqual(summary["object_types"]["existing_tree"]["result_geometry"], "MultiPoint")
            self.assertEqual(
                summary["object_types"]["existing_tree"]["tree_symbol_reduction"][
                    "tree_marker_count"
                ],
                1,
            )
            well_footprint = next(
                item for item in features
                if item["properties"]["object_type"] == "utility_well_footprint"
            )
            self.assertAlmostEqual(shape(well_footprint["geometry"]).area, math.pi * 0.25, places=2)
            self.assertEqual(
                summary["object_types"]["utility_well"]["physical_footprint"][
                    "source_symbol_count"
                ],
                1,
            )

    def test_existing_tree_symbol_uses_one_circular_trunk_anchor(self) -> None:
        records = [
            {
                "geometry": {
                    "kind": "arc",
                    "center": [10.0, 20.0],
                    "radius": 0.21,
                    "start_angle": 0.0,
                    "end_angle": 359.999994,
                }
            },
            {"geometry": {"kind": "ellipse", "center": [10.0, 21.088]}},
            {
                "geometry": {
                    "kind": "line",
                    "start": [9.8, 20.0],
                    "end": [10.2, 20.0],
                }
            },
            {
                "geometry": {
                    "kind": "arc",
                    "center": [30.0, 40.0],
                    "radius": 0.21,
                    "start_angle": 10.0,
                    "end_angle": 9.999994,
                }
            },
        ]
        fallback = [Point(10, 20), Point(10, 21.088), Point(10, 20), Point(30, 40)]

        geometry, diagnostics = normalizer.existing_tree_geometry(records, fallback)

        self.assertEqual(len(geometry.geoms), 2)
        self.assertEqual(diagnostics["tree_marker_count"], 2)
        self.assertEqual(diagnostics["ignored_symbol_decoration_count"], 2)
        self.assertEqual(diagnostics["method"], "full_circle_trunk_marker_centres")


if __name__ == "__main__":
    unittest.main()
