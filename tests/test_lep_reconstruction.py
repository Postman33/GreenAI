import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "experiments" / "lep_reconstruction" / "reconstruct.py"
SPEC = importlib.util.spec_from_file_location("lep_reconstruction", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


Arrow = MODULE.Arrow
Primitive = MODULE.Primitive
build_nodes = MODULE.build_nodes
detect_arrows = MODULE.detect_arrows
is_lep_layer = MODULE.is_lep_layer
reconstruct_routes = MODULE.reconstruct_routes


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

    def test_reciprocal_arrows_make_confirmed_route(self):
        arrows = [
            Arrow("a", (0.0, 0.0), (2.5, 0.0)),
            Arrow("b", (20.0, 0.0), (17.5, 0.0)),
        ]
        nodes = build_nodes(arrows)
        routes, used = reconstruct_routes(arrows, nodes)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].confidence, "confirmed")
        self.assertEqual(used, {0, 1})

    def test_one_sided_arrow_is_candidate(self):
        arrows = [
            Arrow("a", (0.0, 0.0), (2.5, 0.0)),
            Arrow("b", (20.0, 0.0), (20.0, 2.5)),
        ]
        nodes = build_nodes(arrows)
        routes, _ = reconstruct_routes(arrows, nodes)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].confidence, "candidate")

    def test_ignores_short_connection_inside_symbol(self):
        arrows = [
            Arrow("a", (0.0, 0.0), (2.0, 0.0)),
            Arrow("b", (4.0, 0.0), (2.0, 0.0)),
        ]
        nodes = build_nodes(arrows)
        routes, _ = reconstruct_routes(arrows, nodes)
        self.assertEqual(routes, [])


if __name__ == "__main__":
    unittest.main()
