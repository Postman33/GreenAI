"""Differential checks against exhaustive geometry calculations."""

from __future__ import annotations

import math
import random
import unittest
from dataclasses import replace
from itertools import combinations, islice

from shapely.geometry import Point, Polygon, box

from scripts.verify_outputs import count_same_type_spacing_failures
from src.domain.models import PlantingProfile
from src.planting.placement_generator import _occupied_grid, grid_candidates, pack_candidates
from src.planting.service import _spacing_check
from src.planting.spatial import PlantingPointIndex


def profile(name: str, plant_type: str = "tree", spacing: float = 6.0) -> PlantingProfile:
    return PlantingProfile(plant_type=plant_type, species=name, spacing_m=spacing,
                          footprint_radius_m=1.0, symbol_radius_m=1.0,
                          avoid_other_plantings_m=2.0, max_count=100,
                          catalog_reference="test", selection_reasons=())


def exhaustive_count(items):
    count = 0
    for (left, first), (right, second) in combinations(items, 2):
        first_units = left.get("dxf_units_per_meter", 1.0)
        second_units = right.get("dxf_units_per_meter", 1.0)
        threshold = max(left.get("spacing_m", 0.0) * first_units,
                        right.get("spacing_m", 0.0) * second_units,
                        left.get("footprint_radius_m", 0.0) * first_units
                        + right.get("footprint_radius_m", 0.0) * second_units)
        count += first.distance(second) + 1e-7 < threshold
    return count


def scalar_grid(polygon, spacing, angle, phase_x, phase_y):
    """Original coordinate arithmetic, with individual exact covers queries."""
    min_x, min_y, max_x, max_y = polygon.bounds
    cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
    cosine, sine = math.cos(angle), math.sin(angle)
    projected = [((x - cx) * cosine + (y - cy) * sine,
                  -(x - cx) * sine + (y - cy) * cosine)
                 for x, y in [(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)]]
    step_y = spacing * math.sqrt(3) / 2
    min_u, max_u = min(p[0] for p in projected) - spacing, max(p[0] for p in projected) + spacing
    max_v = max(p[1] for p in projected) + step_y
    v = min(p[1] for p in projected) - step_y + phase_y * step_y
    row, result = 0, []
    while v <= max_v + 1e-9:
        u = min_u + phase_x * spacing + (0.0 if row % 2 == 0 else spacing / 2)
        while u <= max_u + 1e-9:
            x, y = cx + u * cosine - v * sine, cy + u * sine + v * cosine
            if polygon.covers(Point(x, y)):
                result.append((x, y))
            u += spacing
        v += step_y
        row += 1
    return result


class SpatialOptimizationTests(unittest.TestCase):
    def test_pair_count_matches_exhaustive_with_mixed_sizes_and_units(self):
        rng = random.Random(451)
        for count in (0, 1, 2, 120):
            items = [({"spacing_m": rng.choice([0.0, 0.003, 1.2, 8.0]),
                       "footprint_radius_m": rng.choice([0.0, 0.002, 2.0]),
                       "dxf_units_per_meter": rng.choice([0.01, 1.0, 1000.0])},
                      Point(rng.uniform(-50, 50), rng.uniform(-50, 50))) for _ in range(count)]
            self.assertEqual(count_same_type_spacing_failures(items), exhaustive_count(items))

    def test_pair_count_keeps_duplicate_points_and_distance_tolerance(self):
        items = [({"spacing_m": 2.0}, Point(x, 0))
                 for x in [0, 0, 2.0, 2.0 - 0.5e-7, 2.0 - 2e-7, 2.0 + 2e-7]]
        self.assertEqual(count_same_type_spacing_failures(items), exhaustive_count(items))
        self.assertEqual(count_same_type_spacing_failures([({}, Point(0, 0))] * 2), 0)

    def test_pair_search_accounts_for_larger_neighbour_crown(self):
        items = [({"footprint_radius_m": radius}, Point(x, 0))
                 for radius, x in [(0.1, 0), (10.0, 9), (0.1, 19)]]
        self.assertEqual(count_same_type_spacing_failures(items), 2)

    def test_nonfinite_metadata_does_not_hide_pairs_during_index_search(self):
        for invalid in (math.inf, math.nan):
            items = [({"spacing_m": 2}, Point(0, 0)),
                     ({"spacing_m": invalid}, Point(1, 0)),
                     ({"footprint_radius_m": 3}, Point(4, 0))]
            self.assertEqual(count_same_type_spacing_failures(items), exhaustive_count(items))

    def test_incremental_explanations_match_full_scan_across_rebuilds(self):
        rng = random.Random(810)
        profiles = {"small": profile("small", "shrub", 1.2),
                    "large": replace(profile("large"), footprint_radius_m=4.0),
                    "same_type_other_species": profile("other", spacing=15.0)}
        occupied, index = [], PlantingPointIndex()
        for ordinal in range(300):
            key = rng.choice(list(profiles))
            point = Point(rng.uniform(-20, 20), rng.uniform(-20, 20))
            # Mix duplicate points with geometries on both sides of each rebuild.
            if ordinal in {127, 128, 255, 256}:
                point = Point(1, 0)
            index.add(key, point)
            occupied.append((key, point.x, point.y))
            if ordinal % 31 == 0 or ordinal in {127, 128, 255, 256, 299}:
                for units in (0.01, 1.0, 1000.0):
                    query = Point(rng.uniform(-25, 25), rng.uniform(-25, 25))
                    for candidate in profiles.values():
                        self.assertEqual(_spacing_check(query, candidate, profiles, occupied, units, index),
                                         _spacing_check(query, candidate, profiles, occupied, units))

    def test_nearest_and_worst_violation_ties_follow_insertion_order(self):
        profiles = {"a": profile("a", spacing=2), "b": profile("b", spacing=2),
                    "far": profile("far", spacing=20)}
        for occupied in [[], [("a", 1, 0), ("b", -1, 0)],
                         [("a", 100, 0), ("b", -1, 0), ("a", 1, 0)],
                         [("a", 1, 0), ("far", 10, 0)],
                         [("a", 0, 1), ("b", 0, -1), ("a", 0, -1)]]:
            index = PlantingPointIndex()
            for key, x, y in occupied:
                index.add(key, Point(x, y))
            self.assertEqual(_spacing_check(Point(0, 0), profiles["a"], profiles, occupied, 1, index),
                             _spacing_check(Point(0, 0), profiles["a"], profiles, occupied, 1))

    def test_nearest_ties_between_tree_and_pending_points(self):
        index, occupied = PlantingPointIndex(), []
        profiles = {"a": profile("a"), "b": profile("b")}
        entries = [("a", 1.0, 0.0)] + [("a", 100.0 + i, 0.0) for i in range(127)]
        entries += [("b", 0.0, 1.0), ("a", -1.0, 0.0)]
        for key, x, y in entries:
            index.add(key, Point(x, y))
            occupied.append((key, x, y))
        self.assertEqual(_spacing_check(Point(0, 0), profiles["a"], profiles, occupied, 1, index),
                         _spacing_check(Point(0, 0), profiles["a"], profiles, occupied, 1))

    def test_vectorized_grid_preserves_coordinates_order_holes_and_edges(self):
        polygons = [box(0, 0, 6, 5), box(-4, -2, 5, 4).difference(box(-1, -1, 1, 1)),
                    Polygon([(0, 0), (8, 0), (8, 1), (1, 1), (1, 8), (0, 8)])]
        for polygon in polygons:
            for angle in (0.0, math.pi / 18, 5 * math.pi / 18):
                for phase_x, phase_y in [(0.0, 0.0), (0.25, 0.5), (0.75, 0.75)]:
                    self.assertEqual(list(grid_candidates(polygon, 1.0, angle, phase_x, phase_y)),
                                     scalar_grid(polygon, 1.0, angle, phase_x, phase_y))

    def test_reused_occupancy_grid_does_not_leak_placements_between_variants(self):
        p = profile("test", spacing=2.0)
        occupied = [("test", -1.0, 0.0), ("test", 8.0, 1.0)]
        grid = _occupied_grid(p, {"test": p}, occupied)
        for candidates in [[(x, 0.0) for x in range(12)], [(x, 1.0) for x in range(12)]] * 2:
            trace, cached_trace = [], []
            expected = pack_candidates(candidates, p, {"test": p}, occupied, 3, trace)
            actual = pack_candidates(candidates, p, {"test": p}, occupied, 3, cached_trace,
                                     occupied_grid=grid)
            self.assertEqual(actual, expected)
            self.assertEqual(cached_trace, trace)
        self.assertEqual(sum(map(len, grid[1].values())), len(occupied))

    def test_grid_keeps_lazy_generation_on_very_long_rows(self):
        candidates = grid_candidates(box(0, 0, 100_000_000, 5), 1.0, 0.0, 0.0, 1.0)
        self.assertEqual(list(islice(candidates, 3)), [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])


if __name__ == "__main__":
    unittest.main()
