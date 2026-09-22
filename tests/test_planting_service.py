from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from shapely.geometry import Point, box, mapping, shape

from tests import ROOT  # noqa: F401 - initializes import paths
from src import planting_service


def write_jsonl(path: Path, features: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in features),
        encoding="utf-8",
    )


def feature(object_type: str, geometry, **properties) -> dict:
    return {
        "type": "Feature",
        "properties": {"object_type": object_type, **properties},
        "geometry": mapping(geometry),
    }


class PlantingServiceTests(unittest.TestCase):
    def test_catalog_dimensions_control_default_spacing_and_crown(self) -> None:
        base = planting_service.PlantingProfile(
            plant_type="tree",
            species="Configured tree",
            spacing_m=5.0,
            footprint_radius_m=2.0,
            symbol_radius_m=2.0,
            avoid_other_plantings_m=1.0,
            max_count=100,
            catalog_reference="config",
            selection_reasons=(),
        )
        selection = planting_service.PlantingSelection(
            request_id="catalog_tree",
            plant_type="tree",
            species="Catalog tree",
            mode="fill_area",
            area=None,
            points=(),
            spacing_m=None,
            max_count=None,
            selection_reasons=(),
            catalog_reference=None,
        )
        report = {
            "plant_catalog": {
                "tree": [{
                    "id": 7,
                    "name": "Catalog tree",
                    "min_spacing_m": 6.0,
                    "recommended_spacing_m": 7.0,
                    "mature_crown_radius_m": 3.0,
                    "dimension_source": "test catalog",
                }]
            }
        }

        resolved = planting_service.resolve_profile(
            selection,
            {"tree": base},
            {"plantingProfiles": []},
            report,
        )
        self.assertEqual(resolved.spacing_m, 7.0)
        self.assertEqual(resolved.footprint_radius_m, 3.0)

        too_dense = replace(selection, spacing_m=5.5)
        with self.assertRaisesRegex(ValueError, "mature crown diameter"):
            planting_service.resolve_profile(
                too_dense,
                {"tree": base},
                {"plantingProfiles": []},
                report,
            )

    def test_automatic_presets_and_tree_overrides(self) -> None:
        config = {
            "plantingProfiles": [
                {
                    "plantType": "tree",
                    "species": "Test tree",
                    "geometryKind": "point",
                    "spacingM": 5.0,
                },
                {
                    "plantType": "shrub",
                    "species": "Test shrub",
                    "geometryKind": "area",
                },
                {
                    "plantType": "herbaceous",
                    "species": "Test lawn",
                    "geometryKind": "area",
                },
            ]
        }

        balanced = planting_service.load_request(None, config, "balanced_mixed")
        self.assertEqual({item.plant_type for item in balanced}, {"tree", "shrub", "herbaceous"})
        self.assertEqual(next(item for item in balanced if item.plant_type == "tree").spacing_m, 6.0)

        dense = planting_service.load_request(None, config, "dense_mixed")
        self.assertEqual(next(item for item in dense if item.plant_type == "tree").spacing_m, 5.0)

        tree_lawn = planting_service.load_request(
            None,
            config,
            "tree_lawn",
            tree_spacing_m=5.5,
            tree_max_count=25,
        )
        self.assertEqual([item.plant_type for item in tree_lawn], ["tree", "herbaceous"])
        tree = tree_lawn[0]
        self.assertEqual(tree.spacing_m, 5.5)
        self.assertEqual(tree.max_count, 25)

        lawn = planting_service.load_request(None, config, "lawn_only")
        self.assertEqual([item.plant_type for item in lawn], ["herbaceous"])

        with self.assertRaisesRegex(ValueError, "Unknown planting preset"):
            planting_service.load_request(None, config, "unknown")

    def make_inputs(self, root: Path, species: str = "Test shrub B") -> dict[str, Path]:
        zones = root / "zones.geojsonl"
        zone_report = root / "zone_report.json"
        normalized = root / "normalized.geojsonl"
        constraints = root / "constraints.geojsonl"
        config = root / "config.json"
        request = root / "request.json"
        write_jsonl(
            zones,
            [feature("plant_allow_zone", box(0, 0, 10, 10), plant_type="shrub")],
        )
        zone_report.write_text(
            json.dumps(
                {
                    "dxf_units_per_meter": 1.0,
                    "plant_types": {"shrub": {"rules": []}},
                    "plant_catalog": {
                        "shrub": [
                            {
                                "id": 12,
                                "name": "Test shrub B",
                                "plant_type": "shrub",
                                "min_spacing_m": 2.0,
                                "is_invasive": False,
                            }
                        ],
                        "herbaceous": [
                            {
                                "id": 20,
                                "name": "Test grass",
                                "plant_type": "herbaceous",
                                "min_spacing_m": 0.0,
                                "is_invasive": False,
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        write_jsonl(normalized, [feature("existing_tree", Point(7, 7))])
        write_jsonl(constraints, [feature("base_allowed_area", box(0, 0, 10, 10))])
        config.write_text(
            json.dumps(
                {
                    "existingTreeCanopyRadiusM": 2.5,
                    "plantingProfiles": [
                        {
                            "plantType": "shrub",
                            "species": "Test shrub A",
                            "geometryKind": "point",
                            "spacingM": 2.0,
                            "footprintRadiusM": 0.5,
                            "symbolRadiusM": 0.5,
                            "avoidOtherPlantingsM": 1.0,
                            "catalogReference": "test catalog",
                        },
                        {
                            "plantType": "herbaceous",
                            "species": "Test grass",
                            "geometryKind": "area",
                            "selectionReasons": ["test grass cover"],
                            "catalogReference": "test grass catalog",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        request.write_text(
            json.dumps(
                {
                    "selections": [
                        {
                            "id": "manual_shrubs",
                            "plant_type": "shrub",
                            "species": species,
                            "mode": "points",
                            "area": mapping(box(1, 1, 9, 9)),
                            "points": [[3, 3], [3.5, 3], [0.5, 0.5]],
                        },
                        {
                            "id": "grass_patch",
                            "plant_type": "herbaceous",
                            "species": "Test grass",
                            "mode": "cover_area",
                            "area": mapping(box(6, 6, 8, 8)),
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "zones": zones,
            "zone_report": zone_report,
            "normalized": normalized,
            "constraints": constraints,
            "config": config,
            "request": request,
        }

    def test_selects_species_area_and_explains_rejected_points(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.make_inputs(root)
            output = root / "plan.geojsonl"
            decisions = root / "decisions.geojsonl"
            report = root / "report.json"
            explanations = root / "explanations.md"
            result = planting_service.plan(
                paths["zones"],
                paths["zone_report"],
                paths["normalized"],
                paths["constraints"],
                root / "missing_utilities.geojsonl",
                paths["config"],
                output,
                report,
                decisions,
                paths["request"],
                explanations,
            )

            plan = [json.loads(row) for row in output.read_text(encoding="utf-8").splitlines()]
            decision_rows = [
                json.loads(row) for row in decisions.read_text(encoding="utf-8").splitlines()
            ]
            points = [item for item in plan if item["properties"]["object_type"] == "proposed_planting"]
            areas = [item for item in plan if item["properties"]["object_type"] == "proposed_planting_area"]

            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]["properties"]["species"], "Test shrub B")
            self.assertIn("plant_catalog#12", str(points[0]["properties"]["checks"]))
            self.assertEqual(len(areas), 1)
            self.assertEqual(areas[0]["properties"]["species"], "Test grass")
            self.assertTrue(shape(areas[0]["geometry"]).covers(Point(7, 7)))
            self.assertIn("743-ПП", str(areas[0]["properties"]["checks"]))
            self.assertIn("2.1.13", str(areas[0]["properties"]["checks"]))
            self.assertEqual(result["rejected_candidate_count"], 2)
            failed = {
                code
                for item in decision_rows
                for code in item["properties"]["failed_checks"]
            }
            self.assertIn("NEW_PLANT_SPACING", failed)
            self.assertIn("ALLOWED_ZONE", failed)
            self.assertIn("USER_SELECTED_AREA", failed)
            explanation_text = explanations.read_text(encoding="utf-8")
            self.assertIn("# Обоснование плана посадок", explanation_text)
            self.assertIn("plant_catalog#12", explanation_text)
            self.assertIn("743-ПП", explanation_text)
            self.assertEqual(result["explanations_output"], str(explanations))

    def test_rejects_species_missing_from_catalog_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.make_inputs(root, species="Unknown shrub")
            with self.assertRaisesRegex(ValueError, "not selectable"):
                planting_service.plan(
                    paths["zones"],
                    paths["zone_report"],
                    paths["normalized"],
                    paths["constraints"],
                    root / "missing_utilities.geojsonl",
                    paths["config"],
                    root / "plan.geojsonl",
                    root / "report.json",
                    root / "decisions.geojsonl",
                    paths["request"],
                )

    def test_default_shrub_area_fills_allow_zone_instead_of_sparse_points(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.make_inputs(root)
            config = json.loads(paths["config"].read_text(encoding="utf-8"))
            config["plantingProfiles"][0]["geometryKind"] = "area"
            paths["config"].write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8"
            )
            output = root / "plan.geojsonl"
            planting_service.plan(
                paths["zones"],
                paths["zone_report"],
                paths["normalized"],
                paths["constraints"],
                root / "missing_utilities.geojsonl",
                paths["config"],
                output,
                root / "report.json",
                root / "decisions.geojsonl",
            )
            plan = [json.loads(row) for row in output.read_text(encoding="utf-8").splitlines()]
            shrub_areas = [
                item for item in plan
                if item["properties"]["plant_type"] == "shrub"
                and item["properties"]["object_type"] == "proposed_planting_area"
            ]
            shrub_points = [
                item for item in plan
                if item["properties"]["plant_type"] == "shrub"
                and item["properties"]["object_type"] == "proposed_planting"
            ]
            self.assertEqual(len(shrub_areas), 1)
            self.assertEqual(shrub_points, [])
            self.assertAlmostEqual(shape(shrub_areas[0]["geometry"]).area, 100.0)
            self.assertTrue(shape(shrub_areas[0]["geometry"]).covers(Point(7, 7)))

    def test_spacing_measurement_respects_dxf_units_per_meter(self) -> None:
        profile = planting_service.PlantingProfile(
            plant_type="shrub",
            species="Test",
            spacing_m=2.0,
            footprint_radius_m=0.5,
            symbol_radius_m=0.5,
            avoid_other_plantings_m=1.0,
            max_count=10,
            catalog_reference="test",
            selection_reasons=(),
        )
        check = planting_service._spacing_check(
            Point(15, 0), profile, {"shrub": profile}, [("shrub", 0, 0)], 10.0
        )
        self.assertEqual(check["status"], "failed")
        self.assertAlmostEqual(check["actual_distance_m"], 1.5)
        self.assertEqual(check["required_distance_m"], 2.0)

    def test_automatic_fill_records_explained_rejected_diagnostic_points(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zones = root / "zones.geojsonl"
            normalized = root / "normalized.geojsonl"
            constraints = root / "constraints.geojsonl"
            zone_report = root / "zone_report.json"
            config = root / "config.json"
            write_jsonl(
                zones,
                [feature("plant_allow_zone", box(0, 0, 20, 10), plant_type="tree")],
            )
            write_jsonl(normalized, [feature("existing_tree", Point(2, 5))])
            write_jsonl(constraints, [feature("base_allowed_area", box(0, 0, 20, 10))])
            zone_report.write_text(
                json.dumps(
                    {
                        "dxf_units_per_meter": 1.0,
                        "plant_types": {"tree": {"rules": []}},
                        "plant_catalog": {
                            "tree": [
                                {
                                    "id": 1,
                                    "name": "Test tree",
                                    "plant_type": "tree",
                                    "min_spacing_m": None,
                                    "is_invasive": False,
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            config.write_text(
                json.dumps(
                    {
                        "existingTreeCanopyRadiusM": 2.5,
                        "existingTreeClearanceM": 2.0,
                        "diagnosticRejectedMaxCount": 20,
                        "plantingProfiles": [
                            {
                                "plantType": "tree",
                                "species": "Test tree",
                                "geometryKind": "point",
                                "spacingM": 6.0,
                                "footprintRadiusM": 2.5,
                                "symbolRadiusM": 2.5,
                                "avoidOtherPlantingsM": 2.0,
                                "catalogReference": "test catalog",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            decisions = root / "decisions.geojsonl"
            report = planting_service.plan(
                zones,
                zone_report,
                normalized,
                constraints,
                root / "missing.geojsonl",
                config,
                root / "plan.geojsonl",
                root / "report.json",
                decisions,
            )
            rows = [
                json.loads(line)
                for line in decisions.read_text(encoding="utf-8").splitlines()
            ]
            diagnostic = [
                row
                for row in rows
                if row["properties"].get("diagnostic_candidate")
            ]
            self.assertGreater(len(diagnostic), 0)
            self.assertGreater(report["summary"]["auto_tree"]["diagnostic_rejected_count"], 0)
            self.assertTrue(
                any(
                    check["code"] in {
                        "PLANT_FOOTPRINT_INSIDE_ZONE",
                        "EXISTING_TREE_CLEARANCE",
                        "NEW_PLANT_SPACING",
                    }
                    and check["status"] == "failed"
                    for row in diagnostic
                    for check in row["properties"]["checks"]
                )
            )
            self.assertTrue(diagnostic[0]["properties"]["rejection_reasons"])

if __name__ == "__main__":
    unittest.main()
