"""Prepare a compact, auditable scene description for Blender.

This module deliberately has no Blender dependency.  It clips a long street to
one representative fragment, recentres local DXF coordinates, triangulates the
visible surfaces and calculates reproducible camera positions.  Blender only
consumes the resulting JSON and cannot move calculated plantings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch
from shapely.geometry import GeometryCollection, LineString, MultiPoint, Point, Polygon, box, shape
from shapely import constrained_delaunay_triangles
from shapely.ops import unary_union
from shapely.validation import make_valid


SCENE_VERSION = 3

PLANT_MASK_COLORS = {
    "tree": ("#00BFFF", "#2979FF", "#00E5FF", "#7C4DFF"),
    "shrub": ("#FF2D55", "#FF8A00", "#E040FB", "#FF1744"),
    "herbaceous": ("#A4F500", "#73D13D", "#C6FF00", "#50E3C2"),
}


def plant_mask_legend(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign reproducible, distinct colors to the proposed species."""
    names = sorted({(item["plant_type"], str(item["properties"].get("species") or "").strip())
                    for item in plan if item["plant_type"] in PLANT_MASK_COLORS})
    counters: dict[str, int] = defaultdict(int)
    result = []
    for plant_type, species in names:
        colors = PLANT_MASK_COLORS[plant_type]
        index = counters[plant_type]
        counters[plant_type] += 1
        if index >= len(colors):
            raise ValueError(f"Too many {plant_type} species for unambiguous color mask")
        result.append({"plant_type": plant_type, "species": species or f"unspecified {plant_type}",
                       "color": colors[index]})
    return result


def polygon_parts(geometry: Any) -> Iterator[Polygon]:
    if geometry is None or geometry.is_empty:
        return
    geometry = make_valid(geometry)
    if isinstance(geometry, Polygon):
        yield geometry
    elif geometry.geom_type == "MultiPolygon":
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def load_grouped(path: Path) -> dict[str, Any]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            object_type = feature.get("properties", {}).get("object_type")
            geometry_data = feature.get("geometry")
            if not object_type or not geometry_data:
                continue
            try:
                grouped[str(object_type)].append(make_valid(shape(geometry_data)))
            except (TypeError, ValueError, KeyError) as error:
                raise ValueError(f"{path}, line {line_number}: invalid geometry") from error
    return {key: unary_union(items) for key, items in grouped.items() if items}


def load_planting_plan(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            try:
                geometry = make_valid(shape(feature["geometry"]))
                properties = feature["properties"]
                plant_type = str(properties["plant_type"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path}, line {line_number}: invalid planting") from error
            result.append({"geometry": geometry, "properties": properties, "plant_type": plant_type})
    if not result:
        raise ValueError(f"No planting features found in {path}")
    return result


def point_parts(geometry: Any) -> list[Point]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Point):
        return [geometry]
    if isinstance(geometry, MultiPoint):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [point for part in geometry.geoms for point in point_parts(part)]
    return []


def choose_focus(plan: list[dict[str, Any]], radius: float) -> Point:
    trees = [item["geometry"] for item in plan if item["plant_type"] == "tree"]
    tree_points = [item for item in trees if isinstance(item, Point)]
    if tree_points:
        best = max(
            tree_points,
            key=lambda candidate: sum(candidate.distance(other) <= radius for other in tree_points),
        )
        nearby = [item for item in tree_points if best.distance(item) <= radius]
        return MultiPoint(nearby).centroid
    areas = [item["geometry"] for item in plan if item["geometry"].geom_type in {"Polygon", "MultiPolygon"}]
    if not areas:
        raise ValueError("Planting plan has neither points nor polygonal areas")
    largest = max((part for area in areas for part in polygon_parts(area)), key=lambda item: item.area)
    return largest.representative_point()


def choose_focuses(plan: list[dict[str, Any]], radius: float, count: int) -> list[Point]:
    """Pick planted, spatially separated places for matched before/after views."""
    if count < 1:
        raise ValueError("At least one focus place is required")
    trees = [item["geometry"] for item in plan
             if item["plant_type"] == "tree" and isinstance(item["geometry"], Point)]
    shrubs = [item["geometry"] for item in plan
              if item["plant_type"] == "shrub" and isinstance(item["geometry"], Point)]
    candidates = trees or shrubs[::max(1, len(shrubs) // 150)]
    if not candidates:
        return [choose_focus(plan, radius)]

    def score(candidate: Point) -> float:
        tree_count = sum(abs(point.x - candidate.x) <= radius and
                         abs(point.y - candidate.y) <= radius for point in trees)
        shrub_count = sum(abs(point.x - candidate.x) <= radius and
                          abs(point.y - candidate.y) <= radius for point in shrubs)
        return tree_count * 20.0 + min(shrub_count, 500) * 0.08

    ranked = sorted(candidates, key=lambda point: (-score(point), point.x, point.y))
    chosen: list[Point] = []
    for separation in (radius * 2.2, radius * 1.6):
        for candidate in ranked:
            if all(candidate.distance(other) >= separation for other in chosen):
                chosen.append(candidate)
                if len(chosen) >= count:
                    return chosen
    return chosen


def principal_axis(geometry: Any, fallback: Iterable[Any]) -> tuple[float, float]:
    coordinates: list[tuple[float, float]] = []
    if geometry is not None and not geometry.is_empty:
        for polygon in polygon_parts(geometry):
            coordinates.extend((float(x), float(y)) for x, y, *_ in polygon.exterior.coords)
    if len(coordinates) < 3:
        for item in fallback:
            if isinstance(item, Point):
                coordinates.append((float(item.x), float(item.y)))
            elif not item.is_empty:
                coordinates.append((float(item.centroid.x), float(item.centroid.y)))
    if len(coordinates) < 2:
        return (0.0, 1.0)
    values = np.asarray(coordinates, dtype=float)
    values -= values.mean(axis=0)
    covariance = np.cov(values, rowvar=False)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    axis = eigenvectors[:, int(np.argmax(eigenvalues))]
    x, y = float(axis[0]), float(axis[1])
    if y < 0 or (abs(y) < 1e-9 and x < 0):
        x, y = -x, -y
    length = math.hypot(x, y) or 1.0
    return (x / length, y / length)


def local_xy(x: float, y: float, origin: Point) -> list[float]:
    return [float(x - origin.x), float(y - origin.y)]


def triangle_records(geometry: Any, origin: Point, z: float) -> list[list[list[float]]]:
    result: list[list[list[float]]] = []
    for polygon in polygon_parts(geometry):
        # Constrained triangulation respects concave edges and interior holes.
        # Centroid filtering of an unconstrained triangulation can draw across
        # a road/building cut-out even when the triangle centre is inside.
        for triangle in constrained_delaunay_triangles(polygon).geoms:
            coords = list(triangle.exterior.coords)[:3]
            result.append([[x - origin.x, y - origin.y, z] for x, y, *_ in coords])
    return result


def polygon_records(geometry: Any, origin: Point, height: float) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for polygon in polygon_parts(geometry):
        records.append(
            {
                "exterior": [local_xy(x, y, origin) for x, y, *_ in polygon.exterior.coords],
                "height": 3.0 if polygon.area < 100.0 else height,
                "height_source": "illustrative_small_footprint" if polygon.area < 100.0 else "illustrative_default",
            }
        )
    return records


def context_buildings(normalized: dict[str, Any], constraints: dict[str, Any],
                      clip: Polygon) -> tuple[Any, dict[str, Any]]:
    """Select whole nearby footprints; a work-boundary intersection is not a building."""
    source = normalized.get("building")
    source_name = "normalized_building"
    if source is None or source.is_empty:
        source = constraints.get("buildings_in_work_area")
        source_name = "buildings_in_work_area_fallback"
    selected = []
    rejected = 0
    for polygon in polygon_parts(source):
        if not polygon.intersects(clip):
            continue
        # Avoid extruding numerical slivers into tall facade panels. These
        # thresholds only affect the illustration, never planting constraints.
        if polygon.area < 1.0 or 2.0 * polygon.area / max(polygon.length, 1e-9) < 0.10:
            rejected += 1
            continue
        selected.append(polygon)
    return unary_union(selected), {
        "source": source_name, "whole_footprints": len(selected),
        "omitted_degenerate_footprints": rejected,
        "height_m": 12.0, "small_footprint_height_m": 3.0,
        "small_footprint_max_area_m2": 100.0,
        "height_source": "illustrative_default",
    }


def deterministic_fraction(*values: Any) -> float:
    digest = hashlib.sha256("|".join(map(str, values)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def scatter_points(geometry: Any, spacing: float, limit: int = 900) -> list[Point]:
    if geometry is None or geometry.is_empty or spacing <= 0:
        return []
    min_x, min_y, max_x, max_y = geometry.bounds
    row_step = spacing * math.sqrt(3.0) / 2.0
    result: list[Point] = []
    row = 0
    y = min_y + row_step * 0.5
    while y <= max_y and len(result) < limit:
        x = min_x + spacing * (0.5 if row % 2 else 0.0)
        while x <= max_x and len(result) < limit:
            point = Point(x, y)
            if geometry.covers(point):
                result.append(point)
            x += spacing
        y += row_step
        row += 1
    return result


def camera_records(axis: tuple[float, float], radius: float) -> list[dict[str, Any]]:
    ux, uy = axis
    vx, vy = -uy, ux
    target = [0.0, 0.0, 2.0]
    return [
        {
            "name": "overview",
            "kind": "perspective",
            "position": [-ux * radius * 0.70 + vx * radius * 0.80,
                         -uy * radius * 0.70 + vy * radius * 0.80,
                         radius * 0.72],
            "target": target,
            "lens_mm": 42.0,
        },
        {
            "name": "pedestrian",
            "kind": "perspective",
            "position": [-ux * radius * 0.70 + vx * radius * 0.28,
                         -uy * radius * 0.70 + vy * radius * 0.28,
                         1.7],
            "target": [ux * radius * 0.42, uy * radius * 0.42, 2.4],
            "lens_mm": 32.0,
        },
        {
            "name": "top",
            "kind": "orthographic",
            "position": [0.0, 0.0, radius * 2.2],
            "target": [0.0, 0.0, 0.0],
            "ortho_scale": radius * 2.15,
        },
    ]


def place_pedestrian_camera(cameras: list[dict[str, Any]], focus: Point,
                            road: Any, buildings: Any, radius: float,
                            existing_trees: Iterable[Point] = (),
                            proposed_trees: Iterable[dict[str, Any]] = ()) -> None:
    """Move the eye onto a road with an unobstructed view of the planting focus."""
    if road.is_empty:
        next(item for item in cameras if item["name"] == "pedestrian")["placement"] = "unverified_no_road"
        return
    camera = next(item for item in cameras if item["name"] == "pedestrian")
    camera["placement"] = "unverified_no_clear_view"
    desired = Point(focus.x + camera["position"][0],
                    focus.y + camera["position"][1])
    safe_road = road.buffer(-0.75)
    if safe_road.is_empty:
        safe_road = road
    building_clearance = buildings.buffer(0.5) if not buildings.is_empty else buildings
    tree_points = list(existing_trees)
    proposed = [(Point(focus.x + tree["position"][0], focus.y + tree["position"][1]),
                 float(tree["crown_radius"])) for tree in proposed_trees]
    target = MultiPoint([point for point, _ in proposed]).centroid if proposed else focus
    step = radius / 10.0
    candidates: list[tuple[float, Point]] = []
    for ix in range(-9, 10):
        for iy in range(-9, 10):
            point = Point(focus.x + ix * step, focus.y + iy * step)
            distance = point.distance(focus)
            if not radius * 0.3 <= distance <= radius * 0.95:
                continue
            if not safe_road.covers(point):
                continue
            if any(point.distance(tree) < 4.0 for tree in tree_points):
                continue
            sightline = LineString([point, target])
            if not building_clearance.is_empty and sightline.intersects(building_clearance):
                continue
            occluding_trees = sum(sightline.distance(tree) < 2.5
                                  for tree in tree_points if tree.distance(focus) > 3.0)
            # A view along the row hides the second new tree behind the first.
            # Score projected crown overlap, not just the distance to the eye.
            crowns = [(math.atan2(tree.y - point.y, tree.x - point.x),
                       math.atan2(crown_radius, max(point.distance(tree), 0.01)))
                      for tree, crown_radius in proposed]
            overlap = 0.0
            for index, (angle, width) in enumerate(crowns):
                for other_angle, other_width in crowns[index + 1:]:
                    separation = abs(math.atan2(math.sin(angle - other_angle),
                                                math.cos(angle - other_angle)))
                    overlap += max(0.0, 1.0 - separation / (width + other_width))
            score = (0.25 * point.distance(desired) + 0.35 * abs(distance - radius * 0.60)
                     + occluding_trees * radius * 0.8 + overlap * radius * 3.0)
            candidates.append((score, point))
    if not candidates:
        return
    point = min(candidates, key=lambda item: item[0])[1]
    camera["position"] = [point.x - focus.x, point.y - focus.y, 1.7]
    camera["target"] = [target.x - focus.x, target.y - focus.y, 2.0]
    camera["lens_mm"] = 40.0
    camera["placement"] = "road_with_clear_view"


def frame_overview_camera(cameras: list[dict[str, Any]], focus: Point,
                          base: Any, road: Any, radius: float,
                          proposed_trees: list[dict[str, Any]]) -> None:
    """Frame the planted bed from its road side, with space above the crowns."""
    parts = list(polygon_parts(base))
    if not parts:
        return
    bed = min(parts, key=lambda polygon: polygon.distance(focus))
    centre = bed.centroid
    ux, uy = principal_axis(bed, [])
    nx, ny = -uy, ux
    candidates = [Point(centre.x - ux * radius * 0.25 + sign * nx * radius * 0.85,
                        centre.y - uy * radius * 0.25 + sign * ny * radius * 0.85)
                  for sign in (-1, 1)]
    eye = max(candidates, key=lambda point: road.intersection(point.buffer(radius * 0.30)).area)
    target = np.array([centre.x - focus.x, centre.y - focus.y, 1.8])
    position = np.array([eye.x - focus.x, eye.y - focus.y, radius * 0.65])
    forward = target - position
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    # Fit the full bed and the crowns above it into a 16:9 sensor, including
    # depth perspective and a margin, rather than using one fixed focal length.
    height = max([tree["height"] for tree in proposed_trees] + [2.0]) + 1.0
    corners = [np.array([x - focus.x, y - focus.y, z])
               for x, y, *_ in bed.minimum_rotated_rectangle.exterior.coords
               for z in (0.0, height)]
    widths = [abs(np.dot(corner - position, right)) / max(np.dot(corner - position, forward), 0.1)
              for corner in corners]
    heights = [abs(np.dot(corner - position, up)) / max(np.dot(corner - position, forward), 0.1)
               for corner in corners]
    lens = min(36.0 / (2 * max(max(widths), 0.01)),
               20.25 / (2 * max(max(heights), 0.01))) / 1.15
    camera = next(item for item in cameras if item["name"] == "overview")
    camera.update(position=position.tolist(), target=target.tolist(),
                  lens_mm=min(55.0, lens), placement="road_side_bed_frame")


def build_manifest(
    normalized_path: Path,
    constraints_path: Path,
    planting_plan_path: Path,
    output_path: Path,
    preview_path: Path | None,
    focus_radius_m: float = 60.0,
    focus_x: float | None = None,
    focus_y: float | None = None,
) -> dict[str, Any]:
    normalized = load_grouped(normalized_path)
    constraints = load_grouped(constraints_path)
    plan = load_planting_plan(planting_plan_path)
    focus = Point(focus_x, focus_y) if focus_x is not None and focus_y is not None else choose_focus(plan, focus_radius_m)
    clip = box(focus.x - focus_radius_m, focus.y - focus_radius_m,
               focus.x + focus_radius_m, focus.y + focus_radius_m)
    mask_legend = plant_mask_legend([item for item in plan if item["geometry"].intersects(clip)])
    mask_colors = {(item["plant_type"], item["species"]): item["color"] for item in mask_legend}

    def clipped(value: Any | None) -> Any:
        if value is None or value.is_empty:
            return GeometryCollection()
        return make_valid(value.intersection(clip))

    road = clipped(constraints.get("road_area"))
    sidewalk = clipped(constraints.get("sidewalk_area"))
    buildings, building_context = context_buildings(normalized, constraints, clip)
    hard = clipped(constraints.get("hard_surface_area"))
    wells = clipped(constraints.get("utility_well_footprints"))
    base = clipped(constraints.get("base_allowed_area"))
    existing_tree_points = [point for point in point_parts(normalized.get("existing_tree")) if clip.covers(point)]
    existing_belts = clipped(normalized.get("existing_tree_belt"))

    plan_areas: dict[str, list[Any]] = defaultdict(list)
    species_areas: dict[tuple[str, str], list[Any]] = defaultdict(list)
    proposed_trees: list[dict[str, Any]] = []
    proposed_shrubs: list[dict[str, Any]] = []
    for item in plan:
        geometry = clipped(item["geometry"])
        if geometry.is_empty:
            continue
        properties = item["properties"]
        species = str(properties.get("species") or f"unspecified {item['plant_type']}").strip()
        if item["plant_type"] == "tree" and isinstance(geometry, Point):
            proposed_trees.append(
                {
                    "id": str(properties.get("planting_id", "tree")),
                    "species": species,
                    "mask_color": mask_colors[("tree", species)],
                    "position": local_xy(geometry.x, geometry.y, focus),
                    "height": 5.5,
                    "crown_radius": float(properties.get("footprint_radius_m")
                                          or properties.get("symbol_radius_m", 2.5)),
                }
            )
        elif item["plant_type"] == "shrub" and isinstance(geometry, Point):
            proposed_shrubs.append(
                {
                    "id": str(properties.get("planting_id", "shrub")),
                    "species": species,
                    "mask_color": mask_colors[("shrub", species)],
                    "position": local_xy(geometry.x, geometry.y, focus),
                    "height": 0.75 + 0.25 * deterministic_fraction(geometry.x, geometry.y),
                    # The CAD symbol is smaller than the validated planting footprint.
                    # Use the latter for a visually continuous mature shrub mass.
                    "radius": max(0.3, float(properties.get("footprint_radius_m")
                                              or properties.get("symbol_radius_m", 0.5))),
                }
            )
        elif geometry.geom_type in {"Polygon", "MultiPolygon", "GeometryCollection"}:
            plan_areas[item["plant_type"]].append(geometry)
            species_areas[(item["plant_type"], species)].append(geometry)

    shrub_area = unary_union(plan_areas.get("shrub", [])) if plan_areas.get("shrub") else GeometryCollection()
    # Older area-style planting files can still supply a shrub bed.  Current
    # pipeline plans individual shrub points; preserve those exact positions.
    shrub_points = scatter_points(shrub_area, 1.45, limit=1000) if not proposed_shrubs else []
    fallback_shrub = next((item for item in mask_legend if item["plant_type"] == "shrub"), None)
    existing_belt_points = scatter_points(existing_belts, 2.8, limit=350)
    axis = principal_axis(road, [item["geometry"] for item in plan])

    surfaces = []
    for name, geometry, material, z in (
        ("hard_surface", hard, "concrete", 0.015),
        ("road", road, "asphalt", 0.025),
        ("sidewalk", sidewalk, "paving", 0.085),
        ("base_ground", base, "soil_grass", 0.095),
        ("utility_wells", wells, "metal", 0.14),
    ):
        triangles = triangle_records(geometry, focus, z)
        if triangles:
            surfaces.append({"name": name, "material": material, "triangles": triangles,
                             "proposed": name in {"herbaceous", "shrub_bed"}})
    for (plant_type, species), areas in species_areas.items():
        if plant_type not in {"herbaceous", "shrub"}:
            continue
        triangles = triangle_records(unary_union(areas), focus,
                                     0.115 if plant_type == "herbaceous" else 0.125)
        if triangles:
            surfaces.append({"name": "herbaceous" if plant_type == "herbaceous" else "shrub_bed",
                             "material": "lawn" if plant_type == "herbaceous" else "mulch",
                             "species": species, "mask_color": mask_colors[(plant_type, species)],
                             "triangles": triangles, "proposed": True})

    cameras = camera_records(axis, focus_radius_m)
    frame_overview_camera(cameras, focus, base, road, focus_radius_m, proposed_trees)
    place_pedestrian_camera(cameras, focus, road, buildings, focus_radius_m,
                            existing_tree_points, proposed_trees)
    visible_species = {("tree", item["species"]) for item in proposed_trees}
    visible_species.update(("shrub", item["species"]) for item in proposed_shrubs)
    if shrub_points and fallback_shrub:
        visible_species.add(("shrub", fallback_shrub["species"]))
    visible_species.update(key for key in species_areas if key[0] in {"shrub", "herbaceous"})
    manifest = {
        "version": SCENE_VERSION,
        "coordinate_reference": "local_dxf_coordinates_recentered",
        "source_origin": {"x": focus.x, "y": focus.y},
        "focus_radius_m": focus_radius_m,
        "axis": list(axis),
        "sources": {
            "normalized_objects": str(normalized_path),
            "constraint_map": str(constraints_path),
            "planting_plan": str(planting_plan_path),
        },
        "plant_mask_legend": [item for item in mask_legend
                              if (item["plant_type"], item["species"]) in visible_species],
        "surfaces": surfaces,
        "buildings": polygon_records(buildings, focus, 12.0),
        "building_context": building_context,
        "proposed_trees": proposed_trees,
        "existing_trees": [
            {
                "id": f"existing-{index:04d}",
                "position": local_xy(point.x, point.y, focus),
                "height": 6.5 + deterministic_fraction(point.x, point.y) * 3.0,
                "crown_radius": 2.2 + deterministic_fraction(point.y, point.x) * 1.3,
            }
            for index, point in enumerate(existing_tree_points, start=1)
        ],
        "shrubs": proposed_shrubs + [
            {
                "species": fallback_shrub["species"] if fallback_shrub else "unspecified shrub",
                "mask_color": fallback_shrub["color"] if fallback_shrub else PLANT_MASK_COLORS["shrub"][0],
                "position": local_xy(point.x, point.y, focus),
                "height": 0.65 + deterministic_fraction(point.x, point.y) * 0.35,
                "radius": 0.55 + deterministic_fraction(point.y, point.x) * 0.25,
            }
            for point in shrub_points
        ],
        "existing_belt_shrubs": [
            {
                "position": local_xy(point.x, point.y, focus),
                "height": 0.8 + deterministic_fraction(point.x, point.y) * 0.5,
                "radius": 0.7 + deterministic_fraction(point.y, point.x) * 0.35,
            }
            for point in existing_belt_points
        ],
        "cameras": cameras,
        "render": {"width": 1280, "height": 720, "quality": "draft"},
        "counts": {
            "proposed_trees": len(proposed_trees),
            "existing_trees": len(existing_tree_points),
            "proposed_shrub_instances": len(proposed_shrubs) + len(shrub_points),
            "existing_belt_instances": len(existing_belt_points),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if preview_path is not None:
        render_preview(manifest, preview_path)
    return manifest


def render_preview(manifest: dict[str, Any], output_path: Path) -> None:
    colors = {
        "hard_surface": "#8d8d8d",
        "road": "#353a3d",
        "sidewalk": "#c9c2b5",
        "base_ground": "#75684b",
        "herbaceous": "#68a94a",
        "shrub_bed": "#795c36",
        "utility_wells": "#353535",
    }
    figure, axis = plt.subplots(figsize=(10, 10), dpi=150)
    figure.patch.set_facecolor("#222629")
    axis.set_facecolor("#222629")
    for surface in manifest["surfaces"]:
        for triangle in surface["triangles"]:
            xs = [point[0] for point in triangle]
            ys = [point[1] for point in triangle]
            axis.fill(xs, ys, facecolor=colors[surface["name"]], edgecolor="none",
                      linewidth=0, antialiased=False)
    for building in manifest["buildings"]:
        xs = [point[0] for point in building["exterior"]]
        ys = [point[1] for point in building["exterior"]]
        axis.fill(xs, ys, color="#a98467", edgecolor="#d7b69b", linewidth=0.5)
    for tree in manifest["existing_trees"]:
        circle = plt.Circle(tree["position"], tree["crown_radius"], color="#315f35", alpha=0.82)
        axis.add_patch(circle)
    for tree in manifest["proposed_trees"]:
        circle = plt.Circle(tree["position"], tree["crown_radius"], color="#8ddd55", alpha=0.86)
        axis.add_patch(circle)
    if manifest["shrubs"]:
        axis.scatter([item["position"][0] for item in manifest["shrubs"]],
                     [item["position"][1] for item in manifest["shrubs"]],
                     s=3, c="#4f8f3d", alpha=0.8)
    radius = float(manifest["focus_radius_m"])
    axis.set_xlim(-radius, radius)
    axis.set_ylim(-radius, radius)
    axis.set_aspect("equal", adjustable="box")
    axis.axis("off")
    axis.legend(handles=[
        Patch(color=colors["road"], label="Road"),
        Patch(color=colors["sidewalk"], label="Sidewalk"),
        Patch(color=colors["herbaceous"], label="Proposed lawn"),
        Patch(color=colors["shrub_bed"], label="Proposed shrub bed"),
        Patch(color="#8ddd55", label="Proposed trees"),
    ], loc="upper right", framealpha=0.88)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, bbox_inches="tight", pad_inches=0.05, facecolor=figure.get_facecolor())
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a cropped Sylvitect-core scene for Blender")
    parser.add_argument("normalized_objects", type=Path)
    parser.add_argument("constraint_map", type=Path)
    parser.add_argument("planting_plan", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/visualization/scene.json"))
    parser.add_argument("--preview", type=Path, default=Path("output/visualization/scene_preview.png"))
    parser.add_argument("--focus-radius", type=float, default=60.0)
    parser.add_argument("--focus-x", type=float)
    parser.add_argument("--focus-y", type=float)
    args = parser.parse_args()
    if args.focus_radius <= 5 or not math.isfinite(args.focus_radius):
        raise SystemExit("--focus-radius must be a finite value greater than 5 metres")
    if (args.focus_x is None) != (args.focus_y is None):
        raise SystemExit("--focus-x and --focus-y must be specified together")
    manifest = build_manifest(
        args.normalized_objects,
        args.constraint_map,
        args.planting_plan,
        args.output,
        args.preview,
        args.focus_radius,
        args.focus_x,
        args.focus_y,
    )
    print(f"Scene: {args.output}")
    print(f"Preview: {args.preview}")
    print(f"Origin: {manifest['source_origin']}")
    print("Objects: " + ", ".join(f"{key}={value}" for key, value in manifest["counts"].items()))


if __name__ == "__main__":
    main()
