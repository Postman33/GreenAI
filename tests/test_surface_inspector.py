from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import box

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.cad_io import surface_inspector
from tests.helpers import feature, raw_hatch, write_jsonl


class SurfaceInspectorTests(unittest.TestCase):
    def test_suggested_class(self) -> None:
        self.assertEqual(surface_inspector.suggested_class("Газон"), "plantable_candidate")
        self.assertEqual(surface_inspector.suggested_class("Покрытие тротуара"), "hard_surface_candidate")
        self.assertEqual(surface_inspector.suggested_class("Газон за тротуар"), "ambiguous")
        self.assertEqual(surface_inspector.suggested_class("Красные линии"), "reference_geometry")

    def test_record_polygon_rejects_open_polyline(self) -> None:
        record = {
            "geometry": {
                "kind": "polyline",
                "closed": False,
                "points": [[0, 0], [1, 0], [1, 1]],
            }
        }
        self.assertIsNone(surface_inspector.record_polygon(record, 0.1))

    def test_inspect_surfaces_writes_auditable_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = root / "normalized.geojsonl"
            candidates = root / "candidates.jsonl"
            dxf = root / "surfaces.dxf"
            png = root / "surfaces.png"
            report = root / "surfaces.json"
            write_jsonl(normalized, [feature("work_boundary", box(0, 0, 10, 10))])
            write_jsonl(
                candidates,
                [raw_hatch([(1, 1), (5, 1), (5, 5), (1, 5)], layer="Газон")],
            )

            surface_inspector.inspect_surfaces(
                candidates, normalized, dxf, png, report, 0.1, 0.01, 72
            )
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(dxf.exists())
            self.assertTrue(png.exists())
            self.assertEqual(data["surface_layer_count"], 1)
            self.assertEqual(data["surfaces"][0]["suggested_class"], "plantable_candidate")


if __name__ == "__main__":
    unittest.main()
