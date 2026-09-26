from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, box

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.detection.cleaning import clean_utilities as cleaner


RULES = {
    "min_fragment_length": 0.4,
    "min_seed_length": 5.0,
    "min_seed_straightness": 0.92,
    "min_component_length": 20.0,
    "min_component_span": 15.0,
    "connect_tolerance": 0.2,
    "max_compact_closed_span": 8.0,
    "max_connector_length": 4.0,
    "min_arrow_leg_length_ratio": 0.65,
    "min_arrow_angle_deg": 12.0,
    "max_arrow_angle_deg": 75.0,
    "max_arrow_barb_angle_deg": 55.0,
    "max_arrow_leg_length": 4.0,
    "max_arrow_barb_length": 2.0,
    "max_arrow_shaft_length": 8.0,
    "min_high_confidence_single_length": 30.0,
    "min_straight_continuation_angle_deg": 150.0,
    "allow_isolated_long_strokes": False,
}


class UtilityCleanerTests(unittest.TestCase):
    def test_connected_components_use_endpoints_not_crossings(self) -> None:
        lines = [
            LineString([(0, 0), (5, 0)]),
            LineString([(5, 0), (10, 0)]),
            LineString([(2.5, -2), (2.5, 2)]),
        ]
        components = sorted(sorted(group) for group in cleaner.connected_components(lines, 0.01))
        self.assertEqual(components, [[0, 1], [2]])

    def test_arrow_detector_finds_v_legs_and_leader(self) -> None:
        lines = [
            LineString([(0, 0), (1, 0.5)]),
            LineString([(0, 0), (1, -0.5)]),
            LineString([(0, 0), (6, 0)]),
        ]
        self.assertEqual(cleaner.find_arrowhead_lines(lines, RULES), {0, 1, 2})

    def test_clean_type_accepts_collinear_route_and_rejects_noise(self) -> None:
        primitives = [
            cleaner.Primitive(LineString([(0, 0), (10, 0)]), "Water", "LINE"),
            cleaner.Primitive(LineString([(10, 0), (25, 0)]), "Water", "LINE"),
            cleaner.Primitive(LineString([(50, 50), (50.1, 50)]), "Water", "LINE"),
        ]
        buckets, report = cleaner.clean_type(primitives, RULES)
        self.assertEqual(report["accepted_primitive_count"], 2)
        self.assertEqual(report["rejected_primitive_count"], 1)
        self.assertEqual(len(buckets["accepted_backbone"]), 2)
        self.assertEqual(len(buckets["too_short"]), 1)

    def test_load_primitives_clips_to_scope(self) -> None:
        from tests.helpers import write_jsonl

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "object_type": "water_pipe",
                        "source_layer": "Water",
                        "dxf_type": "LINE",
                        "geometry": {"kind": "line", "start": [-5, 5], "end": [5, 5]},
                    },
                    {
                        "object_type": "water_pipe",
                        "source_layer": "Water",
                        "dxf_type": "LINE",
                        "geometry": {"kind": "line", "start": [20, 20], "end": [30, 20]},
                    },
                ],
            )
            grouped, outside, invalid = cleaner.load_primitives(
                path, {"water_pipe"}, box(0, 0, 10, 10), 0.1
            )
            self.assertEqual(len(grouped["water_pipe"]), 1)
            self.assertAlmostEqual(grouped["water_pipe"][0].geometry.length, 5)
            self.assertEqual(outside["water_pipe"], 1)
            self.assertEqual(invalid["water_pipe"], 0)


if __name__ == "__main__":
    unittest.main()
