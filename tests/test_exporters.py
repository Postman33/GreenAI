from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, box, mapping

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.cad_io import debug_export, dxf_exporter
from src.rules import plant_allow_zone_debug
from tests.helpers import feature, raw_hatch, write_jsonl


class ExporterTests(unittest.TestCase):
    def test_standalone_diagnostics_keep_context_and_units_without_loading_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostics.dxf"
            with patch.object(plant_allow_zone_debug.ezdxf, "readfile",
                              side_effect=AssertionError("Source DXF must not be loaded")):
                plant_allow_zone_debug.export_dxf(
                    output, box(0, 0, 20, 20), box(0, 0, 2, 20),
                    box(0, 0, 2, 20), box(2, 0, 3, 20), LineString([(2, 0), (2, 20)]),
                    {"building": box(15, 0, 20, 20),
                     "clean_heat_pipe": LineString([(10, 0), (10, 20)]),
                     "existing_tree": Point(5, 5)},
                    {"shrub": box(3, 0, 9, 20)},
                    rule_exclusions={"DEBUG_EXCL_SHRUB_HEAT_1": box(9, 0, 11, 20)},
                    insunits=6,
                )
            document = ezdxf.readfile(output)
            self.assertEqual(document.header["$INSUNITS"], 6)
            self.assertFalse(document.audit().errors)
            populated_layers = {entity.dxf.layer for entity in document.modelspace()}
            self.assertTrue({
                "DEBUG_WORK_BOUNDARY", "DEBUG_ROAD_AREA", "DEBUG_SIDEWALKS",
                "DEBUG_BUILDINGS", "DEBUG_CLEAN_HEAT_PIPE", "DEBUG_EXISTING_TREES",
                "DEBUG_ALLOW_SHRUB", "DEBUG_EXCL_SHRUB_HEAT_1",
            }.issubset(populated_layers))

    def test_filtered_rule_buffers_match_full_geometry_including_line_ends(self) -> None:
        base = box(0, 0, 10, 10).difference(box(4, 4, 6, 6))
        sources = [
            MultiLineString([[(-100, -1), (100, -1)], [(20, 20), (30, 30)],
                             [(2, 2), (2, 8)], [(11, -5), (11, 20)]]),
            MultiPoint([(5, 5), (0, 0), (100, 100)]),
            MultiPolygon([box(-3, -3, -1, -1), box(2, 2, 3, 3), box(100, 100, 110, 110)]),
            GeometryCollection([LineString([(-20, 8), (20, 8)]), Point(100, 100)]),
            LineString([(100, 100), (110, 110)]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            for radius in (0.0, 1.0, 2.0):
                path.write_text(json.dumps({"plant_types": {"shrub": {"rules": [{
                    "status": "applied", "buffer_distance_in_dxf_units": radius,
                    "target_object_type": "heat_pipe", "rule_code": "SHRUB_HEAT",
                }]}}}), encoding="utf-8")
                for source in sources:
                    layers = plant_allow_zone_debug.build_rule_exclusion_layers(
                        path, base, {"heat_pipe": source})
                    expected = plant_allow_zone_debug.as_polygonal(
                        base.intersection(source.buffer(radius, quad_segs=8)))
                    actual = layers.get("DEBUG_EXCL_SHRUB_HEAT", Polygon())
                    self.assertLess(actual.symmetric_difference(expected).area, 1e-9)

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

    def test_composition_removal_proposals_are_separate_review_layers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dxf"
            zones = root / "zones.jsonl"
            plan = root / "plan.geojsonl"
            report = root / "plan_report.json"
            tree_audit = root / "existing_tree_audit.geojsonl"
            output = root / "overlay.dxf"
            original = ezdxf.new("R2018")
            original.modelspace().add_circle((10, 10), 1, dxfattribs={"layer": "SOURCE_TREE"})
            original.saveas(source)
            write_jsonl(zones, [feature("plant_allow_zone", box(0, 0, 30, 30), plant_type="tree")])
            write_jsonl(plan, [feature("proposed_planting", Point(20, 20),
                                       planting_id="T-1", plant_type="tree", species="Tree",
                                       status="accepted", symbol_radius_m=2,
                                       dxf_units_per_meter=1)])
            report.write_text(json.dumps({"dxf_units_per_meter": 1,
                                          "composition_advisories": [
                {"plant_type": "tree", "existing_tree_id": "tree-1",
                 "recommendation": "propose_removal_from_composition",
                 "coordinates": [10, 10], "reason": "Blocks a grove"},
                {"plant_type": "shrub", "existing_shrub_id": "shrub-1",
                 "recommendation": "propose_removal_from_composition",
                 "coordinates": [15, 15], "reason": "Blocks a shrub mass"},
            ]}), encoding="utf-8")
            write_jsonl(tree_audit, [{
                "type": "Feature", "id": "ET-0001",
                "geometry": {"type": "Point", "coordinates": [10, 10]},
                "properties": {
                    "object_type": "existing_tree_rule_screening",
                    "existing_tree_id": "ET-0001", "status": "conflict",
                    "checks": [{"code": "TREE_WATER_2", "status": "conflict"}],
                },
            }])
            dxf_exporter.export_zones(source, zones, output, 0.65,
                                      planting_plan_path=plan,
                                      composition_report_path=report,
                                      existing_tree_audit_path=tree_audit)
            result = ezdxf.readfile(output)
            self.assertEqual(len(result.modelspace().query('CIRCLE[layer=="SOURCE_TREE"]')), 1)
            self.assertEqual(len(result.modelspace().query('CIRCLE[layer=="GREEN_AI_REMOVE_TREE_REVIEW"]')), 1)
            self.assertEqual(len(result.modelspace().query('CIRCLE[layer=="GREEN_AI_REMOVE_SHRUB_REVIEW"]')), 1)
            self.assertEqual(len(result.modelspace().query('CIRCLE[layer=="GREEN_AI_EXISTING_TREE_CONFLICT"]')), 1)
            conflict = result.modelspace().query('CIRCLE[layer=="GREEN_AI_EXISTING_TREE_CONFLICT"]')[0]
            self.assertIn("id=ET-0001", [value for code, value in conflict.get_xdata("GREEN_AI") if code == 1000])
            marker = result.modelspace().query('CIRCLE[layer=="GREEN_AI_REMOVE_TREE_REVIEW"]')[0]
            self.assertIn("id=tree-1", [value for code, value in marker.get_xdata("GREEN_AI")
                                        if code == 1000])
            self.assertFalse(result.audit().errors)

            overlay = root / "overlay_only.dxf"
            dxf_exporter.export_zones(source, zones, overlay, 0.65,
                                      planting_plan_path=plan,
                                      composition_report_path=report,
                                      existing_tree_audit_path=tree_audit,
                                      overlay_only=True, insunits=6)
            only = ezdxf.readfile(overlay)
            self.assertEqual(len(only.modelspace().query('CIRCLE[layer=="SOURCE_TREE"]')), 0)
            self.assertEqual(len(only.modelspace().query('CIRCLE[layer=="GREEN_AI_REMOVE_TREE_REVIEW"]')), 1)
            self.assertEqual(len(only.modelspace().query('CIRCLE[layer=="GREEN_AI_EXISTING_TREE_CONFLICT"]')), 1)
            self.assertFalse(only.audit().errors)

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
                existing_tree_audit=[{
                    "type": "Feature", "id": "ET-0001",
                    "geometry": {"type": "Point", "coordinates": [3, 3]},
                    "properties": {
                        "existing_tree_id": "ET-0001", "status": "conflict",
                        "checks": [{"target": "water_pipe", "status": "conflict"}],
                    },
                }],
            )
            result = ezdxf.readfile(actual)
            self.assertTrue(result.layers.get("DEBUG_WATER_PIPE").is_off())
            self.assertFalse(result.layers.get("DEBUG_EXISTING_TREES").is_off())
            self.assertEqual(result.layers.get("DEBUG_EXISTING_TREES").color, 6)
            entities = list(result.modelspace())
            tree_symbols = [entity for entity in entities if entity.dxf.layer == "DEBUG_EXISTING_TREES"]
            self.assertEqual(len(tree_symbols), 3)
            redraw_order = dict(result.modelspace().get_redraw_order())
            self.assertTrue(all(redraw_order[entity.dxf.handle] == "0" for entity in tree_symbols))
            last_hatch = max(index for index, entity in enumerate(entities) if entity.dxftype() == "HATCH")
            self.assertTrue(all(entities.index(entity) > last_hatch for entity in tree_symbols))
            self.assertFalse(result.layers.get("DEBUG_EXISTING_TREE_CONFLICTS").is_off())
            self.assertTrue(result.layers.get("DEBUG_EXISTING_TREE_CONFLICT_IDS").is_off())
            self.assertTrue(result.layers.get("DEBUG_EXISTING_TREE_CONFLICT_WATER_PIPE").is_off())
            self.assertEqual(len(result.modelspace().query(
                'CIRCLE[layer=="DEBUG_EXISTING_TREE_CONFLICT_WATER_PIPE"]'
            )), 1)
            conflict_symbols = list(result.modelspace().query(
                'CIRCLE[layer=="DEBUG_EXISTING_TREE_CONFLICTS"]'
            ))
            self.assertEqual(len(conflict_symbols), 1)
            self.assertEqual(conflict_symbols[0].dxf.radius, 1.15)
            self.assertEqual(redraw_order[conflict_symbols[0].dxf.handle], "0")
            self.assertIn(
                "id=ET-0001",
                [value for code, value in conflict_symbols[0].get_xdata("GREEN_AI") if code == 1000],
            )
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
                {"existing_tree": Point(5, 5)},
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
            self.assertTrue(result.layers.get("DEBUG_PLANT_SHRUB").is_off())
            self.assertTrue(result.layers.get("DEBUG_PLANT_HERBACEOUS").is_off())
            self.assertFalse(result.layers.get("DEBUG_PLANT_SHRUB_OUTLINE").is_off())
            self.assertFalse(result.layers.get("DEBUG_PLANT_HERBACEOUS_OUTLINE").is_off())
            self.assertGreater(
                len(modelspace.query('LWPOLYLINE[layer=="DEBUG_PLANT_SHRUB_OUTLINE"]')),
                0,
            )
            tree_markers = list(modelspace.query('CIRCLE[layer=="DEBUG_EXISTING_TREES"]'))
            self.assertEqual(len(tree_markers), 1)
            self.assertEqual(tree_markers[0].dxf.radius, 0.75)
            self.assertFalse(result.layers.get("DEBUG_EXISTING_TREES").is_off())

    def test_diagnostic_export_preserves_all_planned_points_and_shrub_areas(self) -> None:
        for units_per_meter in (1.0, 1000.0):
            with self.subTest(units_per_meter=units_per_meter), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                work = box(0, 0, 10 * units_per_meter, 10 * units_per_meter)
                allowed = box(0, 0, 9 * units_per_meter, 10 * units_per_meter)
                points = [
                    ("T-0001", "tree", Point(2 * units_per_meter, 2 * units_per_meter)),
                    ("S-0001", "shrub", Point(4 * units_per_meter, 4 * units_per_meter)),
                    ("S-0002", "shrub", Point(6 * units_per_meter, 6 * units_per_meter)),
                    ("H-0001", "herbaceous", Point(8 * units_per_meter, 8 * units_per_meter)),
                ]
                check = {
                    "code": "SHRUB_HEAT_1", "status": "passed",
                    "actual_distance_m": 3.5, "required_distance_m": 1.0,
                    "norm_reference": "test norm, table 6.3",
                }
                write_jsonl(root / "plan.jsonl", [
                    feature(
                        "proposed_planting", point, planting_id=planting_id,
                        plant_type=plant_type, species="Test species", status="accepted",
                        symbol_radius_m=0.5, dxf_units_per_meter=units_per_meter,
                        checks=[check] if plant_type == "shrub" else [],
                    )
                    for planting_id, plant_type, point in points
                ] + [feature("proposed_planting_area", allowed, plant_type="shrub")])
                write_jsonl(root / "zones.jsonl", [
                    feature("plant_allow_zone", allowed, plant_type="shrub"),
                ])
                write_jsonl(root / "constraints.jsonl", [
                    feature("hard_surface_area", work.difference(allowed)),
                    feature("base_allowed_area", allowed),
                ])
                write_jsonl(root / "normalized.jsonl", [feature("work_boundary", work)])
                write_jsonl(root / "decisions.jsonl", [
                    feature("planting_decision", Point(0, 0), plant_type="shrub",
                            candidate_id="R-S-0001", status="rejected"),
                ])
                (root / "zone_report.json").write_text(
                    json.dumps({"plant_types": {}}), encoding="utf-8"
                )

                plant_allow_zone_debug.build_debug_export(
                    root / "zones.jsonl", root / "constraints.jsonl",
                    root / "normalized.jsonl", root / "debug.dxf", None, 180,
                    planting_plan_path=root / "plan.jsonl",
                    planting_decisions_path=root / "decisions.jsonl",
                    zone_report_path=root / "zone_report.json",
                    legend_output_path=root / "legend.md",
                    insunits=6 if units_per_meter == 1.0 else 4,
                )

                document = ezdxf.readfile(root / "debug.dxf")
                self.assertFalse(document.audit().errors)
                modelspace = document.modelspace()
                for plant_type, layer, expected_count, color in (
                    ("tree", "DEBUG_PLANT_TREE", 1, 3),
                    ("shrub", "DEBUG_PLANT_SHRUB_POINTS", 2, 2),
                    ("herbaceous", "DEBUG_PLANT_HERBACEOUS_POINTS", 1, 94),
                ):
                    markers = list(modelspace.query(f'CIRCLE[layer=="{layer}"]'))
                    self.assertEqual(len(markers), expected_count)
                    expected = {
                        planting_id: point for planting_id, kind, point in points
                        if kind == plant_type
                    }
                    observed_ids = set()
                    for marker in markers:
                        metadata = dict(
                            str(tag.value).split("=", 1)
                            for tag in marker.get_xdata("GREEN_AI") if tag.code == 1000
                        )
                        observed_ids.add(metadata["id"])
                        point = expected[metadata["id"]]
                        self.assertAlmostEqual(marker.dxf.center.x, point.x)
                        self.assertAlmostEqual(marker.dxf.center.y, point.y)
                        self.assertAlmostEqual(marker.dxf.radius, 0.5 * units_per_meter)
                        self.assertEqual(marker.dxf.color, color)
                        self.assertEqual(metadata["status"], "accepted")
                        self.assertEqual(metadata["type"], plant_type)
                    self.assertEqual(observed_ids, set(expected))
                    self.assertFalse(document.layers.get(layer).is_off())
                    self.assertFalse(document.layers.get(layer).is_frozen())
                    self.assertTrue(document.layers.get(f"DEBUG_PLANT_{plant_type.upper()}_IDS").is_off())
                self.assertEqual(len(modelspace.query('HATCH[layer=="DEBUG_PLANT_SHRUB"]')), 1)
                self.assertEqual(len(modelspace.query('CIRCLE[layer=="DEBUG_PLANT_SHRUB"]')), 0)
                self.assertEqual(len(modelspace.query('CIRCLE[layer=="DEBUG_REJECTED_SHRUB"]')), 1)
                reasons = list(modelspace.query('MTEXT[layer=="DEBUG_PLANT_SHRUB_REASONS"]'))
                self.assertEqual(len(reasons), 2)
                self.assertTrue(document.layers.get("DEBUG_PLANT_SHRUB_REASONS").is_off())
                self.assertNotIn("DEBUG_REASON_S_0001", document.layers)
                for reason in reasons:
                    self.assertIn("SHRUB_HEAT_1", reason.text)
                    self.assertIn("3.50 m >= 1.00 m", reason.text)
                    self.assertIn("test norm, table 6.3", reason.text)
                    self.assertAlmostEqual(reason.dxf.rotation, 0.0)
                legend = (root / "legend.md").read_text(encoding="utf-8")
                self.assertIn("DEBUG_PLANT_SHRUB_POINTS", legend)
                self.assertIn("DEBUG_PLANT_*_REASONS", legend)

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
