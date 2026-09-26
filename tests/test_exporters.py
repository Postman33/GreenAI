from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point, Polygon, box, mapping

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.cad_io import debug_export, dxf_exporter
from src.rules import plant_allow_zone_debug
from tests.helpers import feature, raw_hatch, write_jsonl


class ExporterTests(unittest.TestCase):
    def test_diagnostic_export_accepts_drawing_without_reconstructed_road(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zones = root / "zones.geojsonl"
            constraints = root / "constraints.geojsonl"
            normalized = root / "normalized.geojsonl"
            output = root / "debug.dxf"
            write_jsonl(zones, [
                feature("plant_allow_zone", box(1, 1, 4, 4), plant_type="shrub"),
            ])
            write_jsonl(constraints, [
                feature("hard_surface_area", box(8, 0, 10, 10)),
                feature("base_allowed_area", box(0, 0, 8, 10)),
            ])
            write_jsonl(normalized, [feature("work_boundary", box(0, 0, 10, 10))])

            plant_allow_zone_debug.build_debug_export(
                zones, constraints, normalized, output, None, 180,
            )

            document = ezdxf.readfile(output)
            self.assertEqual(len(document.audit().errors), 0)
            self.assertEqual(
                len(document.modelspace().query('*[layer=="DEBUG_ROAD_AREA"]')),
                0,
            )

    def test_diagnostic_export_accepts_drawing_without_sidewalk_area(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zones = root / "zones.geojsonl"
            constraints = root / "constraints.geojsonl"
            normalized = root / "normalized.geojsonl"
            output = root / "debug.dxf"
            work = box(0, 0, 10, 10)
            write_jsonl(zones, [
                feature("plant_allow_zone", box(1, 1, 4, 4), plant_type="shrub"),
            ])
            write_jsonl(constraints, [
                feature("hard_surface_area", box(8, 0, 10, 10)),
                feature("road_area", box(8, 0, 10, 10)),
                feature("base_allowed_area", box(0, 0, 8, 10)),
            ])
            write_jsonl(normalized, [feature("work_boundary", work)])

            plant_allow_zone_debug.build_debug_export(
                zones, constraints, normalized, output, None, 180,
            )

            document = ezdxf.readfile(output)
            self.assertEqual(len(document.audit().errors), 0)
            self.assertGreater(
                len(document.modelspace().query('*[layer=="DEBUG_ROAD_AREA"]')),
                0,
            )

    def test_diagnostic_setback_uses_reconstructed_sidewalk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zones = root / "zones.geojsonl"
            constraints = root / "constraints.geojsonl"
            normalized = root / "normalized.geojsonl"
            report = root / "zone_report.json"
            legend = root / "legend.md"
            output = root / "debug.dxf"
            write_jsonl(zones, [
                feature("plant_allow_zone", box(0, 0, 3, 10), plant_type="tree"),
            ])
            write_jsonl(constraints, [
                feature("hard_surface_area", box(4, 0, 6, 10)),
                feature("road_area", box(8, 0, 10, 10)),
                feature("base_allowed_area", box(0, 0, 10, 10)),
                feature("sidewalk_area", box(4, 0, 6, 10)),
            ])
            write_jsonl(normalized, [
                feature("work_boundary", box(0, 0, 10, 10)),
            ])
            report.write_text(json.dumps({"plant_types": {"tree": {"rules": [
                {"rule_code": "TREE_SIDEWALK_0_7", "target_object_type": "sidewalk",
                 "status": "applied", "buffer_distance_in_dxf_units": 0.7},
            ]}}}), encoding="utf-8")
            plant_allow_zone_debug.build_debug_export(
                zones, constraints, normalized, output, None, 180,
                zone_report_path=report, legend_output_path=legend,
            )
            document = ezdxf.readfile(output)
            self.assertGreater(len(document.modelspace().query(
                '*[layer=="DEBUG_EXCL_TREE_SIDEWALK_0_7"]'
            )), 0)
            self.assertIn("DEBUG_EXCL_TREE_SIDEWALK_0_7", legend.read_text(encoding="utf-8"))

    def test_diagnostic_layers_separate_applied_setbacks_from_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "zone_report.json"
            legend = root / "legend.md"
            report.write_text(json.dumps({"plant_types": {
                "tree": {"rules": [
                    {"rule_code": "TREE_WATER_2", "target_object_type": "water_pipe",
                     "status": "applied", "buffer_distance_in_dxf_units": 2},
                    {"rule_code": "TREE_OVERHEAD_POWER_MANUAL",
                     "target_object_type": "overhead_power_line",
                     "status": "manual_review", "reason": "Voltage unknown"},
                ]},
                "shrub": {"rules": [
                    {"rule_code": "SHRUB_HEAT_1", "target_object_type": "heat_pipe",
                     "status": "applied", "buffer_distance_in_dxf_units": 1},
                ]},
            }}), encoding="utf-8")
            context = {
                "clean_water_pipe": LineString([(5, 0), (5, 10)]),
                "clean_heat_pipe": LineString([(6, 0), (6, 10)]),
                "clean_overhead_power_line": LineString([(8, 0), (8, 10)]),
            }
            exclusions = plant_allow_zone_debug.build_rule_exclusion_layers(
                report, box(0, 0, 10, 10), context,
            )
            self.assertEqual(set(exclusions), {
                "DEBUG_EXCL_TREE_WATER_2", "DEBUG_EXCL_SHRUB_HEAT_1",
            })
            self.assertTrue(exclusions["DEBUG_EXCL_TREE_WATER_2"].covers(Point(5, 5)))
            self.assertTrue(exclusions["DEBUG_EXCL_SHRUB_HEAT_1"].covers(Point(6, 5)))
            plant_allow_zone_debug.export_diagnostic_legend(
                legend, report, exclusions, context,
            )
            content = legend.read_text(encoding="utf-8")
            self.assertIn("DEBUG_EXCL_TREE_WATER_2", content)
            self.assertIn("DEBUG_CLEAN_OVERHEAD_POWER", content)
            self.assertIn("manual_review", content)

    def test_export_repairs_invalid_source_dictionary_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dxf"
            zones = root / "zones.jsonl"
            output = root / "result.dxf"
            document = ezdxf.new("R2013")
            dictionary = document.rootdict.add_new_dict("TestBad")
            record = document.objects.add_xrecord(owner="FFFF")
            dictionary["wrong_owner"] = record
            document.modelspace().add_line((0, 0), (1, 1))
            document.saveas(source)
            write_jsonl(zones, [feature("plant_allow_zone", box(2, 2, 4, 4), plant_type="tree")])
            self.assertEqual(len(ezdxf.readfile(source).audit().fixes), 1)

            dxf_exporter.export_zones(source, zones, output, 0.5)

            result = ezdxf.readfile(output)
            self.assertEqual(len(result.audit().fixes), 0)
            self.assertEqual(len(result.modelspace().query("LINE")), 1)

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

    def test_final_dxf_export_adds_concrete_planting_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dxf"
            zones = root / "zones.jsonl"
            plan = root / "plan.geojsonl"
            output = root / "result.dxf"
            document = ezdxf.new("R2013")
            document.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "SOURCE"})
            document.saveas(source)
            write_jsonl(zones, [feature("plant_allow_zone", box(0, 0, 10, 10), plant_type="tree")])
            plan.write_text(
                json.dumps({
                    "type": "Feature",
                    "id": "T-0001",
                    "properties": {
                        "object_type": "proposed_planting",
                        "planting_id": "T-0001",
                        "plant_type": "tree",
                        "species": "Test tree",
                        "status": "accepted",
                        "symbol_radius_m": 2.5,
                        "dxf_units_per_meter": 1.0,
                    },
                    "geometry": mapping(Point(5, 5)),
                }) + "\n" + json.dumps({
                    "type": "Feature",
                    "id": "H-0001",
                    "properties": {
                        "object_type": "proposed_planting_area",
                        "planting_id": "H-0001",
                        "plant_type": "herbaceous",
                        "species": "Test grass",
                        "status": "accepted",
                    },
                    "geometry": mapping(box(1, 1, 4, 4)),
                }) + "\n",
                encoding="utf-8",
            )

            dxf_exporter.export_zones(
                source, zones, output, 0.65, planting_plan_path=plan
            )

            result = ezdxf.readfile(output)
            trees = list(result.modelspace().query('CIRCLE[layer=="GREEN_AI_PLANT_TREE"]'))
            self.assertEqual(len(trees), 1)
            self.assertAlmostEqual(trees[0].dxf.radius, 2.5)
            self.assertTrue(result.layers.get("GREEN_AI_ZONE_TREE").is_off())
            self.assertFalse(result.layers.get("GREEN_AI_PLANT_TREE").is_off())
            metadata = trees[0].get_xdata("GREEN_AI")
            self.assertIn("id=T-0001", [value for code, value in metadata if code == 1000])
            areas = list(result.modelspace().query('HATCH[layer=="GREEN_AI_HERBACEOUS"]'))
            self.assertEqual(len(areas), 1)
            area_metadata = areas[0].get_xdata("GREEN_AI")
            self.assertIn("id=H-0001", [value for code, value in area_metadata if code == 1000])

    def test_overlay_export_contains_only_result_entities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dxf"
            zones = root / "zones.jsonl"
            constraints = root / "constraints.jsonl"
            plan = root / "plan.geojsonl"
            output = root / "overlay.dxf"
            document = ezdxf.new("R2013")
            document.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "SOURCE"})
            document.saveas(source)
            write_jsonl(
                zones,
                [feature("plant_allow_zone", box(0, 0, 10, 10), plant_type="tree")],
            )
            write_jsonl(constraints, [feature("road_area", box(0, 0, 2, 10))])
            write_jsonl(
                plan,
                [
                    feature(
                        "proposed_planting",
                        Point(5, 5),
                        planting_id="T-0001",
                        plant_type="tree",
                        species="Test tree",
                        status="accepted",
                        symbol_radius_m=2.0,
                        dxf_units_per_meter=1.0,
                    )
                ],
            )

            dxf_exporter.export_zones(
                source,
                zones,
                output,
                0.65,
                constraint_map_path=constraints,
                planting_plan_path=plan,
                overlay_only=True,
                insunits=6,
            )

            result = ezdxf.readfile(output)
            self.assertEqual(result.header["$INSUNITS"], 6)
            self.assertEqual(len(result.modelspace().query('*[layer=="SOURCE"]')), 0)
            self.assertEqual(
                len(result.modelspace().query('HATCH[layer=="GREEN_AI_RECONSTRUCTED_ROAD"]')),
                1,
            )
            self.assertFalse(result.layers.get("GREEN_AI_RECONSTRUCTED_ROAD").is_off())
            self.assertEqual(
                len(result.modelspace().query('CIRCLE[layer=="GREEN_AI_PLANT_TREE"]')),
                1,
            )
            self.assertNotIn("GREEN_AI_ZONE_TREE", result.layers)

    def test_planting_area_keeps_hatch_hole_without_visible_inner_outline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dxf"
            zones = root / "zones.jsonl"
            plan = root / "plan.geojsonl"
            output = root / "overlay.dxf"
            ezdxf.new("R2018").saveas(source)
            write_jsonl(
                zones,
                [feature("plant_allow_zone", box(0, 0, 10, 10), plant_type="shrub")],
            )
            shrub_bed = Polygon(
                [(0, 0), (10, 0), (10, 10), (0, 10)],
                holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]],
            )
            write_jsonl(
                plan,
                [
                    feature(
                        "proposed_planting_area",
                        shrub_bed,
                        planting_id="SA-0001",
                        plant_type="shrub",
                        species="Test shrub",
                        status="accepted",
                    )
                ],
            )

            dxf_exporter.export_zones(
                source,
                zones,
                output,
                0.65,
                planting_plan_path=plan,
                overlay_only=True,
            )

            result = ezdxf.readfile(output)
            hatches = list(
                result.modelspace().query('HATCH[layer=="GREEN_AI_PLANT_SHRUB"]')
            )
            outlines = list(
                result.modelspace().query('LWPOLYLINE[layer=="GREEN_AI_PLANT_SHRUB"]')
            )
            self.assertEqual(len(hatches), 1)
            self.assertEqual(len(hatches[0].paths), 2)
            self.assertEqual(len(outlines), 1)

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
                    "building": box(6, 2, 9, 6),
                    "building_source": LineString([(6, 2), (9, 2)]),
                    "water_pipe": LineString([(2, 0), (2, 10)]),
                    "existing_tree": Point(3, 3),
                },
                {"tree": box(1, 1, 3, 3)},
            )
            result = ezdxf.readfile(actual)
            self.assertTrue(result.layers.get("DEBUG_WATER_PIPE").is_off())
            self.assertFalse(result.layers.get("DEBUG_EXISTING_TREES").is_off())
            self.assertEqual(
                len(result.modelspace().query('HATCH[layer=="DEBUG_BUILDINGS"]')),
                1,
            )
            self.assertGreater(
                len(result.modelspace().query('*[layer=="DEBUG_BUILDING_SOURCE"]')),
                0,
            )
            self.assertGreater(len(result.modelspace().query('*[layer=="DEBUG_ALLOW_TREE"]')), 0)

    def test_debug_dxf_separates_planted_uncovered_and_unused_areas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "debug_plan.dxf"
            actual = plant_allow_zone_debug.export_dxf(
                output,
                box(0, 0, 10, 10),
                Polygon(),
                Polygon(),
                Polygon(),
                LineString(),
                {},
                {
                    "tree": box(0, 0, 10, 10),
                    "shrub": box(0, 0, 10, 10),
                },
                box(0, 0, 10, 10),
                {
                    "shrub": box(0, 0, 8, 10),
                    "herbaceous": box(8, 0, 10, 10),
                },
                {
                    "tree": [
                        (
                            Point(5, 5),
                            {
                                "symbol_radius_m": 1.0,
                                "dxf_units_per_meter": 1.0,
                            },
                        )
                    ]
                },
            )
            result = ezdxf.readfile(actual)
            modelspace = result.modelspace()
            self.assertGreater(
                len(modelspace.query('HATCH[layer=="DEBUG_PLANT_SHRUB"]')), 0
            )
            self.assertGreater(
                len(modelspace.query('HATCH[layer=="DEBUG_SHRUB_UNCOVERED"]')), 0
            )
            self.assertGreater(
                len(modelspace.query('HATCH[layer=="DEBUG_TREE_ALLOWED_UNUSED"]')), 0
            )
            self.assertEqual(
                len(modelspace.query('CIRCLE[layer=="DEBUG_PLANT_TREE"]')), 1
            )
            self.assertTrue(result.layers.get("DEBUG_SHRUB_UNCOVERED").is_off())
            self.assertFalse(result.layers.get("DEBUG_PLANT_SHRUB").is_off())

    def test_debug_dxf_draws_rejected_candidates_in_red_with_reason_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "debug_rejected.dxf"
            rejected = {
                "tree": [
                    (
                        Point(4, 5),
                        {
                            "candidate_id": "R-T-0001",
                            "planting_id": "R-T-0001",
                            "plant_type": "tree",
                            "species": "Test tree",
                            "status": "rejected",
                            "checks": [
                                {
                                    "code": "PLANT_FOOTPRINT_INSIDE_ZONE",
                                    "status": "failed",
                                    "actual_distance_m": 1.0,
                                    "required_distance_m": 2.5,
                                    "norm_reference": "project parameter",
                                    "explanation": "Footprint crosses the zone boundary.",
                                }
                            ],
                        },
                    )
                ]
            }
            actual = plant_allow_zone_debug.export_dxf(
                output,
                box(0, 0, 10, 10),
                Polygon(),
                Polygon(),
                Polygon(),
                LineString(),
                {},
                {"tree": box(0, 0, 10, 10)},
                rejected_points=rejected,
            )
            result = ezdxf.readfile(actual)
            modelspace = result.modelspace()
            markers = list(
                modelspace.query('CIRCLE[layer=="DEBUG_REJECTED_TREE"]')
            )
            self.assertEqual(len(markers), 1)
            self.assertEqual(markers[0].dxf.color, 1)
            self.assertTrue(markers[0].has_xdata("GREEN_AI"))
            metadata_values = [
                str(item.value) for item in markers[0].get_xdata("GREEN_AI")
            ]
            self.assertIn(
                "title_1=Крона растения не помещается в допустимой зоне",
                metadata_values,
            )
            self.assertIn(
                "metric_1=Фактически 1.00 м; требуется 2.50 м; не хватает 1.50 м.",
                metadata_values,
            )
            self.assertTrue(
                any(value.startswith("advice_1=Сдвиньте центр растения") for value in metadata_values)
            )
            self.assertIn("norm_1=project parameter", metadata_values)
            self.assertNotIn("DEBUG_REJECT_REASON_R_T_0001", result.layers)
            self.assertIn("DEBUG_REJECT_REASONS", result.layers)
            self.assertTrue(result.layers.get("DEBUG_REJECT_REASONS").is_off())
            self.assertFalse(result.layers.get("DEBUG_REJECTED_TREE_IDS").is_off())
            reasons = list(
                modelspace.query('MTEXT[layer=="DEBUG_REJECT_REASONS"]')
            )
            self.assertEqual(len(reasons), 1)
            self.assertAlmostEqual(reasons[0].dxf.rotation, 0.0)

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
