from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

import ezdxf
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import unary_union

from tests import ROOT  # noqa: F401
from src.cad_io import dxf_exporter
from src.planting import service as planting_service
from src.planting.design import flowerbed_patches
from src.planting.design import choose_shrub_composition, choose_tree_composition, prune_shrub_fragments, score_tree_composition, tree_grove_layout
from src.domain.models import PlantingProfile


def write_jsonl(path, features):
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in features), encoding="utf-8")


def feature(kind, geometry, **props):
    return {"type": "Feature", "properties": {"object_type": kind, **props}, "geometry": mapping(geometry)}


class DesignModeTests(unittest.TestCase):
    def prepare(self, root, area=None, units=1.0):
        if area is None:
            area = box(0, 0, 60, 24).difference(box(26, 8, 32, 16))
        area = affinity.scale(area, units, units, origin=(0, 0))
        write_jsonl(root / "zones.jsonl", [
            feature("plant_allow_zone", area, plant_type=kind) for kind in ("tree", "shrub")
        ])
        write_jsonl(root / "constraints.jsonl", [feature("base_allowed_area", area)])
        write_jsonl(root / "normalized.jsonl", [])
        catalog = {kind: [{"name": name, "id": i}] for i, (kind, name) in enumerate([
            ("tree", "Tree"), ("shrub", "Shrub"), ("herbaceous", "Lawn")
        ])}
        catalog["herbaceous"] += [{"id": 10 + i, "name": name} for i, name in enumerate(("Flower A", "Flower B", "Flower C"))]
        (root / "zone_report.json").write_text(json.dumps({
            "dxf_units_per_meter": units,
            "plant_types": {"tree": {"rules": []}, "shrub": {"rules": []}},
            "plant_catalog": catalog,
        }), encoding="utf-8")
        config = {
            "diagnosticRejectedMaxCount": 0, "existingTreeClearanceM": 2,
            "plantingDesignDefaults": {"mixed_flowerbed": {"composition": self.mixture()}},
            "plantingProfiles": [
                {"plantType": "tree", "species": "Tree", "geometryKind": "point", "spacingM": 3,
                 "footprintRadiusM": 1, "symbolRadiusM": 1, "maxCount": 100},
                {"plantType": "shrub", "species": "Shrub", "geometryKind": "area", "spacingM": 1},
                {"plantType": "herbaceous", "species": "Lawn", "geometryKind": "area"},
            ],
        }
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return area

    @staticmethod
    def mixture():
        return [{"species": name, "share": share} for name, share in (
            ("Flower A", .4), ("Flower B", .3), ("Flower C", .3),
        )]

    def run_plan(self, root, selections=None, preset="dense_mixed"):
        request = None
        if selections is not None:
            request = root / "request.json"
            request.write_text(json.dumps({"selections": selections}), encoding="utf-8")
        report = planting_service.plan(
            root / "zones.jsonl", root / "zone_report.json", root / "normalized.jsonl",
            root / "constraints.jsonl", root / "utilities.jsonl", root / "config.json",
            root / "plan.jsonl", root / "report.json", root / "decisions.jsonl", request,
            root / "explanations.md", preset=preset,
        )
        return [json.loads(line) for line in (root / "plan.jsonl").read_text(encoding="utf-8").splitlines()], report

    def test_composition_mode_groups_trees_in_broad_plot_and_keeps_clearings(self):
        profile = PlantingProfile("tree", "Tree", 6, 3, 2, 2, 100, "catalog", ())
        trace = {}
        scope = box(0, 0, 50, 40).buffer(-3).difference(box(30, 15, 36, 25).buffer(3))
        points = tree_grove_layout(scope, profile, {"tree": profile}, [], 100, trace=trace)
        self.assertEqual(trace["method"], "tree_grove")
        self.assertGreaterEqual(len(points), 5)
        self.assertEqual(len(points) % trace["winner"]["group_size"], 0)
        self.assertTrue(all(scope.covers(Point(x, y)) for x, y in points))
        self.assertTrue(all(math.dist(left, right) >= 6 - 1e-7
                            for left, right in combinations(points, 2)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root, box(0, 0, 50, 40))
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["treeLayoutMode"] = "composition"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            features, report = self.run_plan(root, [{
                "id": "tree_groups", "plant_type": "tree", "species": "Tree",
                "mode": "fill_area", "design_style": "auto",
            }])
            self.assertGreaterEqual(len(features), 3)
            self.assertTrue(all(f["properties"]["layout_style"] in
                                {"tree_grove", "free_group", "focal_tree"}
                                for f in features))
            traces = [json.loads(line) for line in
                      (root / "planting_layout_trace.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(t["method"] == "composition" and t["winner"]
                                for t in traces))
            self.assertEqual(report["rejected_candidate_count"], 0)

    def test_tree_composition_compares_whole_schemes_by_site_form(self):
        profile = PlantingProfile("tree", "Tree", 6, 3, 2, 2, 100, "catalog", ())
        broad = box(0, 0, 50, 40).buffer(-3)
        trace = {}
        points, style = choose_tree_composition(
            broad, box(0, 0, 50, 40), profile, {"tree": profile},
            [], 100, trace=trace,
        )
        self.assertIn(style, {"tree_grove", "free_group"})
        self.assertGreaterEqual(len(points), 3)
        self.assertGreaterEqual(len(trace["variants"]), 2)
        self.assertEqual(trace["winner"]["score"],
                         max(item["score"] for item in trace["variants"]))
        self.assertTrue(all(len(item["coordinates"]) == item["count"]
                            for item in trace["variants"]))
        self.assertTrue(all(broad.covers(Point(x, y)) for x, y in points))

        strip = box(0, 0, 65, 9).buffer(-2)
        trace = {}
        points, style = choose_tree_composition(
            strip, box(0, 0, 65, 9), profile, {"tree": profile},
            [], 100, trace=trace,
        )
        self.assertEqual(style, "linear")
        self.assertGreaterEqual(len(points), 2)
        self.assertTrue(all(strip.covers(Point(x, y)) for x, y in points))

    def test_cp_sat_mode_selects_and_audits_tree_schemes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            area = self.prepare(root, box(0, 0, 65, 12))
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["treeLayoutMode"] = "cp_sat"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            features, report = self.run_plan(root, [{
                "id": "optimized_trees", "plant_type": "tree", "species": "Tree",
                "mode": "fill_area", "design_style": "auto",
            }])
            points = [shape(item["geometry"]) for item in features]
            self.assertGreater(len(points), 2)
            self.assertTrue(all(area.covers(point) for point in points))
            self.assertTrue(all(left.distance(right) >= 3 - 1e-8
                                for left, right in combinations(points, 2)))
            run = report["layout_optimizer_runs"]["optimized_trees"]
            self.assertEqual(run["solver"], "cp_sat")
            self.assertEqual(run["status"], "OPTIMAL")
            self.assertGreaterEqual(run["option_count"], 2)
            self.assertEqual(run["selected_count"], len(points))

    def test_tree_score_uses_canopy_overlap_only_with_a_shade_target(self):
        profile = PlantingProfile("tree", "Tree", 6, 3, 2, 2, 10, "catalog", ())
        bed = box(0, 0, 30, 20)
        near = score_tree_composition([(5, 5)], bed, profile, "focal_tree",
                                      shade_target=box(5, 6, 10, 10))
        far = score_tree_composition([(25, 5)], bed, profile, "focal_tree",
                                     shade_target=box(5, 6, 10, 10))
        self.assertGreater(near["pedestrian_canopy_overlap"], 0)
        self.assertGreater(near["score"], far["score"])
        self.assertEqual(far["shade_proxy_score"], 0)

    def test_shrub_composition_compares_connected_bed_layouts(self):
        profile = PlantingProfile("shrub", "Shrub", 1.5, .75, .5, .5, 500, "catalog", ())
        for bed, expected in ((box(0, 0, 65, 9), "shrub_bed_rows"),
                              (box(0, 0, 20, 20), "shrub_bed_grid")):
            scope = bed.buffer(-.75)
            trace = {}
            points, style = choose_shrub_composition(
                scope, bed, profile, {"shrub": profile}, [], 500, trace=trace,
            )
            self.assertEqual(style, expected)
            self.assertGreater(len(points), 10)
            self.assertEqual(trace["method"], "shrub_composition")
            self.assertEqual(trace["winner"]["isolated"], 0)
            self.assertTrue(all(scope.covers(Point(x, y)) for x, y in points))

    def test_shrub_fragments_keep_main_mass_but_omit_stray_tail(self):
        main = [(float(x), float(y)) for x in range(4) for y in range(3)]
        stray = [(20.0, 0.0), (21.0, 0.0), (22.0, 0.0)]
        kept, omitted = prune_shrub_fragments(main + stray, 1.0)
        self.assertEqual(kept, main)
        self.assertEqual(omitted, [[x, y] for x, y in stray])
        self.assertEqual(prune_shrub_fragments(stray, 1.0), (stray, []))

    def test_alley_keeps_two_rows_and_phase_across_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            area = self.prepare(root)
            selections = [{"id": "alley", "plant_type": "tree", "species": "Tree", "design_style": "alley",
                           "guide": mapping(LineString([(0, 12), (60, 12)])), "row_count": 2}]
            features, report = self.run_plan(root, selections)
            coords = [f["geometry"]["coordinates"] for f in features]
            self.assertGreater(len(coords), 20)
            self.assertEqual({round(y, 6) for x, y in coords}, {10.5, 13.5})
            self.assertLess(max(x % 3 for x, y in coords) - min(x % 3 for x, y in coords), 1e-8)
            self.assertTrue(all(area.buffer(1e-8).covers(Point(x, y).buffer(1)) for x, y in coords))
            self.assertEqual(report["summary"]["alley"]["design_style"], "alley")

    def test_same_type_multiple_zones_keep_both_spacing_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root)
            features, report = self.run_plan(root, [
                {"id": "tree", "plant_type": "tree", "species": "Tree", "mode": "points", "spacing_m": 8, "points": [[5, 5]]},
                {"id": "other", "plant_type": "tree", "species": "Tree", "mode": "points", "spacing_m": 3, "points": [[10, 5], [15, 5]]},
            ])
            self.assertEqual([f["geometry"]["coordinates"] for f in features], [[5.0, 5.0], [15.0, 5.0]])
            self.assertEqual(report["summary"]["other"]["rejected_count"], 1)

    def test_free_groups_are_reproducible_separated_and_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            area = self.prepare(root)
            selections = [{"id": "groups", "plant_type": "tree", "species": "Tree", "design_style": "free_group", "seed": 19}]
            first, _ = self.run_plan(root, selections)
            again, _ = self.run_plan(root, selections)
            self.assertEqual(first, again)
            points = [shape(f["geometry"]) for f in first]
            self.assertGreater(len(points), 6)
            self.assertTrue(all(area.buffer(1e-8).covers(p.buffer(1)) for p in points))
            self.assertTrue(all(a.distance(b) >= 3 - 1e-8 for a, b in combinations(points, 2)))
            selections[0]["seed"] = 20
            different, _ = self.run_plan(root, selections)
            self.assertNotEqual(first, different)

    def test_hedge_width_holes_lawn_and_estimates_in_meters_and_millimeters(self):
        results = []
        for units in (1, 1000):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                area = self.prepare(root, units=units)
                guide = LineString([(0, 12 * units), (60 * units, 12 * units)])
                features, report = self.run_plan(root, [
                    {"id": "hedge", "plant_type": "shrub", "species": "Shrub", "design_style": "hedge", "band_width_m": 2, "spacing_m": .8, "guide": mapping(guide)},
                    {"id": "lawn", "plant_type": "herbaceous", "species": "Lawn"},
                ])
                shrubs = unary_union([shape(f["geometry"]) for f in features if f["properties"]["plant_type"] == "shrub"])
                lawn = unary_union([shape(f["geometry"]) for f in features if f["properties"]["plant_type"] == "herbaceous"])
                expected = guide.buffer(units, cap_style=2).intersection(area)
                self.assertLess(shrubs.symmetric_difference(expected).area, 1e-7 * units ** 2)
                self.assertEqual(shrubs.intersection(lawn).area, 0)
                self.assertLess(shrubs.union(lawn).symmetric_difference(area).area, 1e-7 * units ** 2)
                self.assertTrue(all(f["properties"].get("nominal_spacing_m", .8) == .8 for f in features))
                results.append(report["summary"]["hedge"]["estimated_plant_count"])
        self.assertEqual(*results)

    def test_flowerbed_area_shares_holes_and_lawn_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            area = self.prepare(root)
            bed = box(5, 4, 48, 20).intersection(area)
            features, report = self.run_plan(root, [
                {"id": "lawn", "plant_type": "herbaceous", "species": "Lawn"},
                {"id": "flowers", "plant_type": "herbaceous", "species": "Flower A", "design_style": "mixed_flowerbed",
                 "area": mapping(bed), "composition": self.mixture()},
            ])
            beds = [f for f in features if f["properties"]["request_id"] == "flowers"]
            for item in self.mixture():
                actual = sum(shape(f["geometry"]).area for f in beds if f["properties"]["species"] == item["species"])
                self.assertAlmostEqual(actual / bed.area, item["share"], places=7)
            geometries = [shape(f["geometry"]) for f in features]
            self.assertLess(sum(g.area for g in geometries) - unary_union(geometries).area, 1e-7)
            self.assertLess(unary_union(geometries).symmetric_difference(area).area, 1e-7)
            self.assertEqual(set(report["summary"]["flowers"]["species_area_m2"]), {p["species"] for p in self.mixture()})

            overlay = root / "overlay.dxf"
            dxf_exporter.export_zones(root / "unused.dxf", root / "zones.jsonl", overlay, .7,
                                      planting_plan_path=root / "plan.jsonl", overlay_only=True, insunits=6)
            doc = ezdxf.readfile(overlay)
            self.assertFalse(doc.audit().has_errors)
            flower_hatches = [e for e in doc.modelspace().query("HATCH") if any(t.value == "design=mixed_flowerbed" for t in e.get_xdata("GREEN_AI"))]
            self.assertEqual(len(flower_hatches), len(beds))
            self.assertEqual(len({e.dxf.color for e in flower_hatches}), 3)

    def test_all_presets_run_and_unknown_mixture_species_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root)
            for preset in ("alley", "hedge", "shrub_mass", "free_group", "mixed_flowerbed"):
                with self.subTest(preset=preset):
                    features, report = self.run_plan(root, preset=preset)
                    self.assertGreater(len(features), 0)
                    self.assertTrue(any(f["properties"].get("design_style") == preset for f in features))
            mixture = self.mixture()
            mixture[1]["species"] = "Unknown flower"
            with self.assertRaisesRegex(ValueError, "not selectable"):
                self.run_plan(root, [{"plant_type": "herbaceous", "species": "Flower A", "design_style": "mixed_flowerbed", "composition": mixture}])

    def test_design_contracts_reject_invalid_or_incompatible_inputs(self):
        invalid = [
            ("tree", "fill_area", {"design_style": "hedge"}),
            ("tree", "points", {"design_style": "alley"}),
            ("tree", "fill_area", {"design_style": "alley", "row_count": 1.5}),
            ("tree", "fill_area", {"design_style": "alley", "guide": {"type": "LineString", "coordinates": [[0, 0], [math.inf, 1]]}}),
            ("shrub", "cover_area", {"design_style": "hedge", "band_width_m": 0}),
            ("herbaceous", "cover_area", {"design_style": "mixed_flowerbed", "composition": self.mixture()[:2]}),
            ("herbaceous", "cover_area", {"design_style": "mixed_flowerbed", "composition": [{"species": "A", "share": .5}, {"species": "A", "share": .5}]}),
        ]
        for plant_type, mode, options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                planting_service._design_options(options, plant_type, mode, "test")

    def test_rotated_concave_flowerbed_preserves_area_and_share(self):
        area = affinity.rotate(Polygon([(0, 0), (30, 0), (30, 8), (8, 8), (8, 25), (0, 25)]).difference(box(2, 2, 5, 5)), 31)
        patches = list(flowerbed_patches(area, tuple(self.mixture())))
        self.assertLess(unary_union([p for p, _ in patches]).symmetric_difference(area).area, 1e-6)
        for item in self.mixture():
            self.assertAlmostEqual(sum(p.area for p, m in patches if m["species"] == item["species"]) / area.area, item["share"], places=7)

    def test_real_survey_holes_do_not_break_flowerbed_partition(self):
        fixture = json.loads((ROOT / "tests/fixtures/flowerbed_survey_ring.json").read_text(encoding="utf-8"))
        area = shape(fixture["geometry"])
        patches = list(flowerbed_patches(area, tuple(self.mixture())))
        self.assertTrue(all(p.is_valid for p, _ in patches))
        union = unary_union([p for p, _ in patches])
        self.assertLess(union.symmetric_difference(area).area, 1e-6)
        self.assertLess(sum(p.area for p, _ in patches) - union.area, 1e-6)
        for item in self.mixture():
            self.assertAlmostEqual(sum(p.area for p, m in patches if m["species"] == item["species"]) / area.area, item["share"], places=7)

    def test_multiple_generated_selections_respect_different_spacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root, box(0, 0, 60, 30))
            points, _ = self.run_plan(root, [
                {"id": "tree", "plant_type": "tree", "species": "Tree", "design_style": "alley", "spacing_m": 8,
                 "guide": mapping(LineString([(0, 10), (60, 10)]))},
                {"id": "other", "plant_type": "tree", "species": "Tree", "design_style": "alley", "spacing_m": 3,
                 "guide": mapping(LineString([(0, 17), (60, 17)]))},
            ])
            self.assertEqual({f["properties"]["request_id"] for f in points}, {"tree", "other"})
            for a, b in combinations(points, 2):
                required = max(a["properties"]["spacing_m"], b["properties"]["spacing_m"])
                self.assertGreaterEqual(shape(a["geometry"]).distance(shape(b["geometry"])) + 1e-8, required)

    @unittest.skipUnless(shutil.which("powershell"), "PowerShell menu requires Windows")
    def test_menu_passes_each_design_preset_and_request_without_conflicts(self):
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drawing = root / "input.dxf"
            drawing.write_text("dry-run placeholder", encoding="utf-8")
            request = root / "request.json"
            request.write_text('{"selections":[]}', encoding="utf-8")
            script = quote(ROOT / "scripts/run_pipeline_interactive.ps1")
            commands = [
                "$styles = @('alley','hedge','shrub_mass','free_group','mixed_flowerbed')",
                f"for ($i=0; $i -lt $styles.Count; $i++) {{ $r = & {script} -DryRun -Answers @('1',{quote(drawing)},'3',[string]($i+8),'6') 6>&1 | Out-String; if ($r -notmatch ('PlantingPreset = ' + $styles[$i])) {{ throw ('Missing preset ' + $styles[$i]) }} }}",
                f"$r = & {script} -DryRun -Answers @('1',{quote(drawing)},'4','7',{quote(request)},'0','6') 6>&1 | Out-String",
                "if ($r -notmatch 'PlantingRequest = ' -or $r -match 'PlantingPreset = ') { throw 'Request/preset conflict' }",
            ]
            result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "; ".join(commands)], capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_independent_verifier_checks_larger_spacing_of_later_species(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            area = self.prepare(root, box(0, 0, 30, 20))
            write_jsonl(root / "constraints.jsonl", [
                feature("base_allowed_area", area), feature("confirmed_plantable_surface", area),
                feature("road_area", box(40, 40, 50, 50)), feature("hard_surface_area", box(40, 40, 50, 50)),
            ])
            points = [feature("proposed_planting", Point(x, 10), planting_id=f"T-{i}", plant_type="tree", species=f"Tree {i}",
                              status="accepted", spacing_m=spacing, footprint_radius_m=1,
                              checks=[{"code": "ALLOWED_ZONE", "status": "passed", "explanation": "Test", "norm_reference": "СП test fixture"}])
                      for i, (x, spacing) in enumerate(((5, 3), (10, 8)))]
            for ordered in (points, list(reversed(points))):
                write_jsonl(root / "bad_plan.jsonl", ordered)
                result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_outputs.py"), str(root / "constraints.jsonl"), str(root / "zones.jsonl"),
                                         "--planting-plan", str(root / "bad_plan.jsonl"), "--output", str(root / "verification.json")], capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                report = json.loads((root / "verification.json").read_text(encoding="utf-8"))
                self.assertEqual(report["planting_plan_checks"]["same_type_spacing_failure_count"], 1)


if __name__ == "__main__":
    unittest.main()
