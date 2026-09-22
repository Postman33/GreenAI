from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf
from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.lldxf.types import DXFTag
from shapely.geometry import Point, box

from tests import ROOT
import inspect_dxf
from scripts import dxf_manifest, pipeline_cache, seed, verify_outputs
from src import loader, utils
from tests.helpers import feature, write_jsonl


class ScriptTests(unittest.TestCase):
    def test_manifest_treats_explicit_default_ellipse_extrusion_as_unchanged(self) -> None:
        ellipse = ezdxf.new("R2018").modelspace().add_ellipse(
            (1, 2), major_axis=(2, 0), ratio=0.8
        )
        collector = TagCollector(dxfversion="AC1032")
        ellipse.export_dxf(collector)
        without_extrusion = [tag for tag in collector.tags if tag.code not in {210, 220, 230}]
        with_extrusion = [
            *without_extrusion,
            DXFTag(210, 0.0),
            DXFTag(220, 0.0),
            DXFTag(230, 1.0),
        ]
        self.assertEqual(
            dxf_manifest.semantic_fingerprint("ELLIPSE", without_extrusion, "AC1032"),
            dxf_manifest.semantic_fingerprint("ELLIPSE", with_extrusion, "AC1032"),
        )

    def test_pipeline_cache_reuses_only_unchanged_complete_preprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            output = workspace / "output"
            workspace.mkdir()
            output.mkdir()
            input_dxf = workspace / "input.dxf"
            input_dxf.write_text("input", encoding="utf-8")
            for relative in pipeline_cache.DEPENDENCIES:
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
            for name in pipeline_cache.ARTIFACTS:
                (output / name).write_text(name, encoding="utf-8")
            manifest = output / "preprocessing_cache.json"
            key = pipeline_cache.build_cache_key(workspace, input_dxf, None, "auto")
            pipeline_cache.write_manifest(manifest, key, output)

            self.assertTrue(
                pipeline_cache.validate_manifest(manifest, key, output)["hit"]
            )
            (output / pipeline_cache.ARTIFACTS[0]).write_text(
                "changed", encoding="utf-8"
            )
            self.assertFalse(
                pipeline_cache.validate_manifest(manifest, key, output)["hit"]
            )

    def write_minimal_verifier_inputs(
        self, root: Path, zone_geometry=box(1, 1, 4, 4)
    ) -> tuple[Path, Path]:
        constraint_map = root / "constraints.jsonl"
        zones = root / "zones.jsonl"
        base = box(0, 0, 10, 10)
        write_jsonl(
            constraint_map,
            [
                feature("base_allowed_area", base),
                feature("road_area", box(20, 20, 21, 21)),
                feature("hard_surface_area", box(30, 30, 31, 31)),
                feature("sidewalk_area", box(40, 40, 41, 41)),
                feature("buildings_in_work_area", box(50, 50, 51, 51)),
                feature("utility_well_footprints", box(60, 60, 61, 61)),
                feature("confirmed_plantable_surface", base),
            ],
        )
        zone = feature(
            "plant_allow_zone", zone_geometry, plant_type="shrub"
        )
        zone["properties"]["area_in_dxf_square_units"] = zone_geometry.area
        write_jsonl(zones, [zone])
        return constraint_map, zones

    def test_inspector_expands_insert_geometry(self) -> None:
        document = ezdxf.new("R2018")
        block = document.blocks.new("TEST_BLOCK")
        block.add_line((0, 0), (2, 0), dxfattribs={"layer": "GEOMETRY"})
        insert = document.modelspace().add_blockref("TEST_BLOCK", (10, 20))
        records = list(inspect_dxf.records_for_entity(insert))
        self.assertEqual(records[0]["type"], "INSERT")
        child = next(record for record in records if record["type"] == "LINE")
        self.assertEqual(child["geometry"]["start"][:2], [10.0, 20.0])
        self.assertEqual(child["geometry"]["end"][:2], [12.0, 20.0])

    def test_utils_preserves_loader_compatibility_exports(self) -> None:
        self.assertIs(utils.extract, loader.extract)
        self.assertIs(utils.record, loader.record)

    def test_seed_catalog_and_rule_codes_are_unique(self) -> None:
        self.assertEqual(len(seed.PLANTS), len({plant.name for plant in seed.PLANTS}))
        self.assertEqual(
            len(seed.PLACEMENT_RULES),
            len({rule.code for rule in seed.PLACEMENT_RULES}),
        )
        generated = seed.distance_rule("TEST_RULE", "tree", "building", 2.5)
        self.assertEqual(generated.conditions["check"], "min_distance")
        self.assertEqual(generated.conditions["min_distance_m"], 2.5)
        manual = seed.manual_rule("TEST_MANUAL", "tree", "gas_pipe", "inspect")
        self.assertEqual(manual.conditions["check"], "manual_review")

        tree_gas_rule = next(
            rule for rule in seed.PLACEMENT_RULES if rule.code == "TREE_GAS_1_5"
        )
        self.assertEqual(tree_gas_rule.conditions["check"], "min_distance")
        self.assertEqual(tree_gas_rule.conditions["min_distance_m"], 1.5)

    def test_plugin_rules_match_database_seed_rules(self) -> None:
        config = json.loads(
            (ROOT / "nanocad-plugin" / "config" / "greenai.plugin.json").read_text(
                encoding="utf-8"
            )
        )
        plugin_rules = {item["code"]: item for item in config["rules"]}
        seed_rules = {item.code: item for item in seed.PLACEMENT_RULES}
        self.assertEqual(set(plugin_rules), set(seed_rules))
        for code, seeded in seed_rules.items():
            plugin = plugin_rules[code]
            self.assertEqual(plugin["plantType"], seeded.plant_type, code)
            self.assertEqual(plugin["targetObjectType"], seeded.target_object, code)
            self.assertEqual(plugin["check"], seeded.conditions["check"], code)
            self.assertEqual(plugin["normReference"], seeded.norm_reference, code)
            if seeded.conditions["check"] == "min_distance":
                self.assertEqual(
                    plugin.get("minDistanceM"),
                    seeded.conditions["min_distance_m"],
                    code,
                )
            else:
                self.assertEqual(
                    plugin.get("reason"), seeded.conditions["reason"], code
                )

    def test_verifier_accepts_zone_inside_all_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraint_map = root / "constraints.jsonl"
            zones = root / "zones.jsonl"
            report = root / "verification.json"
            base = box(0, 0, 10, 10)
            write_jsonl(
                constraint_map,
                [
                    feature("base_allowed_area", base),
                    feature("road_area", box(20, 20, 21, 21)),
                    feature("hard_surface_area", box(30, 30, 31, 31)),
                    feature("sidewalk_area", box(40, 40, 41, 41)),
                    feature("buildings_in_work_area", box(50, 50, 51, 51)),
                    feature("utility_well_footprints", box(60, 60, 61, 61)),
                    feature("confirmed_plantable_surface", base),
                ],
            )
            zone = feature("plant_allow_zone", box(1, 1, 2, 2), plant_type="tree")
            zone["properties"]["area_in_dxf_square_units"] = 1.0
            write_jsonl(zones, [zone])
            with patch.object(
                sys,
                "argv",
                ["verify_outputs.py", str(constraint_map), str(zones), "--output", str(report)],
            ):
                verify_outputs.main()
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "passed")
            self.assertEqual(data["checks"][0]["sidewalk_overlap_area"], 0)
            self.assertEqual(data["checks"][0]["building_overlap_area"], 0)
            self.assertEqual(data["checks"][0]["utility_well_overlap_area"], 0)

    def test_verifier_rejects_unconfirmed_dxf_scale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints, zones = self.write_minimal_verifier_inputs(root)
            zone_report = root / "zone_report.json"
            verification = root / "verification.json"
            zone_report.write_text(
                json.dumps(
                    {
                        "constraint_map_input": str(constraints),
                        "normalized_input": None,
                        "utility_geometry_input": None,
                        "unit_metadata_input": None,
                        "output": str(zones),
                        "dxf_units_per_meter": 1.0,
                        "unit_assumption_requires_confirmation": True,
                        "plant_types": {
                            "shrub": {
                                "allowed_area_in_dxf_square_units": 9.0,
                                "rules": [],
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.object(
                sys,
                "argv",
                [
                    "verify_outputs.py",
                    str(constraints),
                    str(zones),
                    "--zone-report",
                    str(zone_report),
                    "--output",
                    str(verification),
                ],
            ):
                with self.assertRaises(SystemExit):
                    verify_outputs.main()

            data = json.loads(verification.read_text(encoding="utf-8"))
            self.assertIn("DXF unit scale is not confirmed", data["failures"])

    def test_verifier_distinguishes_npa_from_internal_reference(self) -> None:
        self.assertFalse(
            verify_outputs.has_npa_reference(
                [{"norm_reference": "plant_catalog#1: test plant"}]
            )
        )
        self.assertTrue(
            verify_outputs.has_npa_reference(
                [{"norm_reference": "Постановление Правительства Москвы № 743-ПП, п. 2.1.13"}]
            )
        )

    def test_verifier_rejects_area_planting_outside_its_plant_zone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints, zones = self.write_minimal_verifier_inputs(root)
            plan = root / "plan.geojsonl"
            report = root / "report.json"
            area = feature(
                "proposed_planting_area",
                box(2, 2, 6, 6),
                planting_id="shrub-area-1",
                plant_type="shrub",
                status="accepted",
                checks=[
                    {
                        "code": "ALLOWED_ZONE",
                        "status": "passed",
                        "norm_reference": "СП 42.13330.2026, таблица 6.3",
                        "explanation": "Test explanation",
                    }
                ],
            )
            write_jsonl(plan, [area])
            with patch.object(
                sys,
                "argv",
                [
                    "verify_outputs.py",
                    str(constraints),
                    str(zones),
                    "--planting-plan",
                    str(plan),
                    "--output",
                    str(report),
                ],
            ):
                with self.assertRaises(SystemExit):
                    verify_outputs.main()
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "failed")
            self.assertGreater(
                data["planting_plan_checks"]["coverage_outside_plant_zone_area"],
                0,
            )

    def test_verifier_rejects_overlapping_area_plantings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints, zones = self.write_minimal_verifier_inputs(
                root, box(1, 1, 8, 8)
            )
            plan = root / "plan.geojsonl"
            report = root / "report.json"
            common = {
                "plant_type": "shrub",
                "status": "accepted",
                "checks": [
                    {
                        "code": "ALLOWED_ZONE",
                        "status": "passed",
                        "norm_reference": "СП 42.13330.2026, таблица 6.3",
                        "explanation": "Test explanation",
                    }
                ],
            }
            write_jsonl(
                plan,
                [
                    feature(
                        "proposed_planting_area",
                        box(2, 2, 5, 5),
                        planting_id="shrub-area-1",
                        **common,
                    ),
                    feature(
                        "proposed_planting_area",
                        box(4, 4, 7, 7),
                        planting_id="shrub-area-2",
                        **common,
                    ),
                ],
            )
            with patch.object(
                sys,
                "argv",
                [
                    "verify_outputs.py",
                    str(constraints),
                    str(zones),
                    "--planting-plan",
                    str(plan),
                    "--output",
                    str(report),
                ],
            ):
                with self.assertRaises(SystemExit):
                    verify_outputs.main()
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertGreater(
                data["planting_plan_checks"]["same_type_area_overlap"], 0
            )

    def test_verifier_checks_dxf_content_and_planting_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            constraints, zones = self.write_minimal_verifier_inputs(
                root, box(1, 1, 8, 8)
            )
            source_dxf = root / "source.dxf"
            result_dxf = root / "result.dxf"
            source = ezdxf.new("R2018")
            source.layers.add("BASE")
            source.modelspace().add_line(
                (0, 0), (10, 0), dxfattribs={"layer": "BASE"}
            )
            source.saveas(source_dxf)

            result = ezdxf.readfile(source_dxf)
            result.layers.add("GREEN_AI_PLANT_SHRUB")
            result.appids.add("GREEN_AI")
            planting = result.modelspace().add_circle(
                (3, 3), 0.25, dxfattribs={"layer": "GREEN_AI_PLANT_SHRUB"}
            )
            planting.set_xdata("GREEN_AI", [(1000, "id=shrub-point-1")])
            area = result.modelspace().add_hatch(
                dxfattribs={"layer": "GREEN_AI_PLANT_SHRUB"}
            )
            area.set_solid_fill(color=3)
            area.paths.add_polyline_path(
                [(4, 4), (6, 4), (6, 6), (4, 6)], is_closed=True
            )
            area.set_xdata("GREEN_AI", [(1000, "id=shrub-area-1")])
            result.saveas(result_dxf)

            plan = root / "plan.jsonl"
            write_jsonl(
                plan,
                [
                    feature(
                        "proposed_planting",
                        Point(3, 3),
                        planting_id="shrub-point-1",
                        plant_type="shrub",
                        status="accepted",
                        footprint_radius_m=0.1,
                        dxf_units_per_meter=1.0,
                        spacing_m=0.0,
                        checks=[
                            {
                                "code": "ALLOWED_ZONE",
                                "status": "passed",
                                "norm_reference": "СП 42.13330.2026, таблица 6.3",
                                "explanation": "Test explanation",
                            }
                        ],
                    ),
                    feature(
                        "proposed_planting_area",
                        box(4, 4, 6, 6),
                        planting_id="shrub-area-1",
                        plant_type="shrub",
                        status="accepted",
                        checks=[
                            {
                                "code": "SHRUB_AREA_COVERAGE",
                                "status": "passed",
                                "norm_reference": "СП 42.13330.2026, таблица 6.3",
                                "explanation": "Test area explanation",
                            }
                        ],
                    ),
                ],
            )
            report = root / "report.json"
            argv = [
                "verify_outputs.py",
                str(constraints),
                str(zones),
                "--planting-plan",
                str(plan),
                "--input-dxf",
                str(source_dxf),
                "--output-dxf",
                str(result_dxf),
                "--output",
                str(report),
            ]
            with patch.object(sys, "argv", argv):
                verify_outputs.main()
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["dxf_checks"]["changed_source_content_count"], 0)
            self.assertEqual(data["dxf_checks"]["planting_result_id_mismatches"], [])

            missing_area_metadata = ezdxf.readfile(result_dxf)
            area = missing_area_metadata.modelspace().query(
                'HATCH[layer=="GREEN_AI_PLANT_SHRUB"]'
            )[0]
            area.discard_xdata("GREEN_AI")
            missing_area_metadata.saveas(result_dxf)
            with patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    verify_outputs.main()
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                data["dxf_checks"]["planting_result_id_mismatches"][0][
                    "area_entities_without_id"
                ],
                1,
            )

            changed = ezdxf.readfile(result_dxf)
            area = changed.modelspace().query(
                'HATCH[layer=="GREEN_AI_PLANT_SHRUB"]'
            )[0]
            area.set_xdata("GREEN_AI", [(1000, "id=shrub-area-1")])
            changed.modelspace().query('LINE[layer=="BASE"]')[0].dxf.end = (9, 0, 0)
            changed.saveas(result_dxf)
            with patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    verify_outputs.main()
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertGreater(data["dxf_checks"]["changed_source_content_count"], 0)


if __name__ == "__main__":
    unittest.main()
