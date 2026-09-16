from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ezdxf

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src import loader


class LoaderTests(unittest.TestCase):
    def test_layer_tail_and_mapping_filters(self) -> None:
        doc = ezdxf.new()
        line = doc.modelspace().add_line(
            (0, 0), (1, 0), dxfattribs={"layer": "xref$0$Water"}
        )
        mapping = {
            "source": "geobase_blocks",
            "dxf_types": ["LINE"],
            "layer_tail_in": ["water"],
            "exclude_layer_contains": ["project"],
        }
        self.assertEqual(loader.layer_tail(line.dxf.layer), "Water")
        self.assertTrue(loader.mapping_matches(mapping, line, "geobase_blocks"))
        self.assertFalse(loader.mapping_matches(mapping, line, "modelspace"))

    def test_geometry_extracts_common_entities(self) -> None:
        doc = ezdxf.new()
        modelspace = doc.modelspace()
        line = modelspace.add_line((1, 2), (3, 4))
        circle = modelspace.add_circle((5, 6), 2)
        polyline = modelspace.add_lwpolyline([(0, 0), (1, 0), (1, 1)], close=True)

        self.assertEqual(loader.geometry(line)["kind"], "line")
        self.assertEqual(loader.json_value(loader.geometry(circle)["center"]), [5.0, 6.0, 0.0])
        self.assertTrue(loader.geometry(polyline)["closed"])

    def test_extract_reads_modelspace_and_nested_geobase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.dxf"
            output = root / "objects.jsonl"
            doc = ezdxf.new("R2018")
            doc.layers.add("BOUNDARY")
            doc.layers.add("Water")
            doc.modelspace().add_lwpolyline(
                [(0, 0), (10, 0), (10, 10), (0, 10)],
                close=True,
                dxfattribs={"layer": "BOUNDARY"},
            )
            block = doc.blocks.new("Geo_Test")
            block.add_line(
                (0, 0),
                (5, 0),
                dxfattribs={"layer": "Water", "color": 3, "linetype": "BYLAYER"},
            )
            doc.modelspace().add_blockref("Geo_Test", (100, 200))
            doc.saveas(source)

            config = {
                "geobase_blocks": {"name_regex": ["^Geo_"], "max_depth": 5},
                "layer_mapping": {
                    "work_boundary": {
                        "semantic_type": "work_boundary",
                        "source": "modelspace",
                        "dxf_types": ["LWPOLYLINE"],
                        "layer_tail_in": ["BOUNDARY"],
                        "geometry": "polygon",
                        "conversion": "closed_polyline_to_polygon",
                    },
                    "water_pipe": {
                        "semantic_type": "water_pipe",
                        "source": "geobase_blocks",
                        "dxf_types": ["LINE"],
                        "layer_tail_in": ["Water"],
                        "geometry": "multiline",
                        "conversion": "merge_linework",
                    },
                },
            }
            counts, dxf_counts, missing_required = loader.extract(source, config, output)
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(counts, {"work_boundary": 1, "water_pipe": 1})
            self.assertEqual(dxf_counts["LINE"], 1)
            self.assertEqual(missing_required, set())
            water = next(item for item in records if item["object_type"] == "water_pipe")
            self.assertEqual(water["geometry"]["start"][:2], [100.0, 200.0])
            self.assertEqual(water["geometry"]["end"][:2], [105.0, 200.0])
            self.assertEqual(water["color"], 3)
            self.assertIsNone(water["true_color"])
            self.assertEqual(water["linetype"], "BYLAYER")


if __name__ == "__main__":
    unittest.main()
