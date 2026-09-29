from __future__ import annotations

import unittest

from tests import ROOT  # noqa: F401
from src.domain.models import PlantingProfile
from src.planting.layout_optimizer import optimize_layouts


class LayoutOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.profile = PlantingProfile("tree", "Tree", 2, 0.5, 0.5, 0, 20, "test", ())

    def test_selects_compatible_schemes_across_beds(self):
        options = [
            {"bed": 0, "points": [(0, 0), (0, 4)], "score": 3, "style": "row_a"},
            {"bed": 0, "points": [(3, 0), (3, 4)], "score": 2, "style": "row_b"},
            {"bed": 1, "points": [(0, 1), (0, 5)], "score": 2, "style": "row_c"},
        ]
        chosen, trace = optimize_layouts(options, self.profile, 4)
        self.assertEqual(chosen, [1, 2])
        self.assertEqual(trace["solver"], "cp_sat")
        self.assertEqual(trace["status"], "OPTIMAL")
        self.assertEqual(trace["selected_count"], 4)
        self.assertEqual(trace["conflict_count"], 1)

    def test_count_limit_is_hard(self):
        options = [
            {"bed": 0, "points": [(0, 0), (0, 4)], "score": 3, "style": "row_a"},
            {"bed": 1, "points": [(10, 0), (10, 4)], "score": 2, "style": "row_b"},
        ]
        chosen, trace = optimize_layouts(options, self.profile, 2)
        self.assertEqual(chosen, [0])
        self.assertEqual(trace["selected_count"], 2)


if __name__ == "__main__":
    unittest.main()
