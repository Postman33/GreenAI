"""Add calculated planting zones to a copy of the original DXF."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import ezdxf
from ezdxf.colors import float2transparency
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape
from shapely.ops import unary_union
from shapely.validation import make_valid


Polygonal = Polygon | MultiPolygon
ZONE_LAYERS = {
    "tree": ("GREEN_AI_ZONE_TREE", 3),
    "shrub": ("GREEN_AI_ZONE_SHRUB", 2),
    "herbaceous": ("GREEN_AI_ZONE_HERBACEOUS", 4),
    "groundcover": ("GREEN_AI_ZONE_GROUNDCOVER", 6),
}
ROAD_LAYER = ("GREEN_AI_RECONSTRUCTED_ROAD", 8)


def polygon_parts(geometry: Any) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def load_plant_zones(path: Path) -> dict[str, Polygonal]:
    """Read and merge plant_allow_zone features by plant class."""
    grouped: dict[str, list[Polygon]] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid plant-zone GeoJSON"
                ) from error
            properties = feature.get("properties", {})
            if properties.get("object_type") != "plant_allow_zone":
                continue
            plant_type = properties.get("plant_type")
            if not plant_type:
                raise ValueError(f"Line {line_number}: plant_type is missing")
            geometry = make_valid(shape(feature["geometry"]))
            grouped.setdefault(plant_type, []).extend(polygon_parts(geometry))

    if not grouped:
        raise ValueError("No plant_allow_zone features were found")
    return {
        plant_type: unary_union(parts)
        for plant_type, parts in grouped.items()
    }


def load_constraint_geometry(path: Path, object_type: str) -> Polygonal:
    """Read and merge one polygonal object type from a GeoJSONL map."""
    parts: list[Polygon] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid constraint GeoJSON"
                ) from error
            if feature.get("properties", {}).get("object_type") != object_type:
                continue
            parts.extend(polygon_parts(make_valid(shape(feature["geometry"]))))
    if not parts:
        raise ValueError(f"No {object_type} geometry found in {path}")
    return unary_union(parts)


def ensure_layer(document: ezdxf.document.Drawing, name: str, color: int) -> None:
    if name in document.layers:
        layer = document.layers.get(name)
        layer.color = color
        layer.on()
        layer.thaw()
    else:
        document.layers.add(name, color=color, lineweight=50)


def remove_previous_entities(modelspace, layer_name: str) -> int:
    """Make repeated export to an already generated DXF idempotent."""
    removed = 0
    for entity in list(modelspace.query(f'*[layer=="{layer_name}"]')):
        modelspace.delete_entity(entity)
        removed += 1
    return removed


def ring_vertices(ring) -> list[tuple[float, float]]:
    coordinates = [(float(x), float(y)) for x, y, *_ in ring.coords]
    if len(coordinates) > 1 and coordinates[0] == coordinates[-1]:
        coordinates.pop()
    return coordinates


def add_zone_polygon(
    modelspace,
    polygon: Polygon,
    layer_name: str,
    color: int,
    transparency: float,
) -> int:
    exterior = ring_vertices(polygon.exterior)
    if len(exterior) < 3:
        return 0

    hatch = modelspace.add_hatch(
        color=color,
        dxfattribs={
            "layer": layer_name,
            "transparency": float2transparency(transparency),
        },
    )
    hatch.set_solid_fill(color=color)
    hatch.paths.add_polyline_path(exterior, is_closed=True, flags=1)
    for interior in polygon.interiors:
        hole = ring_vertices(interior)
        if len(hole) >= 3:
            hatch.paths.add_polyline_path(hole, is_closed=True, flags=0)

    modelspace.add_lwpolyline(
        exterior,
        close=True,
        dxfattribs={"layer": layer_name, "color": color, "lineweight": 50},
    )
    for interior in polygon.interiors:
        hole = ring_vertices(interior)
        if len(hole) >= 3:
            modelspace.add_lwpolyline(
                hole,
                close=True,
                dxfattribs={
                    "layer": layer_name,
                    "color": color,
                    "lineweight": 50,
                },
            )
    return 1


def export_zones(
    input_dxf: Path,
    zones_path: Path,
    output_dxf: Path,
    transparency: float,
    constraint_map_path: Path | None = None,
) -> None:
    if input_dxf.resolve() == output_dxf.resolve():
        raise ValueError("Output DXF must differ from the original input DXF")
    zones = load_plant_zones(zones_path)
    document = ezdxf.readfile(input_dxf)
    modelspace = document.modelspace()
    original_entity_count = len(modelspace)
    exported: dict[str, dict[str, Any]] = {}

    if constraint_map_path is not None:
        road_area = load_constraint_geometry(constraint_map_path, "road_area")
        road_layer_name, road_color = ROAD_LAYER
        ensure_layer(document, road_layer_name, road_color)
        removed = remove_previous_entities(modelspace, road_layer_name)
        road_polygon_count = sum(
            add_zone_polygon(
                modelspace,
                polygon,
                road_layer_name,
                road_color,
                min(0.82, max(transparency, 0.72)),
            )
            for polygon in polygon_parts(road_area)
        )
        exported["reconstructed_road"] = {
            "layer": road_layer_name,
            "polygons": road_polygon_count,
            "area_in_dxf_square_units": road_area.area,
            "previous_entities_removed": removed,
        }

    # Add larger shrub zones first so tree zones remain visible above them.
    order = ["shrub", "tree", "herbaceous", "groundcover"]
    order.extend(sorted(set(zones) - set(order)))
    for plant_type in order:
        geometry = zones.get(plant_type)
        if geometry is None or geometry.is_empty:
            continue
        layer_name, color = ZONE_LAYERS.get(
            plant_type,
            (f"GREEN_AI_ZONE_{plant_type.upper()}", 3),
        )
        ensure_layer(document, layer_name, color)
        removed = remove_previous_entities(modelspace, layer_name)
        polygon_count = sum(
            add_zone_polygon(
                modelspace,
                polygon,
                layer_name,
                color,
                transparency,
            )
            for polygon in polygon_parts(geometry)
        )
        exported[plant_type] = {
            "layer": layer_name,
            "polygons": polygon_count,
            "area_in_dxf_square_units": geometry.area,
            "previous_entities_removed": removed,
        }

    document.saveas(output_dxf)
    print(f"Original DXF: {input_dxf}")
    print(f"Plant zones: {zones_path}")
    if constraint_map_path is not None:
        print(f"Constraint map: {constraint_map_path}")
    print(f"Output DXF: {output_dxf}")
    print(f"Original modelspace entities: {original_entity_count}")
    for plant_type, result in exported.items():
        print(
            f"  {plant_type}: {result['layer']} | "
            f"{result['polygons']} polygon(s) | "
            f"area {result['area_in_dxf_square_units']:.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add plant allow-zone layers to a copy of the original DXF."
    )
    parser.add_argument("input_dxf", type=Path)
    parser.add_argument("plant_allow_zones_geojsonl", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("result_with_plant_allow_zones.dxf"),
    )
    parser.add_argument(
        "--constraint-map",
        type=Path,
        help="Optional constraint GeoJSONL; adds reconstructed road layer.",
    )
    parser.add_argument(
        "--transparency",
        type=float,
        default=0.65,
        help="Zone fill transparency from 0 (opaque) to 1 (invisible).",
    )
    args = parser.parse_args()
    if not 0 <= args.transparency < 1:
        raise SystemExit("--transparency must be between 0 inclusive and 1 exclusive")
    try:
        export_zones(
            args.input_dxf,
            args.plant_allow_zones_geojsonl,
            args.output,
            args.transparency,
            args.constraint_map,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"DXF export error: {error}") from error


if __name__ == "__main__":
    main()
