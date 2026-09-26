"""Deterministic design geometry; callers retain all planting rule checks.

Coordinates and distances here are in DXF units. No regulatory setbacks or
species suitability decisions belong in this module.
"""

from __future__ import annotations

import math
import random
from typing import Any

from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, Point, box
from shapely.ops import unary_union
from shapely.validation import make_valid

from .placement_generator import grid_candidates, pack_candidates, polygon_parts


STYLE_CONTRACTS = {
    "alley": ("tree", "fill_area"),
    "hedge": ("shrub", "cover_area"),
    "shrub_mass": ("shrub", "cover_area"),
    "free_group": ("tree", "fill_area"),
    "mixed_flowerbed": ("herbaceous", "cover_area"),
}


def principal_axis(polygon: Any) -> LineString:
    """Long axis of the reference polygon, independent of exclusion holes."""
    rectangle = polygon.minimum_rotated_rectangle
    corners = list(rectangle.exterior.coords)
    edges = [(math.dist(corners[i], corners[i + 1]), i) for i in range(4)]
    length, index = max(edges)
    a, b = corners[index], corners[index + 1]
    dx, dy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    if dx < -1e-12 or (abs(dx) <= 1e-12 and dy < 0):
        dx, dy = -dx, -dy
    center = rectangle.centroid
    return LineString([
        (center.x - dx * length / 2, center.y - dy * length / 2),
        (center.x + dx * length / 2, center.y + dy * length / 2),
    ])


def design_axes(reference: Any, guide: Any | None) -> list[LineString]:
    if guide is not None:
        return [guide]
    return [principal_axis(p) for p in sorted(polygon_parts(reference), key=lambda p: (-p.area, p.bounds))]


def alley_layout(scope, reference, guide, profile, profiles, occupied, maximum, rows=1, profile_key=None):
    """One/two aligned rows. Obstacles remove stations without bending the row."""
    generated = []
    for axis in design_axes(reference, guide):
        offsets = (0.0,) if rows == 1 else (-profile.spacing_m / 2, profile.spacing_m / 2)
        lines = [axis if offset == 0 else axis.offset_curve(offset) for offset in offsets]
        best = []
        # Pick a common phase for both rows; all pieces keep the same rhythm.
        for phase in (0.0, 0.25, 0.5, 0.75):
            candidates = []
            for line in lines:
                if line.is_empty or line.geom_type != "LineString":
                    continue
                distance = phase * profile.spacing_m
                while distance <= line.length + 1e-9:
                    point = line.interpolate(distance)
                    if scope.covers(point):
                        candidates.append((point.x, point.y))
                    distance += profile.spacing_m
            packed = pack_candidates(
                candidates, profile, profiles,
                occupied + [(profile_key or profile.plant_type, x, y) for x, y in generated],
                maximum - len(generated),
            )
            if len(packed) > len(best):
                best = packed
        generated.extend(best)
        if len(generated) >= maximum:
            break
    return generated


def hedge_coverage(coverage, reference, guide, width):
    corridors = [line.buffer(width / 2, cap_style=2) for line in design_axes(reference, guide)]
    return coverage.intersection(unary_union(corridors))


def free_group_layout(scope, profile, profiles, occupied, maximum, seed=0, profile_key=None):
    """Small irregular groves with open space between groups, reproducibly."""
    rng = random.Random(seed)
    generated = []
    spacing = profile.spacing_m
    for component in sorted(polygon_parts(scope), key=lambda p: (-p.area, p.bounds)):
        anchors = list(grid_candidates(component, spacing * 4.5, 0, 0.5, 0.5))
        if not anchors:
            center = component.representative_point()
            anchors = [(center.x, center.y)]
        rng.shuffle(anchors)
        for cx, cy in anchors:
            angle = rng.uniform(0, math.tau)
            candidates = [(cx, cy)]
            for i in range(5):
                direction = angle + i * math.tau / 5 + rng.uniform(-0.08, 0.08)
                radius = spacing * rng.uniform(1.1, 1.25)
                candidates.append((cx + radius * math.cos(direction), cy + radius * math.sin(direction)))
            candidates = [(x, y) for x, y in candidates if scope.covers(Point(x, y))]
            packed = pack_candidates(
                candidates, profile, profiles,
                occupied + [(profile_key or profile.plant_type, x, y) for x, y in generated],
                maximum - len(generated),
            )
            # A single leftover point is not a group. A count cap may leave space unused.
            if len(packed) >= 2:
                generated.extend(packed)
            if len(generated) >= maximum:
                return generated
    return generated


def flowerbed_patches(coverage: Any, composition: tuple[dict, ...]):
    """Partition each connected bed into contiguous species bands by area share.

    Binary area cuts handle concavity and holes. Shares mean area, not numbers
    of plants. No point locations or unverified planting density are invented.
    """
    for polygon in polygon_parts(coverage):
        axis = principal_axis(polygon)
        a, b = axis.coords
        angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        origin = (polygon.centroid.x, polygon.centroid.y)
        # Rotate only a simple envelope/cutting rectangle, never the detailed
        # CAD boundary. Round-tripping tiny CAD rings through two rotations can
        # invalidate holes at survey-scale coordinates.
        min_x, min_y, max_x, max_y = affinity.rotate(polygon.envelope, -angle, origin=origin).bounds
        pad = max(max_x - min_x, max_y - min_y, 1.0)

        def clip_at(cut):
            half = affinity.rotate(box(min_x - pad, min_y - pad, cut, max_y + pad), angle, origin=origin)
            return make_valid(polygon.intersection(half))

        previous = GeometryCollection()
        cumulative = 0.0
        for index, item in enumerate(composition):
            cumulative += item["share"]
            if index == len(composition) - 1:
                prefix = polygon
            else:
                low, high = min_x, max_x
                target = polygon.area * cumulative
                for _ in range(48):
                    cut = (low + high) / 2
                    prefix = clip_at(cut)
                    if prefix.area < target:
                        low = cut
                    else:
                        high = cut
                prefix = clip_at((low + high) / 2)
            patch = make_valid(prefix.difference(previous))
            previous = prefix
            for part in polygon_parts(patch):
                if part.area > 0:
                    yield part, item
