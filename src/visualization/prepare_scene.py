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
from shapely.geometry import GeometryCollection, MultiPoint, Point, Polygon, box, shape
from shapely import constrained_delaunay_triangles
from shapely.ops import unary_union
from shapely.validation import make_valid


SCENE_VERSION = 1


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
                "height": height,
            }
        )
    return records


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

    def clipped(value: Any | None) -> Any:
        if value is None or value.is_empty:
            return GeometryCollection()
        return make_valid(value.intersection(clip))

    road = clipped(constraints.get("road_area"))
    sidewalk = clipped(constraints.get("sidewalk_area"))
    buildings = clipped(constraints.get("buildings_in_work_area"))
    hard = clipped(constraints.get("hard_surface_area"))
    wells = clipped(constraints.get("utility_well_footprints"))
    base = clipped(constraints.get("base_allowed_area"))
    existing_tree_points = [point for point in point_parts(normalized.get("existing_tree")) if clip.covers(point)]
    existing_belts = clipped(normalized.get("existing_tree_belt"))

    plan_areas: dict[str, list[Any]] = defaultdict(list)
    proposed_trees: list[dict[str, Any]] = []
    proposed_shrubs: list[dict[str, Any]] = []
    for item in plan:
        geometry = clipped(item["geometry"])
        if geometry.is_empty:
            continue
        properties = item["properties"]
        if item["plant_type"] == "tree" and isinstance(geometry, Point):
            proposed_trees.append(
                {
                    "id": str(properties.get("planting_id", "tree")),
                    "position": local_xy(geometry.x, geometry.y, focus),
                    "height": 5.5,
                    "crown_radius": max(1.2, float(properties.get("symbol_radius_m", 2.5))),
                }
            )
        elif item["plant_type"] == "shrub" and isinstance(geometry, Point):
            proposed_shrubs.append(
                {
                    "id": str(properties.get("planting_id", "shrub")),
                    "position": local_xy(geometry.x, geometry.y, focus),
                    "height": 0.75 + 0.25 * deterministic_fraction(geometry.x, geometry.y),
                    "radius": max(0.3, float(properties.get("symbol_radius_m", 0.5))),
                }
            )
        elif geometry.geom_type in {"Polygon", "MultiPolygon", "GeometryCollection"}:
            plan_areas[item["plant_type"]].append(geometry)

    shrub_area = unary_union(plan_areas.get("shrub", [])) if plan_areas.get("shrub") else GeometryCollection()
    herbaceous_area = unary_union(plan_areas.get("herbaceous", [])) if plan_areas.get("herbaceous") else GeometryCollection()
    # Older area-style planting files can still supply a shrub bed.  Current
    # pipeline plans individual shrub points; preserve those exact positions.
    shrub_points = scatter_points(shrub_area, 1.45, limit=1000) if not proposed_shrubs else []
    existing_belt_points = scatter_points(existing_belts, 2.8, limit=350)
    axis = principal_axis(road, [item["geometry"] for item in plan])

    surfaces = []
    for name, geometry, material, z in (
        ("hard_surface", hard, "concrete", 0.015),
        ("road", road, "asphalt", 0.025),
        ("sidewalk", sidewalk, "paving", 0.085),
        ("base_ground", base, "soil_grass", 0.095),
        ("herbaceous", herbaceous_area, "lawn", 0.115),
        ("shrub_bed", shrub_area, "mulch", 0.125),
        ("utility_wells", wells, "metal", 0.14),
    ):
        triangles = triangle_records(geometry, focus, z)
        if triangles:
            surfaces.append({"name": name, "material": material, "triangles": triangles,
                             "proposed": name in {"herbaceous", "shrub_bed"}})

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
        "surfaces": surfaces,
        "buildings": polygon_records(buildings, focus, 12.0),
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
        "cameras": camera_records(axis, focus_radius_m),
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
    parser = argparse.ArgumentParser(description="Prepare a cropped GreenAI scene for Blender")
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
