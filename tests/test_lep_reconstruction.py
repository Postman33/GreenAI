import json
import tempfile
import unittest
from pathlib import Path

import ezdxf
from shapely.geometry import shape

from tests import ROOT  # noqa: F401 - initializes src imports
from overhead_power_reconstructor import (
    Arrow,
    Primitive,
    build_nodes,
    detect_arrows,
    is_lep_layer,
    merge_accepted_utilities,
    read_lep_entities,
    read_lep_records,
    reconstruct_routes,
    route_feature,
)


class LepReconstructionTest(unittest.TestCase):
    def test_matches_bound_and_plain_layer_names(self):
        self.assertTrue(is_lep_layer("ЛЭП"))
        self.assertTrue(is_lep_layer("3_ДЖКХ$0$Топо_ЛЭП"))
        self.assertFalse(is_lep_layer("Кабель электрический"))

    def test_detects_shaft_with_three_vertex_arrowhead(self):
        primitives = [
            Primitive("shaft", "ЛЭП", ((0.0, 0.0), (2.5, 0.0)), 2.5),
            Primitive("head", "ЛЭП", ((2.0, 0.3), (2.5, 0.0), (2.0, -0.3)), 1.166),
        ]
        arrows = detect_arrows(primitives)
        self.assertEqual(len(arrows), 1)
        self.assertEqual(arrows[0].tail, (0.0, 0.0))
        self.assertEqual(arrows[0].tip, (2.5, 0.0))

    def test_detects_shaft_with_split_arrowhead(self):
        primitives = [
            Primitive("shaft", "ЛЭП", ((0.0, 0.0), (2.5, 0.0)), 2.5),
            Primitive("head-a", "ЛЭП", ((2.5, 0.0), (2.0, 0.3)), 0.583),
            Primitive("head-b", "ЛЭП", ((2.5, 0.0), (2.0, -0.3)), 0.583),
        ]
        arrows = detect_arrows(primitives)
        self.assertEqual(len(arrows), 1)
        self.assertEqual(arrows[0].tip, (2.5, 0.0))

    def test_reads_lep_primitives_inside_transformed_insert(self):
        document = ezdxf.new("R2018")
        document.layers.add("ЛЭП")
        block = document.blocks.new("GEODATA")
        block.add_line((0, 0), (2.5, 0), dxfattribs={"layer": "ЛЭП"})
        document.modelspace().add_blockref("GEODATA", (100, 200), dxfattribs={"rotation": 90})
        primitives, labels = read_lep_entities(document)
        self.assertEqual(labels, [])
        self.assertEqual(len(primitives), 1)
        self.assertAlmostEqual(primitives[0].points[0][0], 100.0)
        self.assertAlmostEqual(primitives[0].points[0][1], 200.0)
        self.assertAlmostEqual(primitives[0].points[1][0], 100.0)
        self.assertAlmostEqual(primitives[0].points[1][1], 202.5)

    def test_reads_fast_extractor_records(self):
        records = [
            {
                "object_type": "building",
                "geometry": {"kind": "line", "start": [0, 0], "end": [1, 0]},
            },
            {
                "object_type": "overhead_power_line",
                "source_layer": "BOUND$0$ЛЭП",
                "dxf_type": "LINE",
                "handle": None,
                "geometry": {"kind": "line", "start": [10, 20], "end": [12.5, 20]},
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "objects.jsonl"
            path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
                encoding="utf-8",
            )
            primitives = read_lep_records(path)
        self.assertEqual(len(primitives), 1)
        self.assertEqual(primitives[0].layer, "BOUND$0$ЛЭП")
        self.assertEqual(primitives[0].points, ((10.0, 20.0), (12.5, 20.0)))

    def test_reciprocal_arrows_make_reciprocal_route(self):
        arrows = [
            Arrow("a", (0.0, 0.0), (2.5, 0.0)),
            Arrow("b", (20.0, 0.0), (17.5, 0.0)),
        ]
        nodes = build_nodes(arrows)
        routes, used = reconstruct_routes(arrows, nodes)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].confidence, "reciprocal")
        self.assertEqual(used, {0, 1})

    def test_one_sided_arrow_is_candidate(self):
        arrows = [
            Arrow("a", (0.0, 0.0), (2.5, 0.0)),
            Arrow("b", (20.0, 0.0), (20.0, 2.5)),
        ]
        nodes = build_nodes(arrows)
        routes, _ = reconstruct_routes(arrows, nodes)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].confidence, "one_sided")

    def test_ignores_short_connection_inside_symbol(self):
        arrows = [
            Arrow("a", (0.0, 0.0), (2.0, 0.0)),
            Arrow("b", (4.0, 0.0), (2.0, 0.0)),
        ]
        nodes = build_nodes(arrows)
        routes, _ = reconstruct_routes(arrows, nodes)
        self.assertEqual(routes, [])

    def test_reciprocal_route_feature_still_requires_manual_semantic_review(self):
        arrows = [
            Arrow("a", (0.0, 0.0), (2.5, 0.0)),
            Arrow("b", (20.0, 0.0), (17.5, 0.0)),
        ]
        nodes = build_nodes(arrows)
        routes, _ = reconstruct_routes(arrows, nodes)
        feature = route_feature(0, routes[0], nodes, [], decision="accepted")
        self.assertEqual(feature["properties"]["decision"], "accepted")
        self.assertTrue(feature["properties"]["manual_review_required"])
        self.assertEqual(feature["properties"]["evidence"], "reciprocal_arrows")
        self.assertEqual(shape(feature["geometry"]).length, 20.0)

    def test_merge_replaces_raw_overhead_graphics(self):
        base_features = [
            {
                "type": "Feature",
                "properties": {"object_type": "gas_pipe", "decision": "accepted"},
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]},
            },
            {
                "type": "Feature",
                "properties": {
                    "object_type": "overhead_power_line",
                    "decision": "accepted",
                    "reason": "raw_arrow_graphics",
                },
                "geometry": {"type": "LineString", "coordinates": [[3, 0], [4, 0]]},
            },
        ]
        accepted_route = {
            "type": "Feature",
            "properties": {
                "object_type": "overhead_power_line",
                "decision": "accepted",
                "reason": "reconstructed_from_reciprocal_arrow_evidence",
            },
            "geometry": {"type": "LineString", "coordinates": [[10, 0], [20, 0]]},
        }
        with tempfile.TemporaryDirectory() as temporary:
            base_path = Path(temporary) / "base.geojsonl"
            output_path = Path(temporary) / "result.geojsonl"
            base_path.write_text(
                "".join(json.dumps(item) + "\n" for item in base_features),
                encoding="utf-8",
            )
            merge_accepted_utilities(base_path, output_path, [accepted_route])
            result = [json.loads(line) for line in output_path.read_text().splitlines()]
        self.assertEqual([item["properties"]["object_type"] for item in result], [
            "gas_pipe",
            "overhead_power_line",
        ])
        self.assertEqual(
            result[1]["properties"]["reason"],
            "reconstructed_from_reciprocal_arrow_evidence",
        )


if __name__ == "__main__":
    unittest.main()
