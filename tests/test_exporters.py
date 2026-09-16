from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point, Polygon, box

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src import debug_export, dxf_exporter, plant_allow_zone_debug
from tests.helpers import feature, raw_hatch, write_jsonl


class ExporterTests(unittest.TestCase):
    def test_final_dxf_export_adds_zone_and_road_layers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dxf"
            zones = root / "zones.jsonl"
            constraints = root / "constraints.jsonl"
            output = root / "result.dxf"
            document = ezdxf.new("R2018")
            document.modelspace().add_line((0, 0), (10, 0))
            document.saveas(source)
            write_jsonl(
                zones,
                [feature("plant_allow_zone", box(1, 1, 4, 4), plant_type="tree")],
            )
            write_jsonl(constraints, [feature("road_area", box(5, 0, 10, 5))])

            dxf_exporter.export_zones(source, zones, output, 0.5, constraints)
            result = ezdxf.readfile(output)
            modelspace = result.modelspace()
            self.assertIn("GREEN_AI_ZONE_TREE", result.layers)
            self.assertIn("GREEN_AI_RECONSTRUCTED_ROAD", result.layers)
            self.assertEqual(len(modelspace.query('HATCH[layer=="GREEN_AI_ZONE_TREE"]')), 1)
            self.assertEqual(len(modelspace.query('HATCH[layer=="GREEN_AI_RECONSTRUCTED_ROAD"]')), 1)

    def test_debug_dxf_marks_raw_utilities_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "debug.dxf"
            actual = plant_allow_zone_debug.export_dxf(
                output,
                box(0, 0, 10, 10),
                box(0, 0, 1, 1),
                box(4, 0, 6, 10),
                box(0, 8, 10, 10),
                LineString([(4, 0), (4, 10)]),
                {
                    "water_pipe": LineString([(2, 0), (2, 10)]),
                    "existing_tree": Point(3, 3),
                },
                {"tree": box(1, 1, 3, 3)},
            )
            result = ezdxf.readfile(actual)
            self.assertTrue(result.layers.get("DEBUG_WATER_PIPE").is_off())
            self.assertFalse(result.layers.get("DEBUG_EXISTING_TREES").is_off())
            self.assertGreater(len(result.modelspace().query('*[layer=="DEBUG_ALLOW_TREE"]')), 0)

    def test_debug_surface_hypothesis_separates_hard_and_lawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidates = Path(directory) / "surfaces.jsonl"
            write_jsonl(
                candidates,
                [
                    raw_hatch(
                        [(0, 0), (4, 0), (4, 4), (0, 4)],
                        layer=debug_export.PROJECT_LAWN_LAYER,
                    ),
                    raw_hatch([(5, 0), (9, 0), (9, 4), (5, 4)], layer="Тротуар"),
                ],
            )
            hard, lawn, diagnostics = debug_export.read_surface_hypothesis(
                candidates, box(0, 0, 10, 10), 0.1, 0.01
            )
            self.assertAlmostEqual(hard.area, 16)
            self.assertAlmostEqual(lawn.area, 16)
            self.assertGreaterEqual(sum(diagnostics["hard_surface_layers"].values()), 1)

    def test_plant_zone_loader_merges_same_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zones.jsonl"
            write_jsonl(
                path,
                [
                    feature("plant_allow_zone", box(0, 0, 1, 1), plant_type="tree"),
                    feature("plant_allow_zone", box(2, 0, 3, 1), plant_type="tree"),
                ],
            )
            zones = dxf_exporter.load_plant_zones(path)
            self.assertAlmostEqual(zones["tree"].area, 2)


if __name__ == "__main__":
    unittest.main()
