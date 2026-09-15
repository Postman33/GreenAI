"""Export per-plant allow zones to a diagnostic PNG and DXF."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.plotting import plot_line, plot_polygon
from shapely.validation import make_valid

from constraint_builder import as_polygonal, read_object_geometry
from plant_allow_zone import load_normalized_objects


Polygonal = Polygon | MultiPolygon
Lineal = LineString | MultiLineString


DEBUG_CONTEXT_LAYERS = {
    "building": ("DEBUG_BUILDINGS", 8),
    "existing_tree": ("DEBUG_EXISTING_TREES", 94),
    "existing_tree_belt": ("DEBUG_EXISTING_TREE_BELTS", 92),
    "vegetation_boundary": ("DEBUG_VEGETATION", 82),
    "water_pipe": ("DEBUG_WATER_PIPE", 5),
    "storm_drain": ("DEBUG_STORM_DRAIN", 4),
    "gas_pipe": ("DEBUG_GAS_PIPE", 2),
    "heat_pipe": ("DEBUG_HEAT_PIPE", 1),
    "sewer_pipe": ("DEBUG_SEWER_PIPE", 6),
    "power_cable": ("DEBUG_POWER_CABLE", 30),
    "telecom_cable": ("DEBUG_TELECOM_CABLE", 3),
    "overhead_power_line": ("DEBUG_OVERHEAD_POWER", 7),
    "utility_marker": ("DEBUG_UTILITY_MARKERS", 200),
    "utility_well": ("DEBUG_UTILITY_WELLS", 210),
}


def polygon_parts(geometry: Any) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def line_parts(geometry: Any) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from line_parts(part)


def point_parts(geometry: Any) -> Iterable[Point]:
    if isinstance(geometry, Point):
        yield geometry
    elif isinstance(geometry, MultiPoint):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from point_parts(part)


def load_plant_zones(path: Path) -> dict[str, Polygonal]:
    zones: dict[str, list[Polygon]] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            properties = feature.get("properties", {})
            if properties.get("object_type") != "plant_allow_zone":
                continue
            plant_type = properties.get("plant_type")
            if not plant_type:
                raise ValueError(f"Line {line_number}: plant_type is missing")
            geometry = as_polygonal(make_valid(shape(feature["geometry"])))
            zones.setdefault(plant_type, []).extend(polygon_parts(geometry))
    if not zones:
        raise ValueError("No plant_allow_zone features were found")
    return {
        plant_type: as_polygonal(MultiPolygon(parts))
        for plant_type, parts in zones.items()
    }


def add_polygons(modelspace, geometry: Polygonal, layer: str) -> None:
    for polygon in polygon_parts(geometry):
        modelspace.add_lwpolyline(
            list(polygon.exterior.coords), close=True, dxfattribs={"layer": layer}
        )
        for interior in polygon.interiors:
            modelspace.add_lwpolyline(
                list(interior.coords), close=True, dxfattribs={"layer": layer}
            )


def add_lines(modelspace, geometry: Lineal, layer: str) -> None:
    for line in line_parts(geometry):
        coordinates = list(line.coords)
        if len(coordinates) >= 2:
            modelspace.add_lwpolyline(coordinates, dxfattribs={"layer": layer})


def add_points(modelspace, geometry: Any, layer: str, radius: float = 0.35) -> None:
    for point in point_parts(geometry):
        modelspace.add_circle(
            (point.x, point.y),
            radius=radius,
            dxfattribs={"layer": layer},
        )


def add_context_geometry(modelspace, geometry: Any, layer: str) -> None:
    add_polygons(modelspace, geometry, layer)
    add_lines(modelspace, geometry, layer)
    add_points(modelspace, geometry, layer)


def export_dxf(
    output_path: Path,
    work_boundary: Polygonal,
    hard_surfaces: Polygonal,
    road_area: Polygonal,
    sidewalks: Polygonal,
    road_edges: Lineal,
    context_geometries: dict[str, Any],
    zones: dict[str, Polygonal],
) -> None:
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 0
    layer_colors = {
        "DEBUG_WORK_BOUNDARY": 5,
        "DEBUG_HARD_SURFACES": 1,
        "DEBUG_ROAD_AREA": 8,
        "DEBUG_ROAD_EDGES": 7,
        "DEBUG_SIDEWALKS": 4,
        "DEBUG_ALLOW_TREE": 3,
        "DEBUG_ALLOW_SHRUB": 2,
        "DEBUG_ALLOW_HERBACEOUS": 4,
        "DEBUG_ALLOW_GROUNDCOVER": 6,
    }
    for layer, color in layer_colors.items():
        document.layers.add(layer, color=color)
    for layer, color in DEBUG_CONTEXT_LAYERS.values():
        document.layers.add(layer, color=color)

    modelspace = document.modelspace()
    add_polygons(modelspace, work_boundary, "DEBUG_WORK_BOUNDARY")
    add_polygons(modelspace, road_area, "DEBUG_ROAD_AREA")
    add_polygons(modelspace, hard_surfaces, "DEBUG_HARD_SURFACES")
    add_polygons(modelspace, sidewalks, "DEBUG_SIDEWALKS")
    add_lines(modelspace, road_edges, "DEBUG_ROAD_EDGES")
    for object_type, geometry in context_geometries.items():
        layer, _ = DEBUG_CONTEXT_LAYERS[object_type]
        add_context_geometry(modelspace, geometry, layer)
    for plant_type, geometry in zones.items():
        layer = f"DEBUG_ALLOW_{plant_type.upper()}"
        if layer not in document.layers:
            document.layers.add(layer, color=3)
        add_polygons(modelspace, geometry, layer)
    document.saveas(output_path)


def draw_context(
    axis,
    work_boundary: Polygonal,
    hard_surfaces: Polygonal,
    road_area: Polygonal,
    sidewalks: Polygonal,
    road_edges: Lineal,
) -> None:
    if not road_area.is_empty:
        plot_polygon(
            road_area,
            axis,
            add_points=False,
            facecolor="#757575",
            edgecolor="#424242",
            linewidth=0.35,
            alpha=0.72,
            zorder=2,
        )
    if not hard_surfaces.is_empty:
        plot_polygon(
            hard_surfaces,
            axis,
            add_points=False,
            facecolor="#EF5350",
            edgecolor="#B71C1C",
            linewidth=0.35,
            alpha=0.70,
            zorder=3,
        )
    if not sidewalks.is_empty:
        plot_polygon(
            sidewalks,
            axis,
            add_points=False,
            facecolor="none",
            edgecolor="#00BCD4",
            linewidth=0.8,
            alpha=0.95,
            zorder=4,
        )
    if not road_edges.is_empty:
        plot_line(
            road_edges,
            axis,
            add_points=False,
            color="#212121",
            linewidth=0.35,
            alpha=0.85,
            zorder=5,
        )
    plot_polygon(
        work_boundary,
        axis,
        add_points=False,
        facecolor="none",
        edgecolor="#1565C0",
        linewidth=1.5,
        zorder=6,
    )


def export_png(
    output_path: Path,
    work_boundary: Polygonal,
    hard_surfaces: Polygonal,
    road_area: Polygonal,
    sidewalks: Polygonal,
    road_edges: Lineal,
    zones: dict[str, Polygonal],
    dpi: int,
) -> None:
    ordered_types = [item for item in ("tree", "shrub") if item in zones]
    ordered_types.extend(sorted(set(zones) - set(ordered_types)))
    figure, axes = plt.subplots(
        1,
        len(ordered_types),
        figsize=(8 * len(ordered_types), 12),
        squeeze=False,
        sharex=True,
        sharey=True,
    )
    colors = {
        "tree": ("#66BB6A", "#1B5E20"),
        "shrub": ("#FFCA28", "#E65100"),
        "herbaceous": ("#42A5F5", "#0D47A1"),
        "groundcover": ("#AB47BC", "#4A148C"),
    }
    for axis, plant_type in zip(axes[0], ordered_types):
        fill, edge = colors.get(plant_type, ("#66BB6A", "#1B5E20"))
        plot_polygon(
            zones[plant_type],
            axis,
            add_points=False,
            facecolor=fill,
            edgecolor=edge,
            linewidth=0.45,
            alpha=0.58,
            zorder=1,
        )
        draw_context(
            axis,
            work_boundary,
            hard_surfaces,
            road_area,
            sidewalks,
            road_edges,
        )
        axis.set_aspect("equal", adjustable="box")
        axis.set_title(f"Allow zone: {plant_type}")
        axis.set_xlabel("DXF X coordinate")
        axis.grid(True, linewidth=0.25, alpha=0.3)
    axes[0][0].set_ylabel("DXF Y coordinate")
    figure.legend(
        handles=[
            Patch(facecolor="#66BB6A", edgecolor="#1B5E20", alpha=0.58, label="Tree allow zone"),
            Patch(facecolor="#FFCA28", edgecolor="#E65100", alpha=0.58, label="Shrub allow zone"),
            Patch(facecolor="#EF5350", edgecolor="#B71C1C", alpha=0.70, label="Hard surfaces"),
            Patch(facecolor="#757575", edgecolor="#424242", alpha=0.72, label="Reconstructed road"),
            Patch(facecolor="none", edgecolor="#00BCD4", label="Sidewalks"),
            Line2D([0], [0], color="#212121", linewidth=1, label="Road edges"),
            Line2D([0], [0], color="#1565C0", linewidth=2, label="Work boundary"),
        ],
        loc="upper center",
        ncol=3,
    )
    figure.suptitle("Plant allow zones and road geometry", y=0.98)
    figure.tight_layout(rect=(0, 0, 1, 0.94))
    figure.savefig(output_path, dpi=dpi, bbox_inches="tight")
    plt.close(figure)


def build_debug_export(
    plant_zones_path: Path,
    constraint_map_path: Path,
    normalized_path: Path,
    dxf_output: Path,
    png_output: Path,
    dpi: int,
) -> None:
    zones = load_plant_zones(plant_zones_path)
    normalized_objects = load_normalized_objects(normalized_path)
    work_boundary_geometry = normalized_objects.get("work_boundary")
    if work_boundary_geometry is None or work_boundary_geometry.is_empty:
        raise ValueError("No work_boundary geometry found in normalized input")
    work_boundary = as_polygonal(work_boundary_geometry)
    hard_surfaces = as_polygonal(
        read_object_geometry(constraint_map_path, "hard_surface_area")
    )
    road_area = as_polygonal(
        read_object_geometry(constraint_map_path, "road_area")
    )
    sidewalk_geometry = normalized_objects.get("sidewalk")
    sidewalks = (
        as_polygonal(sidewalk_geometry.intersection(work_boundary))
        if sidewalk_geometry is not None and not sidewalk_geometry.is_empty
        else Polygon()
    )
    context_clip = work_boundary.buffer(2.0)
    road_edge_geometry = normalized_objects.get("road_edge")
    road_edges = (
        road_edge_geometry.intersection(context_clip)
        if road_edge_geometry is not None and not road_edge_geometry.is_empty
        else MultiLineString([])
    )
    context_geometries = {
        object_type: geometry.intersection(context_clip)
        for object_type in DEBUG_CONTEXT_LAYERS
        if (geometry := normalized_objects.get(object_type)) is not None
        and not geometry.is_empty
    }

    export_dxf(
        dxf_output,
        work_boundary,
        hard_surfaces,
        road_area,
        sidewalks,
        road_edges,
        context_geometries,
        zones,
    )
    export_png(
        png_output,
        work_boundary,
        hard_surfaces,
        road_area,
        sidewalks,
        road_edges,
        zones,
        dpi,
    )

    print(f"Plant zones: {plant_zones_path}")
    print(f"DXF: {dxf_output}")
    print(f"PNG: {png_output}")
    print(f"Reconstructed road: {road_area.area:.3f} square DXF units")
    print(f"Sidewalks: {sidewalks.area:.3f} square DXF units")
    for object_type, geometry in context_geometries.items():
        layer, _ = DEBUG_CONTEXT_LAYERS[object_type]
        part_count = len(geometry.geoms) if hasattr(geometry, "geoms") else 1
        print(f"  {object_type}: {layer} | {part_count} part(s)")
    for plant_type, geometry in sorted(zones.items()):
        print(f"  {plant_type}: {geometry.area:.3f} square DXF units")
    print(
        "WARNING: reconstructed road geometry is heuristic and must be "
        "visually confirmed in CAD"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export plant allow zones with roads to DXF and PNG."
    )
    parser.add_argument("plant_allow_zones_geojsonl", type=Path)
    parser.add_argument("constraint_map_geojsonl", type=Path)
    parser.add_argument("normalized_objects_geojsonl", type=Path)
    parser.add_argument(
        "--dxf-output", type=Path, default=Path("plant_allow_zones_debug.dxf")
    )
    parser.add_argument(
        "--png-output", type=Path, default=Path("plant_allow_zones_debug.png")
    )
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()
    if args.dpi <= 0:
        raise SystemExit("--dpi must be greater than zero")
    try:
        build_debug_export(
            args.plant_allow_zones_geojsonl,
            args.constraint_map_geojsonl,
            args.normalized_objects_geojsonl,
            args.dxf_output,
            args.png_output,
            args.dpi,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Plant-zone debug export error: {error}") from error


if __name__ == "__main__":
    main()
