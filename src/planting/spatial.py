"""Incremental nearest-neighbour lookup for planting explanations."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely import STRtree, distance
from shapely.geometry import Point


@dataclass
class _PointGroup:
    points: list[Point] = field(default_factory=list)
    ordinals: list[int] = field(default_factory=list)
    tree: STRtree | None = None
    indexed_count: int = 0

    def add(self, point: Point, ordinal: int) -> None:
        self.points.append(point)
        self.ordinals.append(ordinal)
        # Rebuild in batches; also examine the short unindexed tail on every
        # query so a newly accepted planting is visible immediately.
        if len(self.points) - self.indexed_count >= 128:
            self.tree = STRtree(self.points)
            self.indexed_count = len(self.points)

    def nearest(self, point: Point) -> tuple[int, float]:
        candidates: list[int] = []
        if self.tree is not None:
            candidates.append(int(self.tree.query_nearest(point, all_matches=True).min()))
        if self.indexed_count < len(self.points):
            tail = distance(point, self.points[self.indexed_count:])
            candidates.append(self.indexed_count + int(np.argmin(tail)))
        # Original insertion order wins exact-distance ties, as in the full scan.
        return min(((self.ordinals[index], point.distance(self.points[index]))
                    for index in candidates), key=lambda item: (item[1], item[0]))


class PlantingPointIndex:
    """Nearest point per request/profile, with deterministic insertion-order ties.

    A profile has one spacing threshold against a given candidate. Its nearest
    member therefore also has the worst spacing deficit within that profile.
    Keeping one neighbour per profile preserves both nearest-distance and
    worst-violation explanations without scanning every occupied point.
    """

    def __init__(self) -> None:
        self._groups: dict[str, _PointGroup] = {}
        self._count = 0

    def add(self, key: str, point: Point) -> None:
        if key not in self._groups:
            self._groups[key] = _PointGroup()
        self._groups[key].add(point, self._count)
        self._count += 1

    def nearest_by_profile(self, point: Point) -> list[tuple[str, float]]:
        nearest = []
        for key, group in self._groups.items():
            ordinal, actual = group.nearest(point)
            nearest.append((ordinal, key, actual))
        return [(key, actual) for _ordinal, key, actual in sorted(nearest)]
