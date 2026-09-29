"""Tree/shrub layers must fill the bed without losing trunk or site clearances."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shapely.geometry import Point, LineString, box, shape

from src.domain.models import PlantingProfile
from src.planting import service
from src.planting.placement_generator import (load_profiles, pack_candidates, required_spacing,
    safe_scope, configured_existing_tree_clearance_m)
from scripts import verify_outputs
from tests.test_planting_service import feature, write_jsonl


class MixedPlantingTests(unittest.TestCase):
    def profiles(self):
        tree = PlantingProfile("tree", "Tree", 6, 3, 2, 2, 100, "design", (),
                               understory_trunk_clearance_m=1)
        shrub = PlantingProfile("shrub", "Shrub", 1.5, .75, .5, .5, 100, "design", (),
                                allow_under_tree_canopy=True)
        return tree, shrub

    def test_overlap_is_mutual_symmetric_and_preserves_trunk_and_same_type_spacing(self):
        tree, shrub = self.profiles()
        self.assertEqual(required_spacing(tree, shrub), 2)
        self.assertEqual(required_spacing(shrub, tree), 2)
        self.assertEqual(required_spacing(tree, tree), 6)
        self.assertEqual(required_spacing(shrub, shrub), 1.5)
        self.assertEqual(required_spacing(replace(tree, understory_trunk_clearance_m=None), shrub), 3.75)
        self.assertEqual(required_spacing(tree, replace(shrub, allow_under_tree_canopy=False)), 3.75)
        self.assertEqual(required_spacing(tree, replace(shrub, footprint_radius_m=2.5)), 3.5)
        self.assertEqual(required_spacing(tree, replace(shrub, plant_type="herbaceous")), 3.75)
        self.assertEqual(pack_candidates([(0.5, 0), (3, 0), (3.5, 0)], shrub,
                                        {"tree": tree, "shrub": shrub}, [("tree", 0, 0)], 100),
                         [(3, 0)])

    def test_species_change_does_not_inherit_understory_permission(self):
        tree, shrub = self.profiles()
        for profile in (tree, shrub):
            selection = service.PlantingSelection("changed", profile.plant_type, "Other species",
                "fill_area", None, (), None, None, (), None)
            resolved = service.resolve_profile(selection, {profile.plant_type: profile},
                {"plantingProfiles": []}, {"plant_catalog": {profile.plant_type: [
                    {"id": 1, "name": "Other species"}]}})
            self.assertIsNone(resolved.understory_trunk_clearance_m)
            self.assertFalse(resolved.allow_under_tree_canopy)

    def test_invalid_trunk_clearance_fails_at_configuration_load(self):
        config = json.loads(Path("config/planting.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (0, -1, float("nan"), float("inf")):
                config["plantingProfiles"][0]["understoryTrunkClearanceM"] = value
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_profiles(path)

    def test_plan_fills_between_trees_but_preserves_obstacles_and_verifies_in_m_and_mm(self):
        for units in (1, 1000):
            with self.subTest(units=units), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def point(x, y):
                    return Point(x * units, y * units)
                bed = box(0, 0, 20 * units, 10 * units)
                well = point(3, 5).buffer(1 * units)
                allowed = bed.difference(well)
                zones, report, normalized, constraints, config, request, plan = (
                    root / name for name in ("zones.jsonl", "zone_report.json", "normalized.jsonl",
                                            "constraints.jsonl", "config.json", "request.json", "plan.jsonl"))
                write_jsonl(zones, [feature("plant_allow_zone", allowed, plant_type=kind)
                                   for kind in ("tree", "shrub")])
                normalized.write_text("", encoding="utf-8")
                far = box(50 * units, 50 * units, 51 * units, 51 * units)
                write_jsonl(constraints, [
                    feature("base_allowed_area", allowed), feature("confirmed_plantable_surface", allowed),
                    feature("utility_well_footprints", well), feature("road_area", far),
                    feature("hard_surface_area", far), feature("sidewalk_area", far)])
                report.write_text(json.dumps({"dxf_units_per_meter": units, "plant_types": {
                    kind: {"rules": []} for kind in ("tree", "shrub")}}), encoding="utf-8")
                config.write_text(json.dumps({"diagnosticRejectedMaxCount": 0, "plantingProfiles": [
                    {"plantType": "tree", "species": "Tree", "geometryKind": "point", "spacingM": 6,
                     "footprintRadiusM": 3, "symbolRadiusM": 2, "avoidOtherPlantingsM": 2,
                     "understoryTrunkClearanceM": 1},
                    {"plantType": "shrub", "species": "Shrub", "geometryKind": "point", "spacingM": 1.5,
                     "footprintRadiusM": .75, "symbolRadiusM": .5, "avoidOtherPlantingsM": .5,
                     "allowUnderTreeCanopy": True}]}), encoding="utf-8")
                request.write_text(json.dumps({"selections": [
                    {"id": "trees", "plant_type": "tree", "species": "Tree", "mode": "points",
                     "points": [[7 * units, 5 * units], [13 * units, 5 * units]]},
                    {"id": "shrubs", "plant_type": "shrub", "species": "Shrub", "mode": "fill_area"},
                    {"id": "bad_shrub", "plant_type": "shrub", "species": "Shrub", "mode": "points",
                     "points": [[3 * units, 5 * units], [7 * units, 5 * units]]}]}), encoding="utf-8")
                service.plan(zones, report, normalized, constraints, root / "missing.jsonl", config,
                             plan, root / "report.json", root / "decisions.jsonl", request)
                features = [json.loads(line) for line in plan.read_text(encoding="utf-8").splitlines()]
                shrubs = [shape(item["geometry"]) for item in features if item["properties"]["plant_type"] == "shrub"]
                self.assertTrue(any(point(10, 5).distance(p) < 1.25 * units for p in shrubs))
                for p in shrubs:
                    self.assertGreaterEqual(p.distance(well) + 1e-7, .75 * units)
                    self.assertTrue(all(p.distance(point(x, 5)) + 1e-7 >= 2 * units for x in (7, 13)))
                decisions = [json.loads(line) for line in (root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
                invalid = [item for item in decisions if item["properties"]["request_id"] == "bad_shrub"]
                self.assertEqual([item["properties"]["status"] for item in invalid], ["rejected", "rejected"])
                mixed = [check for item in features for check in item["properties"]["checks"]
                         if check.get("tree_shrub_spacing")]
                self.assertTrue(mixed)
                self.assertEqual({entry["required_distance_m"] for check in mixed
                                  for entry in check["tree_shrub_spacing"]}, {2})
                # This synthetic site has no NPA rules; supply a fixture reference
                # so the independent verifier can also exercise spatial invariants.
                for item in features:
                    item["properties"]["checks"][0]["norm_reference"] = "СП fixture, table 1"
                write_jsonl(plan, features)
                with patch.object(sys, "argv", ["verify_outputs.py", str(constraints), str(zones),
                        "--normalized-objects", str(normalized),
                        "--planting-plan", str(plan), "--output", str(root / "verified.json")]), contextlib.redirect_stdout(io.StringIO()):
                    verify_outputs.main()
                verified = json.loads((root / "verified.json").read_text(encoding="utf-8"))
                self.assertEqual(verified["status"], "passed", verified["failures"])
                # Corrupt a coordinate without updating its saved checks:
                # verification must recompute spacing, not trust 'passed'.
                next(item for item in features if item["properties"]["plant_type"] == "shrub")["geometry"] = {
                    "type": "Point", "coordinates": [8.9 * units, 5 * units]}
                write_jsonl(plan, features)
                with patch.object(sys, "argv", ["verify_outputs.py", str(constraints), str(zones),
                        "--normalized-objects", str(normalized),
                        "--planting-plan", str(plan), "--output", str(root / "verified.json")]), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        verify_outputs.main()
                verified = json.loads((root / "verified.json").read_text(encoding="utf-8"))
                self.assertGreater(verified["planting_plan_checks"]["cross_type_spacing_failure_count"], 0)

    def test_existing_tree_profile_overrides_legacy_clearance_and_requires_physical_ground(self):
        _tree, shrub = self.profiles()
        self.assertEqual(configured_existing_tree_clearance_m({}, shrub), 3.25)
        shrub = replace(shrub, existing_tree_clearance_m=2, footprint_boundary="physical_area")
        self.assertEqual(configured_existing_tree_clearance_m({"existingTreeClearanceM": 5}, shrub), 2)
        with self.assertRaisesRegex(ValueError, "physical_area is required"):
            safe_scope(box(0, 0, 10, 10), shrub, None, None, 2, 1)
        # The selected polygon remains a physical boundary, even with centre-only rules.
        scope = safe_scope(box(0, 0, 10, 10), shrub, None, None, 2, 1, box(0, 0, 5, 10))
        self.assertFalse(scope.covers(Point(4.5, 5)))
        self.assertTrue(scope.covers(Point(4, 5)))

    def test_tree_crown_does_not_repeat_centre_setback(self):
        config = json.loads(Path("config/planting.json").read_text(encoding="utf-8"))
        tree_config = config["plantingProfiles"][0]
        self.assertEqual(tree_config["footprintBoundary"], "physical_area")
        # Older/custom profiles without the option must use the same safe default.
        tree_config.pop("footprintBoundary")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            tree = load_profiles(path)["tree"]
        self.assertEqual(tree.footprint_boundary, "physical_area")
        physical_ground = box(0, 0, 20, 12)
        centre_zone = box(2, 0, 20, 12)
        scope = safe_scope(centre_zone, tree, None, None, 0, 1, physical_ground)
        self.assertTrue(scope.covers(Point(3.5, 6)))
        self.assertFalse(scope.covers(Point(2.5, 6)))
        self.assertFalse(scope.covers(Point(18, 6)))

    def test_centre_setbacks_and_existing_tree_spacing_preserve_physical_obstacles(self):
        for units in (1, 1000):
            with self.subTest(units=units), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def pt(x, y):
                    return Point(x * units, y * units)
                def rect(a, b, c, d):
                    return box(a * units, b * units, c * units, d * units)
                bed = rect(0, 0, 30, 12)
                obstacles = {"road_area": rect(0, 0, 30, 1),
                    "utility_well_footprints": pt(5, 6).buffer(units),
                    "buildings_in_work_area": rect(22, 2, 25, 5),
                    "heat_chamber_footprints": rect(23, 8, 25, 11)}
                ground = bed
                for geometry in obstacles.values():
                    ground = ground.difference(geometry)
                cable = LineString([(15 * units, 0), (15 * units, 12 * units)])
                zone = ground.difference(cable.buffer(.75 * units))
                paths = {name: root / (name + ".jsonl") for name in
                         ("zones", "normalized", "constraints", "plan", "decisions")}
                write_jsonl(paths["zones"], [feature("plant_allow_zone", zone, plant_type="shrub")])
                write_jsonl(paths["normalized"], [feature("existing_tree", pt(10, 6)),
                    feature("existing_tree_belt", rect(2, 8, 4, 10)), feature("power_cable", cable)])
                write_jsonl(paths["constraints"], [feature(k, g) for k, g in {
                    **obstacles, "base_allowed_area": bed, "confirmed_plantable_surface": bed,
                    "hard_surface_area": obstacles["road_area"]}.items()])
                config = root / "config.json"
                config.write_text(json.dumps({"diagnosticRejectedMaxCount": 0, "plantingProfiles": [{
                    "plantType": "shrub", "species": "Shrub", "geometryKind": "point", "spacingM": 1.5,
                    "footprintRadiusM": .75, "symbolRadiusM": .5, "avoidOtherPlantingsM": .5,
                    "allowUnderTreeCanopy": True, "existingTreeClearanceM": 2,
                    "footprintBoundary": "physical_area"}]}), encoding="utf-8")
                zone_report = root / "zone_report.json"
                zone_report.write_text(json.dumps({"dxf_units_per_meter": units, "plant_types": {"shrub": {"rules": [{
                    "rule_code": "POWER_TEST", "target_object_type": "power_cable", "status": "applied",
                    "min_distance_m": .75, "norm_reference": "СП fixture, table 1"}]}}}), encoding="utf-8")
                coordinates = [(14, 8), (12.2, 6), (27, 8), (10.2, 6), (14.5, 3),
                               (6.3, 6), (18, 1.5), (21.5, 3), (22.5, 9), (29.6, 8), (4.5, 9)]
                request = root / "request.json"
                request.write_text(json.dumps({"selections": [{"id": "shrubs", "plant_type": "shrub",
                    "species": "Shrub", "mode": "points", "points": [[x * units, y * units] for x, y in coordinates]}]}), encoding="utf-8")
                service.plan(paths["zones"], zone_report, paths["normalized"], paths["constraints"],
                             root / "missing", config, paths["plan"], root / "report.json", paths["decisions"], request)
                decisions = [json.loads(line) for line in paths["decisions"].read_text(encoding="utf-8").splitlines()]
                self.assertEqual([d["properties"]["status"] for d in decisions], ["accepted"] * 3 + ["rejected"] * 8)
                records = [json.loads(line) for line in paths["plan"].read_text(encoding="utf-8").splitlines()]
                self.assertEqual({f["properties"]["existing_tree_clearance_m"] for f in records}, {2})
                self.assertEqual({f["properties"]["footprint_boundary"] for f in records}, {"physical_area"})
                for f in records:
                    checks = {c["code"]: c for c in f["properties"]["checks"]}
                    self.assertEqual(checks["EXISTING_TREE_CLEARANCE"]["status"], "passed")
                    self.assertEqual(checks["PLANT_FOOTPRINT_INSIDE_SITE"]["status"], "passed")
                    self.assertEqual(checks["POWER_TEST"]["required_distance_m"], .75)
                def verify():
                    with patch.object(sys, "argv", ["verify_outputs.py", str(paths["constraints"]), str(paths["zones"]),
                        "--planting-plan", str(paths["plan"]), "--normalized-objects", str(paths["normalized"]),
                        "--output", str(root / "verified.json")]), contextlib.redirect_stdout(io.StringIO()):
                        verify_outputs.main()
                verify()
                good = json.loads((root / "verified.json").read_text())
                self.assertGreater(good["planting_plan_checks"]["canopy_overlap_with_rule_buffers_area"], 0)
                self.assertEqual(good["planting_plan_checks"]["footprint_outside_physical_area"], 0)
                for position, counter in [((14.5, 8), "centre_outside_allow_zone_count"),
                        ((10.5, 6), "existing_tree_clearance_failure_count"),
                        ((5, 6), "footprint_outside_physical_area"),
                        ((4.5, 9), "existing_tree_belt_clearance_failure_count")]:
                    with self.subTest(corruption=counter):
                        tampered = json.loads(json.dumps(records))
                        tampered[0]["geometry"]["coordinates"] = [v * units for v in position]
                        write_jsonl(paths["plan"], tampered)
                        with self.assertRaises(SystemExit):
                            verify()
                        failed = json.loads((root / "verified.json").read_text())
                        self.assertGreater(failed["planting_plan_checks"][counter], 0)


if __name__ == "__main__":
    unittest.main()
