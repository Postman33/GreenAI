"""Small CAD-like geometries that pin down road reconstruction behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, box
from shapely.ops import unary_union

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.geometry import constraint_builder as roads
from tests.helpers import raw_hatch, write_jsonl


class RoadReconstructionTests(unittest.TestCase):
    def test_dashed_curb_closes_small_gap_without_leaking_to_other_side(self) -> None:
        work = box(0, 0, 20, 10)
        curbs = MultiLineString([
            [(10, 0), (10, 4.8)],
            [(10, 5.2), (10, 10)],
        ])

        road, report = roads.build_road_area(
            curbs, work, box(1, 1, 2, 2),
            stitch_tolerance=0.3, min_seed_overlap_area=0.1,
        )

        self.assertTrue(road.covers(Point(5, 5)))
        self.assertFalse(road.covers(Point(15, 5)))
        self.assertEqual(report["seed_intersecting_cells"], 1)
        self.assertEqual(report["fallback_cells"], 0)

    def test_large_curb_gap_fails_area_sanity_check(self) -> None:
        work = box(0, 0, 20, 10)
        curbs = MultiLineString([
            [(10, 0), (10, 4)],
            [(10, 6), (10, 10)],
        ])

        with self.assertRaisesRegex(ValueError, "implausible area ratio"):
            roads.build_road_area(
                curbs, work, box(1, 1, 2, 2),
                stitch_tolerance=0.3, min_seed_overlap_area=0.1,
            )

    def test_known_non_road_polygon_is_not_swallowed_by_reconstruction(self) -> None:
        excluded = box(3, 4, 5, 6)
        seed = box(1, 1, 2, 2)

        road, report = roads.build_road_area(
            LineString([(10, 0), (10, 10)]), box(0, 0, 20, 10), seed,
            known_non_road_areas=(excluded,),
            stitch_tolerance=0.1, min_seed_overlap_area=0.1,
        )

        self.assertAlmostEqual(road.intersection(excluded).area, 0)
        self.assertTrue(road.covers(seed))
        self.assertAlmostEqual(
            report["known_non_road_area_in_dxf_square_units"], excluded.area,
        )

    def test_explicit_road_hatch_survives_curb_barrier(self) -> None:
        # The seed crosses the artificial 0.3-unit curb buffer, but its
        # overlap with the opposite cell stays below the seed threshold.
        seed = box(8, 1, 10.35, 2)
        road, _ = roads.build_road_area(
            LineString([(10, 0), (10, 10)]), box(0, 0, 20, 10), seed,
            stitch_tolerance=0.3, min_seed_overlap_area=0.1,
        )

        self.assertTrue(road.covers(seed))
        self.assertFalse(road.covers(Point(15, 5)))

    def test_terminal_recovery_rejects_interior_and_disconnected_pieces(self) -> None:
        work = box(0, 0, 20, 100)
        strict = box(5, 20, 15, 80)
        relaxed = unary_union([
            strict,
            box(5, 80, 15, 90),  # touches the road but not the work-area end
            box(5, 95, 15, 100),  # reaches the end but is disconnected
        ])

        road, report = roads.recover_outer_terminal_road(strict, relaxed, work)

        self.assertTrue(road.equals(strict))
        self.assertEqual(report["selected_extension_count"], 0)
        self.assertAlmostEqual(report["added_area_in_dxf_square_units"], 0)

    def test_terminal_recovery_uses_each_component_local_end(self) -> None:
        vertical = box(0, 0, 20, 100)
        horizontal = box(100, 0, 200, 20)
        work = MultiPolygon([vertical, horizontal])
        strict = unary_union([box(5, 0, 15, 60), box(100, 5, 160, 15)])
        relaxed = unary_union([box(5, 0, 15, 100), box(100, 5, 200, 15)])

        road, report = roads.recover_outer_terminal_road(strict, relaxed, work)

        self.assertTrue(road.covers(Point(10, 95)))
        self.assertTrue(road.covers(Point(195, 10)))
        self.assertEqual(report["selected_extension_count"], 2)
        self.assertEqual(
            {item["orientation"] for item in report["work_components"]},
            {"vertical", "horizontal"},
        )

    def test_terminal_recovery_rejects_implausibly_large_extension(self) -> None:
        strict = box(5, 0, 15, 10)
        road, report = roads.recover_outer_terminal_road(
            strict, box(5, 0, 15, 100), box(0, 0, 20, 100),
        )

        self.assertTrue(road.equals(strict))
        self.assertEqual(report["selected_extension_count"], 0)

    def test_unseeded_continuation_keeps_confirmed_non_road_hole(self) -> None:
        first = box(0, 0, 40, 100)
        second = box(0, 110, 40, 210)
        lawns = MultiPolygon([box(0, 110, 10, 210), box(30, 110, 40, 210)])
        excluded = box(15, 150, 25, 160)

        road, report = roads.recover_unseeded_road_components(
            box(10, 0, 30, 100), MultiPolygon([first, second]), lawns, excluded,
        )

        self.assertGreater(road.intersection(second).area, 1000)
        self.assertAlmostEqual(road.intersection(lawns).area, 0)
        self.assertAlmostEqual(road.intersection(excluded).area, 0)
        self.assertEqual(len(report["inferred_components"]), 1)

    def test_unseeded_continuation_stops_after_large_gap_or_without_lawns(self) -> None:
        first = box(0, 0, 40, 100)
        seeded = box(10, 0, 30, 100)
        scenarios = (
            (box(0, 130, 40, 230), MultiPolygon([
                box(0, 130, 10, 230), box(30, 130, 40, 230),
            ])),
            (box(50, 110, 90, 210), MultiPolygon([
                box(50, 110, 60, 210), box(80, 110, 90, 210),
            ])),
            (box(0, 110, 40, 210), box(0, 0, 0, 0)),
        )
        for second, lawns in scenarios:
            with self.subTest(second_bounds=second.bounds, lawns=lawns.area):
                road, report = roads.recover_unseeded_road_components(
                    seeded, MultiPolygon([first, second]), lawns, box(0, 0, 0, 0),
                )
                self.assertTrue(road.equals(seeded))
                self.assertEqual(report["inferred_components"], [])

    def test_road_hatch_selection_respects_layer_material_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidates = Path(directory) / "surfaces.jsonl"
            write_jsonl(candidates, [
                raw_hatch([(0, 0), (4, 0), (4, 5), (0, 5)], layer="ПЧ за ТРОТ"),
                raw_hatch([(6, 0), (10, 0), (10, 5), (6, 5)], layer="ТРОТ за ПЧ"),
            ])

            road_seed, report = roads.read_surface_area_by_predicate(
                candidates, box(0, 0, 10, 5), roads.is_road_surface_layer,
            )

            self.assertAlmostEqual(road_seed.area, 20)
            self.assertTrue(road_seed.covers(Point(2, 2)))
            self.assertFalse(road_seed.covers(Point(8, 2)))
            self.assertEqual(report["matched_records"], 1)


if __name__ == "__main__":
    unittest.main()
