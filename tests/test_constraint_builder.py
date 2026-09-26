from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.ops import unary_union

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from core.heat_chamber_detector import detect_dashed_square_chambers
from src.geometry import constraint_builder as constraints
from tests.helpers import feature, raw_hatch, write_jsonl


class ConstraintBuilderTests(unittest.TestCase):
    def test_dashed_chamber_ignores_annotation_leader_spurs(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "heat_chamber_leader_spur.json")
            .read_text(encoding="utf-8")
        )
        lines = MultiLineString(fixture["lines"])
        well_x, well_y, well_radius = fixture["well"]
        chamber, report = detect_dashed_square_chambers(
            lines,
            Point(well_x, well_y).buffer(well_radius),
            box(-1, -1, 6, 7),
            1.0,
        )

        self.assertEqual(report["accepted"], 1)
        self.assertTrue(chamber.covers(Point(1.3681, 3.4484)))
        self.assertLess(chamber.bounds[3], 5.1)
        self.assertEqual(len(chamber.exterior.coords), 5)

    def test_dashed_chamber_with_jog_is_filled_but_solid_pipe_box_is_not(self) -> None:
        def dashed_outline(corners):
            segments = []
            for start, end in zip(corners, corners[1:]):
                length = math.dist(start, end)
                offset = 0.0
                while offset < length:
                    finish = min(offset + 0.72, length)
                    segments.append(LineString([
                        (
                            start[0] + (end[0] - start[0]) * offset / length,
                            start[1] + (end[1] - start[1]) * offset / length,
                        ),
                        (
                            start[0] + (end[0] - start[0]) * finish / length,
                            start[1] + (end[1] - start[1]) * finish / length,
                        ),
                    ]))
                    offset += 1.2
            return segments

        jogged = dashed_outline([
            (0, 0), (8, 0), (8, 5), (7, 5),
            (7, 11), (0, 11), (0, 0),
        ])
        solid_pipe_box = box(20, 0, 28, 5).boundary
        wells = unary_union([
            box(6.7, 0.7, 7.7, 1.7),
            box(22, 1, 23, 2),
        ])
        recovered, report = detect_dashed_square_chambers(
            unary_union(jogged + [solid_pipe_box]),
            wells,
            box(-2, -2, 30, 13),
            1.0,
        )
        self.assertTrue(recovered.covers(box(7.5, 2, 7.8, 3)))
        self.assertFalse(recovered.covers(box(22, 1, 23, 2)))
        self.assertLessEqual(len(recovered.exterior.coords), 20)
        self.assertGreaterEqual(report["buffered_refinements"], 1)

    def test_large_dashed_chamber_beside_small_chamber(self) -> None:
        def dashed_side(start, end):
            length = math.dist(start, end)
            return [
                LineString([
                    (
                        start[0] + (end[0] - start[0]) * offset / length,
                        start[1] + (end[1] - start[1]) * offset / length,
                    ),
                    (
                        start[0] + (end[0] - start[0])
                         * min(offset + 0.7, length) / length,
                        start[1] + (end[1] - start[1])
                         * min(offset + 0.7, length) / length,
                    ),
                ])
                for offset in (index * 1.2 for index in range(10))
                if offset < length
            ]

        outlines = [
            [(0, 0), (5, 0), (5, 10), (0, 10), (0, 0)],
            [(5, 0), (7.2, 0), (7.2, 2.8), (5, 2.8), (5, 0)],
        ]
        raw = unary_union([
            stroke
            for outline in outlines
            for start, end in zip(outline, outline[1:])
            for stroke in dashed_side(start, end)
        ])
        wells = unary_union([box(0.5, 8.5, 1.5, 9.5), box(5.5, 0.5, 6.5, 1.5)])
        recovered, _ = detect_dashed_square_chambers(
            raw, wells, box(-2, -2, 9, 12), 1.0
        )
        self.assertTrue(recovered.covers(box(1, 8, 2, 9)))
        self.assertTrue(recovered.covers(box(5.6, 0.6, 6.2, 1.2)))

    def test_heat_chamber_uses_full_well_at_work_boundary(self) -> None:
        outline = [(2, 2), (7, 2), (7, 7), (2, 7), (2, 2)]
        strokes = [
            LineString([
                (
                    start[0] + (end[0] - start[0]) * offset / 5,
                    start[1] + (end[1] - start[1]) * offset / 5,
                ),
                (
                    start[0] + (end[0] - start[0]) * (offset + 0.7) / 5,
                    start[1] + (end[1] - start[1]) * (offset + 0.7) / 5,
                ),
            ])
            for start, end in zip(outline, outline[1:])
            for offset in (0, 1.2, 2.4, 3.6)
        ]
        work = box(2.98, 0, 10, 10)
        well = box(2, 4.5, 3, 5.5)
        self.assertLess(well.intersection(work).area, 0.1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            surfaces = root / "surfaces.jsonl"
            reconstructed = root / "reconstructed.geojsonl"
            output = root / "constraints.geojsonl"
            report_path = root / "report.json"
            write_jsonl(normalized, [
                feature("work_boundary", work),
                feature("building", box(20, 20, 21, 21)),
                feature("utility_well_footprint", well),
                feature("heat_pipe", unary_union(strokes)),
            ])
            write_jsonl(surfaces, [raw_hatch(
                [(2.98, 0), (10, 0), (10, 10), (2.98, 10)],
                layer="Р“Р°Р·РѕРЅ",
            )])
            write_jsonl(reconstructed, [feature(
                "heat_pipe", LineString([(20, 20), (30, 30)])
            )])
            constraints.build(
                normalized, surfaces, output, report_path,
                reconstructed_utilities_path=reconstructed,
            )
            chambers = constraints.read_object_geometry(
                output, "heat_chamber_footprints"
            )
            full_chambers = constraints.read_object_geometry(
                output, "heat_chamber_full_footprints"
            )
            base = constraints.read_object_geometry(output, "base_allowed_area")
            self.assertTrue(chambers.covers(box(4, 4, 5, 5)))
            self.assertTrue(full_chambers.covers(box(2.2, 4, 2.6, 5)))
            self.assertFalse(chambers.covers(box(2.2, 4, 2.6, 5)))
            self.assertFalse(base.covers(box(4, 4, 5, 5)))

    def test_dashed_square_heat_chamber_is_recovered_from_raw_lines(self) -> None:
        angle = math.radians(14)

        def rotate(point):
            x, y = point
            return (
                10 + x * math.cos(angle) - y * math.sin(angle),
                10 + x * math.sin(angle) + y * math.cos(angle),
            )

        corners = [rotate(point) for point in (
            (-2, -2), (2, -2), (2, 2), (-2, 2),
        )]
        sides = [
            (corners[index], corners[(index + 1) % 4])
            for index in range(4)
        ]
        strokes = [
            LineString([
                (start[0] + (end[0] - start[0]) * lo,
                 start[1] + (end[1] - start[1]) * lo),
                (start[0] + (end[0] - start[0]) * hi,
                 start[1] + (end[1] - start[1]) * hi),
            ])
            for start, end in sides
            for lo, hi in ((0, 0.185), (0.31, 0.495), (0.62, 0.805))
        ]
        raw_heat = unary_union(strokes)
        well = box(9.7, 9.7, 10.3, 10.3)
        work = box(0, 0, 20, 20)
        recovered, detection = detect_dashed_square_chambers(
            raw_heat, well, work, 1.0
        )
        self.assertEqual(detection["accepted"], 1)
        self.assertTrue(recovered.covers(box(9.9, 9.9, 10.1, 10.1)))
        incomplete, _ = detect_dashed_square_chambers(
            unary_union(strokes[:9]), well, work, 1.0
        )
        self.assertTrue(incomplete.is_empty)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            surfaces = root / "surfaces.jsonl"
            reconstructed = root / "reconstructed.geojsonl"
            output = root / "constraints.geojsonl"
            report_path = root / "report.json"
            write_jsonl(normalized, [
                feature("work_boundary", work),
                feature("building", box(30, 30, 31, 31)),
                feature("utility_well_footprint", well),
                feature("heat_pipe", raw_heat),
            ])
            write_jsonl(surfaces, [raw_hatch(
                [(0, 0), (20, 0), (20, 20), (0, 20)], layer="Газон"
            )])
            write_jsonl(reconstructed, [feature(
                "heat_pipe", LineString([(30, 30), (40, 40)])
            )])
            constraints.build(
                normalized, surfaces, output, report_path,
                reconstructed_utilities_path=reconstructed,
            )
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            by_type = {
                record["properties"]["object_type"]: shape(record["geometry"])
                for record in records
            }
            self.assertFalse(by_type["base_allowed_area"].covers(box(9.9, 9.9, 10.1, 10.1)))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                report["heat_chamber_detection"]["dashed_square_detection"]["accepted"], 1
            )

    def test_heat_chamber_exclusion_requires_well_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            surfaces = root / "surfaces.jsonl"
            reconstructed = root / "reconstructed.geojsonl"
            output = root / "constraints.geojsonl"
            report_path = root / "report.json"
            chamber = box(2, 2, 7, 7)
            unrelated_loop = box(12, 2, 17, 7)
            heat_lines = unary_union([chamber.boundary, unrelated_loop.boundary])
            write_jsonl(normalized, [
                feature("work_boundary", box(0, 0, 20, 10)),
                feature("building", box(30, 30, 31, 31)),
                feature("utility_well_footprint", box(2, 4, 3, 5)),
            ])
            write_jsonl(surfaces, [
                raw_hatch(
                    [(0, 0), (20, 0), (20, 10), (0, 10)],
                    layer="Газон",
                ),
            ])
            write_jsonl(reconstructed, [feature("heat_pipe", heat_lines)])

            constraints.build(
                normalized, surfaces, output, report_path,
                reconstructed_utilities_path=reconstructed,
            )

            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            by_type = {
                record["properties"]["object_type"]: shape(record["geometry"])
                for record in records
            }
            self.assertAlmostEqual(by_type["heat_chamber_footprints"].area, 25.0)
            self.assertAlmostEqual(
                by_type["heat_chamber_review_footprints"].area, 25.0
            )
            self.assertFalse(by_type["base_allowed_area"].covers(chamber.centroid))
            self.assertTrue(by_type["base_allowed_area"].covers(unrelated_loop.centroid))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["heat_chamber_detection"]["accepted_faces"], 1)
            self.assertEqual(
                report["heat_chamber_detection"]["review_faces_with_planting_potential"], 1
            )

    def test_irregular_heat_loop_with_well_is_not_chamber(self) -> None:
        irregular = Polygon([
            (0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6),
        ])
        accepted, review, report = constraints.infer_heat_chamber_footprints(
            irregular.boundary, box(0.2, 0.2, 1.2, 1.2), 1.0
        )
        self.assertTrue(accepted.is_empty)
        self.assertTrue(review.is_empty)
        self.assertEqual(report["accepted_faces"], 0)

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
        for layer in (
            "ДВ_ПП_Тип5_Р ТР",
            "ДВ_ПП_Тип6_У_ТР_3м за счет Газона",
            "ДВ_ПП_Тип7_У_ТР_до 3м за счет АБ_ПЧ",
            "ДВ_ПП_Тип5_Тротуар АБ сущий",
            "ДВ_ПП_Тип7_Тротуар АБ менее 2м за Газон",
        ):
            with self.subTest(layer=layer):
                self.assertEqual(constraints.classify_surface_layer(layer), "hard_surface")
                self.assertTrue(constraints.is_sidewalk_partition_layer(layer))
                self.assertFalse(constraints.is_road_surface_layer(layer))
        self.assertEqual(
            constraints.classify_surface_layer("ДВ_ПП_Газон_У за счет АБ_ТР"),
            "plantable_candidate",
        )

    def test_project_sidewalk_hatch_excludes_replaced_lawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            surfaces = root / "surfaces.jsonl"
            output = root / "constraints.geojsonl"
            write_jsonl(normalized, [
                feature("work_boundary", box(0, 0, 10, 10)),
                feature("building", box(20, 20, 21, 21)),
            ])
            write_jsonl(surfaces, [
                raw_hatch(
                    [(0, 0), (10, 0), (10, 10), (0, 10)],
                    layer="ДВ_ПП_Газон_Р",
                ),
                raw_hatch(
                    [(4, 0), (6, 0), (6, 10), (4, 10)],
                    layer="ДВ_ПП_Тип7_Тротуар АБ менее 2м за Газон",
                ),
            ])
            constraints.build(normalized, surfaces, output, root / "report.json")
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            by_type = {
                record["properties"]["object_type"]: shape(record["geometry"])
                for record in records
            }
            self.assertAlmostEqual(by_type["sidewalk_area"].area, 20)
            self.assertAlmostEqual(by_type["base_allowed_area"].area, 80)
            self.assertAlmostEqual(by_type["base_allowed_area"].intersection(
                by_type["sidewalk_area"]
            ).area, 0)

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

    def test_terminal_road_keeps_carriageway_part_beside_sidewalk(self) -> None:
        work = box(0, 0, 20, 100)
        strict = box(5, 0, 15, 60)
        relaxed = box(5, 0, 15, 100)
        sidewalk = box(13, 60, 15, 100)

        road, report = constraints.recover_outer_terminal_road(
            strict,
            relaxed,
            work,
            terminal_exclusion=sidewalk,
        )

        self.assertAlmostEqual(road.area, 920)
        self.assertAlmostEqual(road.intersection(sidewalk).area, 0)
        self.assertAlmostEqual(report["raw_extension_area_in_dxf_square_units"], 400)
        self.assertAlmostEqual(report["excluded_terminal_area_in_dxf_square_units"], 80)
        self.assertAlmostEqual(report["added_area_in_dxf_square_units"], 320)

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

    def test_reviewed_road_overrides_sidewalk_and_reviewed_sidewalk_stays_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            surfaces = root / "surfaces.jsonl"
            corrections = root / "road_review.geojson"
            output = root / "constraints.geojsonl"
            report_path = root / "report.json"
            write_jsonl(normalized, [
                feature("work_boundary", box(0, 0, 20, 10)),
                feature("sidewalk", box(0, 0, 10, 10)),
                feature("building", box(30, 30, 31, 31)),
            ])
            surfaces.write_text("", encoding="utf-8")
            corrections.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature", "id": "road-review",
                        "properties": {"classification": "road", "evidence": "checked"},
                        "geometry": mapping(box(2, 0, 4, 10)),
                    },
                    {
                        "type": "Feature", "id": "sidewalk-review",
                        "properties": {"classification": "sidewalk", "evidence": "checked"},
                        "geometry": mapping(box(12, 0, 14, 10)),
                    },
                ],
            }), encoding="utf-8")

            constraints.build(
                normalized, surfaces, output, report_path,
                road_corrections_path=corrections,
            )
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            by_type = {record["properties"]["object_type"]: shape(record["geometry"]) for record in records}
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(by_type["road_area"].area, 20)
            self.assertFalse(by_type["sidewalk_area"].covers(box(2, 2, 4, 4)))
            self.assertTrue(by_type["sidewalk_area"].covers(box(12, 2, 14, 4)))
            self.assertAlmostEqual(by_type["base_allowed_area"].intersection(by_type["road_area"]).area, 0)
            self.assertEqual(report["road_review_corrections"]["status"], "applied")

    def test_road_review_rejects_conflicting_lawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.geojson"
            path.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"classification": "road"},
                    "geometry": mapping(box(1, 1, 3, 3)),
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "confirmed lawn"):
                constraints.read_road_review_corrections(
                    path, box(0, 0, 10, 10), box(2, 2, 4, 4), box(8, 8, 9, 9)
                )


if __name__ == "__main__":
    unittest.main()
