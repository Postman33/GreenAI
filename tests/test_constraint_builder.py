from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, box, shape

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src import constraint_builder as constraints
from tests.helpers import feature, raw_hatch, write_jsonl


class ConstraintBuilderTests(unittest.TestCase):
    def test_surface_and_road_layer_classification(self) -> None:
        self.assertEqual(
            constraints.classify_surface_layer("ДВ_ГП_П_Газон_Рулонный"),
            "plantable_candidate",
        )
        self.assertEqual(
            constraints.classify_surface_layer("Замена покрытия тротуара"),
            "hard_surface",
        )
        self.assertTrue(constraints.is_road_surface_layer("ПЧ за ТРОТ"))
        self.assertFalse(constraints.is_road_surface_layer("ТРОТ за ПЧ"))

    def test_build_road_area_selects_seeded_partition(self) -> None:
        work = box(0, 0, 20, 10)
        divider = LineString([(10, 0), (10, 10)])
        seed = box(1, 1, 2, 2)
        road, report = constraints.build_road_area(
            divider,
            work,
            seed,
            known_non_road_areas=(),
            stitch_tolerance=0.01,
            min_seed_overlap_area=0.1,
        )
        self.assertTrue(road.covers(seed.centroid))
        self.assertEqual(report["method"], "curb_barrier_cells_selected_by_road_hatch")
        self.assertLess(road.area, work.area)

    def test_build_uses_positive_plantable_mask_and_excludes_sidewalk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            surfaces = root / "surfaces.jsonl"
            output = root / "constraints.geojsonl"
            report_path = root / "report.json"
            write_jsonl(
                normalized,
                [
                    feature("work_boundary", box(0, 0, 20, 20)),
                    feature("sidewalk", box(0, 0, 2, 10)),
                    feature("building", box(30, 30, 31, 31)),
                ],
            )
            write_jsonl(
                surfaces,
                [
                    raw_hatch(
                        [(0, 0), (10, 0), (10, 10), (0, 10)],
                        layer="Газон рулонный",
                    ),
                    raw_hatch(
                        [(4, 0), (6, 0), (6, 10), (4, 10)],
                        layer="Покрытие тротуара",
                    ),
                ],
            )

            constraints.build(normalized, surfaces, output, report_path)
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            by_type = {record["properties"]["object_type"]: shape(record["geometry"]) for record in records}
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertAlmostEqual(by_type["base_allowed_area"].area, 60.0, places=5)
            self.assertAlmostEqual(by_type["base_allowed_area"].intersection(by_type["sidewalk_area"]).area, 0.0)
            self.assertAlmostEqual(by_type["base_allowed_area"].difference(by_type["confirmed_plantable_surface"]).area, 0.0)
            self.assertEqual(report["planting_candidate_source"], "confirmed_plantable_surface")


if __name__ == "__main__":
    unittest.main()
