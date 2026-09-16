from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf
from shapely.geometry import box

from tests import ROOT  # noqa: F401 - initializes script-module import paths
import inspect_dxf
from scripts import seed, verify_outputs
from src import loader, utils
from tests.helpers import feature, write_jsonl


class ScriptTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
