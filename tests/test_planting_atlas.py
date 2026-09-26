from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image as PillowImage
from pypdf import PdfReader
from shapely.geometry import Point, box, mapping

from scripts.generate_planting_atlas import build_atlas


def write_features(path: Path, features: list[dict]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in features), encoding="utf-8")


def feature(object_type: str, geometry, **properties) -> dict:
    return {"type": "Feature", "properties": {"object_type": object_type, **properties},
            "geometry": mapping(geometry)}


class PlantingAtlasTests(unittest.TestCase):
    def test_two_sections_reconcile_counts_and_areas_and_render_maps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            constraints = root / "constraints.geojsonl"
            zones = root / "zones.geojsonl"
            plan = root / "plan.geojsonl"
            pdf = root / "atlas.pdf"
            schedule = root / "schedule.json"
            image_dir = root / "views"
            write_features(normalized, [
                feature("work_boundary", box(0, 0, 200, 100)),
                feature("building", box(5, 5, 15, 15)),
            ])
            write_features(constraints, [
                feature("base_allowed_area", box(0, 0, 200, 100)),
                feature("road_area", box(0, 0, 200, 10)),
            ])
            write_features(zones, [
                feature("plant_allow_zone", box(0, 10, 200, 100), plant_type="tree"),
                feature("plant_allow_zone", box(0, 10, 200, 100), plant_type="shrub"),
            ])
            write_features(plan, [
                feature("proposed_planting", Point(50, 50), plant_type="tree",
                        species="Липа", footprint_radius_m=3, dxf_units_per_meter=1),
                feature("proposed_planting", Point(150, 50), plant_type="shrub",
                        species="Дерен", footprint_radius_m=1, dxf_units_per_meter=1),
                feature("proposed_planting_area", box(20, 20, 180, 40),
                        plant_type="herbaceous", species="Газон", dxf_units_per_meter=1),
            ])

            build_atlas(normalized, constraints, zones, plan, pdf, schedule,
                        tile_size_m=100, preview_directory=image_dir)

            data = json.loads(schedule.read_text(encoding="utf-8"))
            self.assertEqual(len(data["sections"]), 2)
            self.assertEqual(sum(row["tree_count"] for row in data["sections"]), 1)
            self.assertEqual(sum(row["shrub_count"] for row in data["sections"]), 1)
            self.assertAlmostEqual(sum(row["herbaceous_area_m2"] for row in data["sections"]), 3200)
            self.assertEqual({row["herbaceous_area_m2"] for row in data["sections"]}, {1600})
            self.assertEqual(data["totals"]["herbaceous_area_m2"], 3200)
            self.assertGreater(data["sections"][0]["tree_crown_projection_m2"], 25)
            self.assertGreater(data["sections"][1]["shrub_projection_m2"], 3)
            self.assertEqual(len(PdfReader(pdf).pages), 3)
            overview = PillowImage.open(image_dir / "overview.png").convert("RGB")
            self.assertGreater(len(overview.getcolors(overview.width * overview.height) or []), 20)
            self.assertTrue((image_dir / "У-01_context.png").is_file())
            self.assertTrue((image_dir / "У-02_planting.png").is_file())


if __name__ == "__main__":
    unittest.main()
