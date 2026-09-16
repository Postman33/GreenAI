from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, Point

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src import normalizer
from tests.helpers import raw_polyline, write_jsonl


class NormalizerTests(unittest.TestCase):
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
                    {
                        "object_type": "existing_tree",
                        "source_layer": "TREES",
                        "geometry": {"kind": "circle", "center": [3, 4], "radius": 1},
                    },
                ],
            )
            normalizer.normalize(source, output, report, 0.1, None)
            features = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            summary = json.loads(report.read_text(encoding="utf-8"))

            self.assertEqual({item["properties"]["object_type"] for item in features}, {"work_boundary", "existing_tree"})
            self.assertEqual(summary["object_types"]["work_boundary"]["status"], "ok")
            self.assertEqual(summary["object_types"]["existing_tree"]["result_geometry"], "MultiPoint")


if __name__ == "__main__":
    unittest.main()
