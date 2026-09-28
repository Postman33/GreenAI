"""Deterministic design geometry; callers retain all planting rule checks.

Coordinates and distances here are in DXF units. No regulatory setbacks or
species suitability decisions belong in this module.
"""

from __future__ import annotations

import math
import random
from typing import Any

from shapely import STRtree, affinity
from shapely.geometry import GeometryCollection, LineString, Point, box
from shapely.ops import polylabel, unary_union
from shapely.prepared import prep
from shapely.validation import make_valid

from .placement_generator import (
    best_component_layout, best_linear_layout, grid_candidates, linear_reference, pack_candidates,
    polygon_parts,
)


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


def score_tree_composition(
    points: list[tuple[float, float]],
    reference: Any,
    profile: Any,
    style: str,
    existing_trees: Any | None = None,
    shade_target: Any | None = None,
) -> dict[str, float | int]:
    """Comparable design score; rule compliance is checked before and after it.

    The score describes visual structure, not tree health or legal permission.
    Existing trees provide a rhythm anchor and are never subtracted here.
    """
    if not points:
        return {"score": 0.0, "count": 0, "adjacent_pairs": 0,
                "isolated": 0, "existing_tree_links": 0,
                "site_fit": 0.0, "open_space": 0.0,
                "pedestrian_canopy_overlap": 0.0, "shade_proxy_score": 0.0}
    spacing = profile.spacing_m
    tree_points = [Point(x, y) for x, y in points]
    adjacent_pairs = 0
    isolated = 0
    if len(tree_points) > 1:
        index = STRtree(tree_points)
        for left, point in enumerate(tree_points):
            neighbours = 0
            for raw_right in index.query(point.buffer(1.35 * spacing).envelope):
                right = int(raw_right)
                if right == left:
                    continue
                distance = point.distance(tree_points[right])
                if distance <= 1.35 * spacing + 1e-7:
                    neighbours += 1
                    if right > left:
                        adjacent_pairs += 1
            isolated += neighbours == 0
    anchored = 0
    if existing_trees is not None and not existing_trees.is_empty and points:
        originals = (list(existing_trees.geoms)
                     if hasattr(existing_trees, "geoms") else [existing_trees])
        originals = [point for point in originals if isinstance(point, Point)]
        if originals:
            index = STRtree(originals)
            for point in tree_points:
                anchored += any(
                    0.7 * spacing <= point.distance(originals[int(raw)]) <= 2.1 * spacing
                    for raw in index.query(point.buffer(2.1 * spacing).envelope)
                )
    elongated = linear_reference(reference, spacing) is not None
    fit = (3.0 if elongated else -1.5) if style == "linear" else (
        -1.0 if elongated else 2.0
    ) if style in {"tree_grove", "free_group"} else 0.0
    open_space = 1.0 if style == "tree_grove" and len(points) >= 3 else 0.0
    shade_overlap = 0.0
    shade_score = 0.0
    radius = profile.footprint_radius_m
    if (shade_target is not None and not shade_target.is_empty and radius > 0):
        crowns = unary_union([point.buffer(radius, quad_segs=8) for point in tree_points])
        shade_overlap = crowns.intersection(shade_target).area
        # A local canopy-footprint proxy, not a sun-position or cooling model.
        shade_score = .5 * shade_overlap / (math.pi * radius * radius)
    # Count matters, but one extra point cannot outweigh a broken rhythm.
    score = (len(points) + 1.1 * adjacent_pairs - 1.5 * isolated
             + 0.6 * anchored + fit + open_space + shade_score)
    return {
        "score": round(score, 4), "count": len(points),
        "adjacent_pairs": adjacent_pairs, "isolated": isolated,
        "existing_tree_links": anchored, "site_fit": fit,
        "open_space": open_space,
        "pedestrian_canopy_overlap": round(shade_overlap, 4),
        "shade_proxy_score": round(shade_score, 4),
    }


def choose_tree_composition(
    scope: Any, reference: Any, profile: Any, profiles: dict[str, Any],
    occupied: list[tuple[str, float, float]], maximum: int,
    existing_trees: Any | None = None, *, profile_key: str | None = None,
    shade_target: Any | None = None, trace: dict[str, Any] | None = None,
    include_alternatives: bool = False,
) -> tuple[list[tuple[float, float]], str]:
    """Compare complete, rule-safe tree arrangements for one planting bed."""
    if maximum <= 0 or scope.is_empty:
        if trace is not None:
            trace.update({"method": "composition", "variants": [], "winner": None,
                          "winning_grid_rejections": []})
        return [], "composition"
    variants: list[dict[str, Any]] = []
    seen_layouts: set[tuple[tuple[float, float], ...]] = set()

    def add(style: str, points: list[tuple[float, float]], detail: dict[str, Any]):
        signature = tuple(points)
        if points and signature not in seen_layouts:
            seen_layouts.add(signature)
            variants.append({"style": style, "points": points, "detail": detail,
                             "quality": score_tree_composition(
                                 points, reference, profile, style, existing_trees,
                                 shade_target,
                             )})

    linear_trace: dict[str, Any] = {}
    linear = best_linear_layout(scope, reference, profile, profiles, occupied,
                                maximum, trace=linear_trace,
                                collect_alternatives=include_alternatives)
    add("linear", linear or [], linear_trace)
    if include_alternatives:
        for item in linear_trace.get("candidate_layouts", []):
            add("linear", item["points"], {"method": "linear", "phase_u": item["phase_u"],
                                              "phase_v": item["phase_v"]})

    grove_points: list[tuple[float, float]] = []
    grove_details = []
    for part in sorted(polygon_parts(scope), key=lambda item: (-item.area, item.bounds)):
        remaining = maximum - len(grove_points)
        if remaining <= 0:
            break
        detail: dict[str, Any] = {}
        found = tree_grove_layout(
            part, profile, profiles,
            occupied + [(profile_key or profile.plant_type, x, y)
                        for x, y in grove_points],
            remaining, profile_key=profile_key, trace=detail,
        )
        grove_points.extend(found)
        grove_details.append(detail)
    grove_style = ("tree_grove" if any(item.get("method") == "tree_grove"
                                 and item.get("winner") for item in grove_details)
                   else "focal_tree")
    add(grove_style, grove_points, {"components": grove_details})

    # A repeatable informal composition offers a genuine alternative to a
    # fixed three/five-tree motif on broad, irregular beds.
    if linear_reference(reference, profile.spacing_m) is None:
        groups = free_group_layout(scope, profile, profiles, occupied, maximum,
                                   seed=2026, profile_key=profile_key)
        add("free_group", groups, {"method": "free_group", "seed": 2026})

    if not variants:
        if trace is not None:
            trace.update({"method": "composition", "variants": [], "winner": None,
                          "winning_grid_rejections": []})
        return [], "composition"
    winner = max(variants, key=lambda item: (
        item["quality"]["score"], item["quality"]["count"],
        item["style"] == "linear", item["style"],
    ))
    if trace is not None:
        trace.update({
            "method": "composition", "objective": "compare safe whole-bed schemes by "
                "plant count, rhythm, isolated trees, site form, existing-tree "
                "links and pedestrian canopy overlap when sidewalk geometry exists",
            "score_weights": {"tree": 1.0, "adjacent_pair": 1.1,
                              "isolated_tree": -1.5, "existing_tree_link": 0.6,
                              "pedestrian_canopy_equivalent": 0.5},
            "not_evaluated": ["species_mix", "sun_angle", "tree_health"],
            "variants": [{"style": item["style"], **item["quality"],
                          "coordinates": [[x, y] for x, y in item["points"]]}
                         for item in variants],
            "winner": {"style": winner["style"], **winner["quality"]},
            "winning_grid_rejections": winner["detail"].get(
                "winning_grid_rejections", []),
            "audit_scope": "whole schemes generated inside the safe planting scope",
        })
    return winner["points"], winner["style"]


def choose_shrub_composition(
    scope: Any, reference: Any, profile: Any, profiles: dict[str, Any],
    occupied: list[tuple[str, float, float]], maximum: int,
    *, trace: dict[str, Any] | None = None,
) -> tuple[list[tuple[float, float]], str]:
    """Keep near-maximum shrub coverage and prefer connected planting beds."""
    if maximum <= 0 or scope.is_empty:
        if trace is not None:
            trace.update({"method": "shrub_composition", "variants": [],
                          "winner": None, "winning_grid_rejections": []})
        return [], "shrub_composition"
    candidates = []
    for style, generate in (
        ("shrub_bed_rows", lambda detail: best_linear_layout(
            scope, reference, profile, profiles, occupied, maximum, trace=detail)),
        ("shrub_bed_grid", lambda detail: best_component_layout(
            scope, profile, profiles, occupied, maximum, trace=detail)),
    ):
        detail: dict[str, Any] = {}
        raw_points = generate(detail) or []
        points, omitted = prune_shrub_fragments(raw_points, profile.spacing_m)
        if not points:
            continue
        geometries = [Point(x, y) for x, y in points]
        index = STRtree(geometries)
        isolated = 0
        adjacent_pairs = 0
        for left, point in enumerate(geometries):
            neighbours = 0
            for raw_right in index.query(point.buffer(1.5 * profile.spacing_m).envelope):
                right = int(raw_right)
                if right != left and point.distance(geometries[right]) <= 1.5 * profile.spacing_m:
                    neighbours += 1
                    adjacent_pairs += right > left
            isolated += neighbours == 0
        elongated = linear_reference(reference, profile.spacing_m) is not None
        fit = (2.0 if elongated else -1.0) if style == "shrub_bed_rows" else (
            1.0 if not elongated else 0.0
        )
        score = (len(points) + 0.12 * min(adjacent_pairs, 2 * len(points))
                 - 2.0 * isolated + fit)
        candidates.append({
            "style": style, "points": points, "detail": detail,
            "score": round(score, 4), "count": len(points),
            "adjacent_pairs": adjacent_pairs, "isolated": isolated,
            "site_fit": fit, "omitted_fragments": omitted,
        })
    if not candidates:
        if trace is not None:
            trace.update({"method": "shrub_composition", "variants": [],
                          "winner": None, "winning_grid_rejections": []})
        return [], "shrub_composition"
    best_count = max(item["count"] for item in candidates)
    eligible = [item for item in candidates if item["count"] >= math.ceil(.9 * best_count)]
    winner = max(eligible, key=lambda item: (item["score"], item["count"], item["style"]))
    if trace is not None:
        trace.update({
            "method": "shrub_composition",
            "objective": "near-maximum coverage, connected points and fit to bed form",
            "score_weights": {"shrub": 1.0, "adjacent_pair_capped": 0.12,
                              "isolated_shrub": -2.0},
            "minimum_count_retention_ratio": 0.9,
            "variants": [{**{key: item[key] for key in
                             ("style", "score", "count", "adjacent_pairs", "isolated", "site_fit")},
                          "coordinates": [[x, y] for x, y in item["points"]]}
                         for item in candidates],
            "winner": {key: winner[key] for key in
                       ("style", "score", "count", "adjacent_pairs", "isolated", "site_fit")},
            "winning_grid_rejections": winner["detail"].get("winning_grid_rejections", []),
            "omitted_short_fragments": winner["omitted_fragments"],
            "audit_scope": "whole point layouts inside one safe planting bed",
        })
    return winner["points"], winner["style"]


def prune_shrub_fragments(
    points: list[tuple[float, float]], spacing: float,
) -> tuple[list[tuple[float, float]], list[list[float]]]:
    """Drop stray one-to-three-shrub tails only when a real mass is present."""
    if len(points) < 8:
        return points, []
    geometries = [Point(x, y) for x, y in points]
    index = STRtree(geometries)
    parent = list(range(len(points)))

    def root(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for left, point in enumerate(geometries):
        for raw_right in index.query(point.buffer(1.55 * spacing).envelope):
            right = int(raw_right)
            if right > left and point.distance(geometries[right]) <= 1.55 * spacing:
                parent[root(right)] = root(left)
    sizes: dict[int, int] = {}
    for item in range(len(points)):
        group = root(item)
        sizes[group] = sizes.get(group, 0) + 1
    if max(sizes.values()) < 5:
        return points, []
    retained = [point for item, point in enumerate(points) if sizes[root(item)] >= 4]
    omitted = [[x, y] for item, (x, y) in enumerate(points) if sizes[root(item)] < 4]
    return retained, omitted


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
