from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, box, shape

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src import constraint_builder as constraints
from tests.helpers import feature, raw_hatch, write_jsonl


class ConstraintBuilderTests(unittest.TestCase):
    def test_surface_and_road_layer_classification(self) -> None:
        self.assertEqual(
            constraints.classify_surface_layer("_АД_граница покрытия"),
            "reference_geometry",
        )
        self.assertEqual(
            constraints.classify_surface_layer(
                "ДВ_ПП_ДО_Тип3_Устройство_уширений_магистральные за счет ГАЗОНА"
            ),
            "hard_surface",
        )
        self.assertEqual(
            constraints.classify_surface_layer("ДВ_ПП_ДО_Тип6_ТРТ за ГАЗОН"),
            "hard_surface",
        )
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

    def test_road_continues_only_into_nearby_aligned_unseeded_parcel(self) -> None:
        first = box(0, 0, 40, 100)
        second = box(0, 110, 40, 210)
        remote = box(100, 220, 140, 320)
        work = MultiPolygon([first, second, remote])
        seeded = box(10, 0, 30, 100)
        lawns = MultiPolygon([box(0, 110, 10, 210), box(30, 110, 40, 210)])

        road, report = constraints.recover_unseeded_road_components(
            seeded, work, lawns, box(0, 0, 0, 0)
        )

        self.assertGreater(road.intersection(second).area, 1000)
        self.assertAlmostEqual(road.intersection(lawns).area, 0)
        self.assertAlmostEqual(road.intersection(remote).area, 0)
        self.assertEqual(len(report["inferred_components"]), 1)

    def test_build_uses_positive_plantable_mask_and_excludes_sidewalk_and_building(self) -> None:
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
                    feature("building", box(7, 0, 9, 2)),
                    feature("utility_well_footprint", box(2.5, 5, 3.5, 6)),
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

            self.assertAlmostEqual(by_type["base_allowed_area"].area, 55.0, places=5)
            self.assertAlmostEqual(by_type["base_allowed_area"].intersection(by_type["sidewalk_area"]).area, 0.0)
            self.assertAlmostEqual(by_type["base_allowed_area"].intersection(by_type["buildings_in_work_area"]).area, 0.0)
            self.assertAlmostEqual(
                by_type["base_allowed_area"].intersection(
                    by_type["utility_well_footprints"]
                ).area,
                0.0,
            )
            self.assertAlmostEqual(by_type["base_allowed_area"].difference(by_type["confirmed_plantable_surface"]).area, 0.0)
            self.assertEqual(report["planting_candidate_source"], "confirmed_plantable_surface")
            self.assertIn("utility_well_footprints", report["applied_restrictions"])

    def test_explicit_road_surface_survives_failed_curb_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            surfaces = root / "surfaces.jsonl"
            output = root / "constraints.geojsonl"
            report_path = root / "report.json"
            write_jsonl(
                normalized,
                [
                    feature("work_boundary", box(0, 0, 10, 10)),
                    feature("building", box(20, 20, 21, 21)),
                    feature("sidewalk", box(20, 22, 21, 23)),
                ],
            )
            write_jsonl(
                surfaces,
                [
                    raw_hatch([(0, 0), (10, 0), (10, 10), (0, 10)], layer="Газон"),
                    raw_hatch([(0, 0), (2, 0), (2, 10), (0, 10)], layer="ПЧ ремонт"),
                ],
            )

            constraints.build(normalized, surfaces, output, report_path)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            by_type = {record["properties"]["object_type"]: shape(record["geometry"]) for record in records}

            self.assertEqual(report["road_reconstruction"]["status"], "explicit_surface_fallback")
            self.assertAlmostEqual(by_type["road_area"].area, 20.0)
            self.assertAlmostEqual(by_type["base_allowed_area"].area, 80.0)


if __name__ == "__main__":
    unittest.main()
