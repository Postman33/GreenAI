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
from shapely.ops import polylabel, unary_union
from shapely.prepared import prep
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


def tree_grove_layout(
    scope, profile, profiles, occupied, maximum, *, profile_key=None, trace=None,
):
    """Place complete, aligned tree groups in a broad plot, or one focal tree.

    The scheme has a shared local axis and grid phase. A group is kept only
    when all of its trees fit the safe scope and pass the ordinary spacing
    check. This avoids the broken, scattered tails of maximum-density grids.
    """
    if maximum <= 0 or scope.is_empty:
        return []
    polygon = max(polygon_parts(scope), key=lambda item: item.area)
    rectangle = polygon.minimum_rotated_rectangle
    corners = list(rectangle.exterior.coords)
    longest = max(range(4), key=lambda i: math.dist(corners[i], corners[i + 1]))
    start, end = corners[longest], corners[longest + 1]
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    c, s = math.cos(angle), math.sin(angle)
    projected = [(x * c + y * s, -x * s + y * c) for x, y in corners[:-1]]
    min_u, max_u = min(x for x, _ in projected), max(x for x, _ in projected)
    min_v, max_v = min(y for _, y in projected), max(y for _, y in projected)
    spacing = profile.spacing_m
    prepared = prep(scope)
    variants = []
    # Keep visible lawn between groups; a dense lattice of touching crowns
    # reads as an accidental grid rather than a sequence of planting masses.
    for group_size, cell_size in ((5, 3.75 * spacing), (3, 3.25 * spacing)):
        if maximum < group_size:
            continue
        if group_size == 5:
            offsets = ((0, 0), (-spacing, 0), (spacing, 0),
                       (0, -spacing), (0, spacing))
        else:
            height = spacing / math.sqrt(3)
            offsets = ((-spacing / 2, -height / 2),
                       (spacing / 2, -height / 2), (0, height))
        for phase_u in (0.0, 0.5):
            for phase_v in (0.0, 0.5):
                clusters = []
                first_u = math.ceil(min_u / cell_size - phase_u)
                last_u = math.floor(max_u / cell_size - phase_u)
                first_v = math.ceil(min_v / cell_size - phase_v)
                last_v = math.floor(max_v / cell_size - phase_v)
                for row in range(first_v, last_v + 1):
                    v = (row + phase_v) * cell_size
                    for column in range(first_u, last_u + 1):
                        u = (column + phase_u) * cell_size
                        cluster = tuple(
                            ((u + du) * c - (v + dv) * s,
                             (u + du) * s + (v + dv) * c)
                            for du, dv in offsets
                        )
                        if all(prepared.covers(Point(x, y)) for x, y in cluster):
                            clusters.append(cluster)
                if not clusters:
                    continue
                candidates = [point for cluster in clusters for point in cluster]
                packed = pack_candidates(
                    candidates, profile, profiles, occupied,
                    (maximum // group_size) * group_size,
                )
                accepted = set(packed)
                complete = [cluster for cluster in clusters if all(p in accepted for p in cluster)]
                points = [point for cluster in complete for point in cluster]
                variants.append({
                    "points": points,
                    "group_size": group_size,
                    "group_count": len(complete),
                    "phase_u": phase_u,
                    "phase_v": phase_v,
                })
    if variants and any(item["points"] for item in variants):
        best_count = max(len(item["points"]) for item in variants)
        eligible = [item for item in variants if len(item["points"]) >= math.ceil(best_count * 0.85)]
        winner = max(eligible, key=lambda item: (item["group_size"], len(item["points"])))
        if trace is not None:
            trace.update({
                "method": "tree_grove", "angle_deg": round(math.degrees(angle), 3),
                "objective": "complete aligned 3- or 5-tree groups; preserve open space",
                "variants": [{key: item[key] for key in ("group_size", "group_count", "phase_u", "phase_v")}
                             for item in variants],
                "winner": {key: winner[key] for key in ("group_size", "group_count", "phase_u", "phase_v")},
                "winning_grid_rejections": [],
                "audit_scope": "complete groups inside the safe planting scope",
            })
        return winner["points"]

    # A narrow residual pocket is still a valid location for one deliberate
    # specimen: the safe scope already includes mandatory clearance and crown
    # checks. Rejecting it by bounding-box width loses real planting sites.
    center = polylabel(polygon, tolerance=max(spacing / 20, 1e-4))
    focal = pack_candidates([(center.x, center.y)], profile, profiles,
                            occupied, 1)
    if focal:
        if trace is not None:
            trace.update({"method": "focal_tree", "variants": [],
                          "winner": {"accepted_count": 1},
                          "winning_grid_rejections": [],
                          "audit_scope": "largest inscribed point in a residual safe pocket"})
        return focal
    if trace is not None:
        trace.update({"method": "tree_grove", "variants": [], "winner": None,
                      "winning_grid_rejections": [],
                      "audit_scope": "no complete group or focal location fits"})
    return []


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
