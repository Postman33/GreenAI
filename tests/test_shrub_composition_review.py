from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Point, box, mapping, shape

from tests import ROOT  # noqa: F401
from src.planting import service
from src.planting.composition_review import load_shrub_survey, review_shrub_composition, review_tree_composition
from src.planting.design import tree_grove_layout
from src.domain.models import PlantingProfile
from tests.test_planting_design import DesignModeTests


def proposed_mass(species="Dogwood", style="shrub_mass"):
    return [{
        "type": "Feature", "id": "SA-0001",
        "properties": {
            "object_type": "proposed_planting_area", "planting_id": "SA-0001",
            "plant_type": "shrub", "design_style": style,
            "status": "accepted", "species": species,
        },
        "geometry": mapping(box(0, 0, 10, 10)),
    }]


def shrub(identifier, x, y, species="Lilac"):
    return {"id": identifier, "point": Point(x, y), "species": species}


class CompositionReviewTests(unittest.TestCase):
    def test_tree_removal_is_proposed_only_for_a_complete_blocked_group(self):
        profile = PlantingProfile("tree", "Tree", 6, 3, 2, 2, 5, "catalog", ())
        scope = box(0, 0, 50, 40).buffer(-3)
        trace = {}
        grove = tree_grove_layout(scope, profile, {"tree": profile}, [], profile.max_count, trace=trace)
        self.assertEqual(trace["method"], "tree_grove")
        group_size = trace["winner"]["group_size"]
        first_group = grove[:group_size]
        blocker = Point(sum(x for x, _ in first_group) / group_size,
                        sum(y for _, y in first_group) / group_size)
        clearance = 8.0
        retained = scope.difference(blocker.buffer(clearance))
        result = review_tree_composition(
            scope, retained, blocker, box(0, 0, 50, 40),
            profile, {"tree": profile}, [], clearance,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["recommendation"], "propose_removal_from_composition")
        self.assertGreaterEqual(result[0]["blocked_group_stations"], 2)
        self.assertGreaterEqual(result[0]["design_quality_gain"], 4)
        self.assertTrue(result[0]["review_required"])
        self.assertEqual(review_tree_composition(
            scope, retained, blocker, box(100, 100, 110, 110),
            profile, {"tree": profile}, [], clearance,
        ), [])

        three = PlantingProfile("tree", "Tree", 6, 3, 2, 2, 3, "catalog", ())
        self.assertEqual(review_tree_composition(
            scope, retained, blocker, box(0, 0, 50, 40),
            three, {"tree": three}, [], clearance,
        ), [])

    def test_only_confirmed_isolated_different_species_in_uniform_mass_is_advised(self):
        features = proposed_mass()
        individuals = [shrub("S1", 5, 5)]
        result = review_shrub_composition(features, individuals, box(20, 20, 30, 30), 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["recommendation"], "propose_removal_from_composition")
        self.assertTrue(result[0]["review_required"])
        for masses, plants in (
            (box(4, 4, 6, 6), individuals),
            (box(20, 20, 30, 30), [*individuals, shrub("S2", 6, 5)]),
        ):
            self.assertEqual(review_shrub_composition(features, plants, masses, 1), [])
        self.assertEqual(review_shrub_composition(features, [shrub("S1", 5, 5, "Dogwood")],
                                                       box(20, 20, 30, 30), 1), [])
        self.assertEqual(review_shrub_composition(proposed_mass(style="auto"), individuals,
                                                       box(20, 20, 30, 30), 1), [])

    def test_unknown_species_requests_identification_not_removal(self):
        result = review_shrub_composition(proposed_mass(), [shrub("S1", 5, 5, "")],
                                          box(20, 20, 30, 30), 1)
        self.assertEqual(result[0]["recommendation"], "identify_existing_shrub_before_design_decision")

    def test_survey_contract_and_planner_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            DesignModeTests().prepare(root)
            request = root / "request.json"
            request.write_text(json.dumps({"selections": [{
                "id": "mass", "plant_type": "shrub", "species": "Shrub",
                "mode": "cover_area", "design_style": "shrub_mass",
            }]}), encoding="utf-8")
            survey = root / "survey.geojson"
            survey.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": "existing-1",
                 "properties": {"object_type": "existing_shrub", "species": "Lilac"},
                 "geometry": mapping(Point(10, 10))},
            ]}), encoding="utf-8")
            individuals, masses = load_shrub_survey(survey)
            self.assertEqual(len(individuals), 1)
            self.assertTrue(masses.is_empty)
            report = service.plan(
                root / "zones.jsonl", root / "zone_report.json", root / "normalized.jsonl",
                root / "constraints.jsonl", root / "utilities.jsonl", root / "config.json",
                root / "plan.jsonl", root / "report.json", root / "decisions.jsonl",
                request, root / "explanations.md", existing_shrub_survey_path=survey,
            )
            self.assertEqual(report["composition_review_status"], "surveyed")
            self.assertEqual(len(report["composition_advisories"]), 1)
            self.assertIn("existing-1", (root / "explanations.md").read_text(encoding="utf-8"))
            self.assertEqual(len((root / "plan.jsonl").read_text(encoding="utf-8").splitlines()), 1)

            survey.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": "existing-1",
                 "properties": {"object_type": "existing_shrub", "species": "Lilac"},
                 "geometry": mapping(Point(10, 10))},
                {"type": "Feature", "id": "mass-1",
                 "properties": {"object_type": "existing_shrub_mass"},
                 "geometry": mapping(box(8, 8, 12, 12))},
            ]}), encoding="utf-8")
            report = service.plan(
                root / "zones.jsonl", root / "zone_report.json", root / "normalized.jsonl",
                root / "constraints.jsonl", root / "utilities.jsonl", root / "config.json",
                root / "plan.jsonl", root / "report.json", root / "decisions.jsonl",
                request, root / "explanations.md", existing_shrub_survey_path=survey,
            )
            self.assertEqual(report["composition_advisories"], [])
            polygons = [shape(json.loads(line)["geometry"])
                        for line in (root / "plan.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(polygon.intersection(box(8, 8, 12, 12)).area < 1e-8
                                for polygon in polygons))

    def test_rejects_unconfirmed_or_ambiguous_survey_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "survey.geojson"
            path.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": "x", "properties": {"object_type": "existing_shrub"},
                 "geometry": mapping(box(0, 0, 1, 1))},
            ]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_shrub_survey(path)


if __name__ == "__main__":
    unittest.main()
