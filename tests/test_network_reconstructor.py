from __future__ import annotations

import unittest

from shapely.geometry import LineString

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from src.detection.network_reconstructor import (
    candidate_connector,
    default_rules,
    reconstruct_type,
    scaled_rules,
)


def rules(**overrides):
    config = {"defaults": {**default_rules(), **overrides}}
    return scaled_rules(config, "water_pipe", 1.0)


class NetworkReconstructorTests(unittest.TestCase):
    def test_reconstructs_collinear_gap(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(12, 0), (20, 0)]),
        ]

        accepted, review, endpoints = reconstruct_type(lines, rules())

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].kind, "continuation")
        self.assertEqual(list(candidate_connector(accepted[0], endpoints).coords), [(10.0, 0.0), (12.0, 0.0)])
        self.assertEqual(review, [])

    def test_preserves_both_sides_of_wide_pipe(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(12, 0), (20, 0)]),
            LineString([(0, 2), (10, 2)]),
            LineString([(12, 2), (20, 2)]),
        ]

        accepted, _, endpoints = reconstruct_type(lines, rules())
        connectors = {
            tuple(candidate_connector(item, endpoints).coords)
            for item in accepted
        }

        self.assertEqual(len(accepted), 2)
        self.assertEqual(connectors, {
            ((10.0, 0.0), (12.0, 0.0)),
            ((10.0, 2.0), (12.0, 2.0)),
        })
        self.assertTrue(all(item.parallel_pipe_support for item in accepted))

    def test_reconstructs_t_junction_to_line_interior(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(5, -5), (5, -1)]),
        ]

        accepted, _, endpoints = reconstruct_type(lines, rules())
        junctions = [item for item in accepted if item.kind == "junction"]

        self.assertEqual(len(junctions), 1)
        self.assertEqual(
            list(candidate_connector(junctions[0], endpoints).coords),
            [(5.0, -1.0), (5.0, 0.0)],
        )

    def test_allows_multiple_continuations_for_wide_pipe_fan(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(14, 0.2), (20, 0.2)]),
            LineString([(14, -0.2), (20, -0.2)]),
        ]

        accepted, _, endpoints = reconstruct_type(lines, rules())
        from_common_end = [
            item
            for item in accepted
            if tuple(candidate_connector(item, endpoints).coords)[0] == (10.0, 0.0)
        ]

        self.assertEqual(len(from_common_end), 2)
        self.assertTrue(all(item.wide_pipe_fan for item in from_common_end))

    def test_does_not_join_lines_with_wrong_direction(self) -> None:
        lines = [
            LineString([(0, 0), (10, 0)]),
            LineString([(12, 1), (12, 10)]),
        ]

        accepted, _, _ = reconstruct_type(lines, rules(enable_junctions=False))

        self.assertEqual(accepted, [])

    def test_one_continuous_line_needs_no_connector(self) -> None:
        accepted, review, _ = reconstruct_type(
            [LineString([(0, 0), (10, 0), (20, 0)])],
            rules(),
        )

        self.assertEqual(accepted, [])
        self.assertEqual(review, [])


if __name__ == "__main__":
    unittest.main()
