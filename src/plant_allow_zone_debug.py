"""Export per-plant allow zones to a diagnostic PNG and DXF."""

from __future__ import annotations

import argparse
import json
import re
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
from shapely.ops import unary_union
from shapely.validation import make_valid

from constraint_builder import as_polygonal, read_object_geometry
from dxf_exporter import (
    GREEN_AI_APPID,
    add_zone_polygon,
    attach_planting_metadata,
    build_failure_details,
)
from normalizer import primitive_to_geometry
from plant_allow_zone import load_cleaned_utilities, load_normalized_objects


Polygonal = Polygon | MultiPolygon
Lineal = LineString | MultiLineString


DEBUG_CONTEXT_LAYERS = {
    "building": ("DEBUG_BUILDINGS", 30),
    "building_source": ("DEBUG_BUILDING_SOURCE", 1),
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
    "utility_well_footprint": ("DEBUG_UTILITY_WELL_FOOTPRINTS", 20),
    "clean_water_pipe": ("DEBUG_CLEAN_WATER_PIPE", 6),
    "clean_storm_drain": ("DEBUG_CLEAN_STORM_DRAIN", 34),
    "clean_gas_pipe": ("DEBUG_CLEAN_GAS_PIPE", 3),
    "clean_heat_pipe": ("DEBUG_CLEAN_HEAT_PIPE", 2),
    "clean_sewer_pipe": ("DEBUG_CLEAN_SEWER_PIPE", 32),
    "clean_power_cable": ("DEBUG_CLEAN_POWER_CABLE", 30),
    "clean_telecom_cable": ("DEBUG_CLEAN_TELECOM_CABLE", 94),
    "clean_overhead_power_line": ("DEBUG_CLEAN_OVERHEAD_POWER", 7),
}

RAW_UTILITY_CONTEXT_TYPES = {
    "water_pipe",
    "storm_drain",
    "gas_pipe",
    "heat_pipe",
    "sewer_pipe",
    "power_cable",
    "telecom_cable",
    "overhead_power_line",
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


def load_planting_plan(
    path: Path,
) -> tuple[dict[str, Polygonal], dict[str, list[tuple[Point, dict[str, Any]]]]]:
    """Load accepted area and point proposals for diagnostic rendering."""
    area_parts: dict[str, list[Polygon]] = {}
    points: dict[str, list[tuple[Point, dict[str, Any]]]] = {}
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            properties = feature.get("properties", {})
            object_type = properties.get("object_type")
            plant_type = properties.get("plant_type")
            if not plant_type or object_type not in {
                "proposed_planting",
                "proposed_planting_area",
            }:
                continue
            geometry = make_valid(shape(feature["geometry"]))
            if object_type == "proposed_planting":
                if not isinstance(geometry, Point):
                    raise ValueError(
                        f"Line {line_number}: proposed_planting must be a Point"
                    )
                points.setdefault(plant_type, []).append((geometry, properties))
            else:
                area_parts.setdefault(plant_type, []).extend(polygon_parts(geometry))
    areas = {
        plant_type: as_polygonal(unary_union(parts))
        for plant_type, parts in area_parts.items()
    }
    return areas, points


def load_rejected_decisions(
    path: Path,
) -> dict[str, list[tuple[Point, dict[str, Any]]]]:
    """Load rejected point candidates for red diagnostic rendering."""
    points: dict[str, list[tuple[Point, dict[str, Any]]]] = {}
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            properties = dict(feature.get("properties", {}))
            if (
                properties.get("object_type") != "planting_decision"
                or properties.get("status") != "rejected"
            ):
                continue
            geometry = make_valid(shape(feature["geometry"]))
            if not isinstance(geometry, Point):
                raise ValueError(
                    f"Line {line_number}: rejected planting decision must be a Point"
                )
            plant_type = str(properties.get("plant_type") or "plant")
            properties.setdefault(
                "planting_id", properties.get("candidate_id", "rejected")
            )
            points.setdefault(plant_type, []).append((geometry, properties))
    return points


def build_rule_exclusion_layers(
    report_path: Path,
    base_allowed_area: Polygonal,
    available_geometries: dict[str, Any],
    plant_type: str = "shrub",
) -> dict[str, Polygonal]:
    """Rebuild the sequential area removed by every applied placement rule."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    plant_report = report.get("plant_types", {}).get(plant_type, {})
    allowed = base_allowed_area
    result: dict[str, Polygonal] = {}
    for rule in plant_report.get("rules", []):
        if rule.get("status") != "applied":
            continue
        distance = rule.get("buffer_distance_in_dxf_units")
        target = str(rule.get("target_object_type", ""))
        if distance is None or not target:
            continue
        source = available_geometries.get(f"clean_{target}")
        if source is None or source.is_empty:
            source = available_geometries.get(target)
        if source is None or source.is_empty:
            continue
        exclusion = source.buffer(float(distance), quad_segs=8)
        removed = as_polygonal(make_valid(allowed.intersection(exclusion)))
        if not removed.is_empty:
            safe_code = re.sub(r"[^A-Z0-9_]+", "_", str(rule.get("rule_code", target)).upper())
            result[f"DEBUG_EXCL_{safe_code}"] = removed
        allowed = as_polygonal(make_valid(allowed.difference(exclusion)))
    return result


def point_reason_lines(properties: dict[str, Any]) -> list[str]:
    """Build a compact CAD explanation for one proposed point planting."""
    planting_id = str(properties.get("planting_id", "planting"))
    species = str(properties.get("species", ""))
    status = str(properties.get("status", ""))
    lines = [f"{planting_id}: {species}", f"status: {status}"]
    if status == "rejected":
        failures = build_failure_details(properties)
        lines.append(f"failed checks: {len(failures)}")
        for index, failure in enumerate(failures, start=1):
            lines.append(f"! {index}. {failure['title']} [{failure['code']}]")
            lines.append(f"  {failure['metric']}")
            if failure["detail"]:
                lines.append(f"  why: {failure['detail']}")
            lines.append(f"  action: {failure['advice']}")
            if failure["norm"]:
                lines.append(f"  source: {failure['norm']}")
        manual_checks = properties.get("manual_review_checks", [])
        if manual_checks:
            lines.append("? manual review: " + ", ".join(map(str, manual_checks)))
        return lines

    for check in properties.get("checks", []):
        check_status = str(check.get("status", ""))
        marker = "+" if check_status == "passed" else ("!" if check_status == "failed" else "?")
        code = str(check.get("code", "CHECK"))
        actual = check.get("actual_distance_m")
        required = check.get("required_distance_m")
        distance = ""
        if actual is not None and required is not None:
            distance = f": {float(actual):.2f} m >= {float(required):.2f} m"
        elif actual is not None:
            distance = f": measured {float(actual):.2f} m"
        elif required is not None:
            distance = f": required {float(required):.2f} m"
        lines.append(f"{marker} {code}{distance}")
        norm = str(check.get("norm_reference") or "").strip()
        if norm:
            lines.append(f"  {norm}")
        explanation = str(check.get("explanation") or "").strip()
        if check_status != "passed" and explanation:
            lines.append(f"  {explanation}")
    return lines


def export_reason_markdown(
    output_path: Path,
    planting_points: dict[str, list[tuple[Point, dict[str, Any]]]],
) -> None:
    """Write a readable audit table keyed by the IDs shown in CAD."""
    lines = ["# Обоснование точечных посадок", ""]
    for plant_type, items in sorted(planting_points.items()):
        for point, properties in items:
            planting_id = str(properties.get("planting_id", "planting"))
            lines.extend(
                [
                    f"## {planting_id}",
                    "",
                    f"- Тип: `{plant_type}`",
                    f"- Вид: {properties.get('species', '')}",
                    f"- Статус: `{properties.get('status', '')}`",
                    f"- Координаты DXF: X={point.x:.3f}, Y={point.y:.3f}",
                    "",
                    "| Проверка | Статус | Факт, м | Требуется, м | Норма | Объяснение |",
                    "|---|---:|---:|---:|---|---|",
                ]
            )
            for check in properties.get("checks", []):
                def clean(value: Any) -> str:
                    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

                actual = check.get("actual_distance_m")
                required = check.get("required_distance_m")
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            clean(check.get("code", "")),
                            clean(check.get("status", "")),
                            f"{float(actual):.3f}" if actual is not None else "",
                            f"{float(required):.3f}" if required is not None else "",
                            clean(check.get("norm_reference", "")),
                            clean(check.get("explanation", "")),
                        ]
                    )
                    + " |"
                )
            lines.append("")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def load_raw_object_linework(
    path: Path,
    object_type: str,
    curve_tolerance: float = 0.1,
) -> Any:
    """Read original extracted CAD primitives without polygon repairs."""
    geometries = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("object_type") != object_type:
                continue
            geometry = primitive_to_geometry(record, curve_tolerance)
            if geometry is not None and not geometry.is_empty:
                geometries.append(geometry)
    return unary_union(geometries) if geometries else GeometryCollection()


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
    base_allowed_area: Polygonal | None = None,
    planting_areas: dict[str, Polygonal] | None = None,
    planting_points: dict[str, list[tuple[Point, dict[str, Any]]]] | None = None,
    base_dxf_path: Path | None = None,
    rule_exclusions: dict[str, Polygonal] | None = None,
    rejected_points: dict[str, list[tuple[Point, dict[str, Any]]]] | None = None,
) -> Path:
    document = (
        ezdxf.readfile(base_dxf_path)
        if base_dxf_path is not None
        else ezdxf.new("R2018")
    )
    if base_dxf_path is None:
        document.header["$INSUNITS"] = 0
    if GREEN_AI_APPID not in document.appids:
        document.appids.add(GREEN_AI_APPID)
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
        "DEBUG_BASE_ALLOWED": 92,
        "DEBUG_NO_SHRUB": 1,
        "DEBUG_SHRUB_UNCOVERED": 30,
        "DEBUG_TREE_ALLOWED_UNUSED": 5,
        "DEBUG_PLANT_SHRUB": 2,
        "DEBUG_PLANT_HERBACEOUS": 94,
        "DEBUG_PLANT_TREE": 3,
        "DEBUG_PLANT_TREE_IDS": 7,
        "DEBUG_REJECTED_TREE": 1,
        "DEBUG_REJECTED_TREE_IDS": 1,
    }
    for layer, color in layer_colors.items():
        if layer in document.layers:
            layer_definition = document.layers.get(layer)
            layer_definition.color = color
            layer_definition.on()
            layer_definition.thaw()
        else:
            document.layers.add(layer, color=color)
    for object_type, (layer, color) in DEBUG_CONTEXT_LAYERS.items():
        if layer in document.layers:
            layer_definition = document.layers.get(layer)
            layer_definition.color = color
            layer_definition.on()
            layer_definition.thaw()
        else:
            layer_definition = document.layers.add(layer, color=color)
        if object_type in RAW_UTILITY_CONTEXT_TYPES:
            layer_definition.off()
    for index, layer in enumerate((rule_exclusions or {}).keys()):
        color = (1, 30, 6, 4, 5, 2)[index % 6]
        if layer in document.layers:
            layer_definition = document.layers.get(layer)
            layer_definition.color = color
            layer_definition.on()
            layer_definition.thaw()
        else:
            layer_definition = document.layers.add(layer, color=color)
        layer_definition.off()

    modelspace = document.modelspace()
    for entity in list(modelspace):
        if str(entity.dxf.layer).startswith("DEBUG_"):
            modelspace.delete_entity(entity)
    add_polygons(modelspace, work_boundary, "DEBUG_WORK_BOUNDARY")
    add_polygons(modelspace, road_area, "DEBUG_ROAD_AREA")
    add_polygons(modelspace, hard_surfaces, "DEBUG_HARD_SURFACES")
    add_polygons(modelspace, sidewalks, "DEBUG_SIDEWALKS")
    add_lines(modelspace, road_edges, "DEBUG_ROAD_EDGES")
    for object_type, geometry in context_geometries.items():
        layer, _ = DEBUG_CONTEXT_LAYERS[object_type]
        if object_type == "building":
            for polygon in polygon_parts(as_polygonal(geometry)):
                add_zone_polygon(modelspace, polygon, layer, 30, 0.15)
        else:
            add_context_geometry(modelspace, geometry, layer)
    for plant_type, geometry in zones.items():
        layer = f"DEBUG_ALLOW_{plant_type.upper()}"
        if layer not in document.layers:
            document.layers.add(layer, color=3)
        add_polygons(modelspace, geometry, layer)

    planting_areas = planting_areas or {}
    planting_points = planting_points or {}
    if base_allowed_area is not None and not base_allowed_area.is_empty:
        for polygon in polygon_parts(base_allowed_area):
            add_zone_polygon(
                modelspace, polygon, "DEBUG_BASE_ALLOWED", 92, 0.88
            )
        shrub_zone = zones.get("shrub", Polygon())
        no_shrub = as_polygonal(
            make_valid(base_allowed_area.difference(shrub_zone))
        )
        for polygon in polygon_parts(no_shrub):
            add_zone_polygon(modelspace, polygon, "DEBUG_NO_SHRUB", 1, 0.68)

    shrub_zone = zones.get("shrub", Polygon())
    shrub_planting = planting_areas.get("shrub", Polygon())
    if not shrub_zone.is_empty:
        uncovered = as_polygonal(make_valid(shrub_zone.difference(shrub_planting)))
        for polygon in polygon_parts(uncovered):
            add_zone_polygon(
                modelspace, polygon, "DEBUG_SHRUB_UNCOVERED", 30, 0.55
            )

    tree_zone = zones.get("tree", Polygon())
    if not tree_zone.is_empty:
        tree_symbols = [
            point.buffer(
                max(
                    0.05,
                    float(properties.get("symbol_radius_m", 0.75))
                    * float(properties.get("dxf_units_per_meter", 1.0)),
                ),
                quad_segs=12,
            )
            for point, properties in planting_points.get("tree", [])
        ]
        selected_tree_area = unary_union(tree_symbols) if tree_symbols else Polygon()
        unused_tree_area = as_polygonal(
            make_valid(tree_zone.difference(selected_tree_area))
        )
        for polygon in polygon_parts(unused_tree_area):
            add_zone_polygon(
                modelspace, polygon, "DEBUG_TREE_ALLOWED_UNUSED", 5, 0.88
            )

    result_area_layers = {
        "shrub": ("DEBUG_PLANT_SHRUB", 2),
        "herbaceous": ("DEBUG_PLANT_HERBACEOUS", 94),
    }
    for plant_type, (layer, color) in result_area_layers.items():
        geometry = planting_areas.get(plant_type)
        if geometry is None or geometry.is_empty:
            continue
        for polygon in polygon_parts(geometry):
            add_zone_polygon(modelspace, polygon, layer, color, 0.42)

    for point, properties in planting_points.get("tree", []):
        radius = max(
            0.05,
            float(properties.get("symbol_radius_m", 0.75))
            * float(properties.get("dxf_units_per_meter", 1.0)),
        )
        modelspace.add_circle(
            (point.x, point.y),
            radius,
            dxfattribs={
                "layer": "DEBUG_PLANT_TREE",
                "color": 3,
                "lineweight": 70,
            },
        )
        planting_id = str(properties.get("planting_id", "TREE"))
        modelspace.add_text(
            planting_id,
            height=max(0.35, radius * 0.35),
            dxfattribs={"layer": "DEBUG_PLANT_TREE_IDS", "color": 7},
        ).set_placement((point.x + radius, point.y + radius))
        reason_layer = "DEBUG_REASON_" + re.sub(
            r"[^A-Z0-9_]+", "_", planting_id.upper()
        )
        if reason_layer not in document.layers:
            reason_definition = document.layers.add(reason_layer, color=7)
        else:
            reason_definition = document.layers.get(reason_layer)
        reason_definition.off()
        label_x = point.x + radius + 1.0
        label_y = point.y + radius + 1.0
        modelspace.add_line(
            (point.x, point.y),
            (label_x, label_y),
            dxfattribs={"layer": reason_layer, "color": 7},
        )
        reason_text = "\\P".join(point_reason_lines(properties))
        modelspace.add_mtext(
            reason_text,
            dxfattribs={
                "layer": reason_layer,
                "color": 7,
                "char_height": 0.55,
                "width": 65.0,
                "insert": (label_x, label_y),
            },
        )

    for plant_type, items in sorted((rejected_points or {}).items()):
        suffix = re.sub(r"[^A-Z0-9_]+", "_", plant_type.upper()) or "PLANT"
        marker_layer = f"DEBUG_REJECTED_{suffix}"
        id_layer = f"DEBUG_REJECTED_{suffix}_IDS"
        for layer in (marker_layer, id_layer):
            if layer not in document.layers:
                document.layers.add(layer, color=1)
            definition = document.layers.get(layer)
            definition.color = 1
            definition.on()
            definition.thaw()
        for point, properties in items:
            candidate_id = str(
                properties.get("candidate_id")
                or properties.get("planting_id")
                or "REJECTED"
            )
            marker_radius = 0.45
            circle = modelspace.add_circle(
                (point.x, point.y),
                marker_radius,
                dxfattribs={"layer": marker_layer, "color": 1, "lineweight": 70},
            )
            attach_planting_metadata(circle, properties)
            for start, end in (
                (
                    (point.x - marker_radius, point.y - marker_radius),
                    (point.x + marker_radius, point.y + marker_radius),
                ),
                (
                    (point.x - marker_radius, point.y + marker_radius),
                    (point.x + marker_radius, point.y - marker_radius),
                ),
            ):
                modelspace.add_line(
                    start,
                    end,
                    dxfattribs={"layer": marker_layer, "color": 1, "lineweight": 70},
                )
            modelspace.add_text(
                candidate_id,
                height=0.35,
                dxfattribs={"layer": id_layer, "color": 1},
            ).set_placement((point.x + 0.6, point.y + 0.6))

            reason_layer = "DEBUG_REJECT_REASON_" + re.sub(
                r"[^A-Z0-9_]+", "_", candidate_id.upper()
            )
            if reason_layer not in document.layers:
                reason_definition = document.layers.add(reason_layer, color=1)
            else:
                reason_definition = document.layers.get(reason_layer)
            reason_definition.off()
            label_x, label_y = point.x + 1.0, point.y + 1.0
            modelspace.add_line(
                (point.x, point.y),
                (label_x, label_y),
                dxfattribs={"layer": reason_layer, "color": 1},
            )
            modelspace.add_mtext(
                "\\P".join(point_reason_lines(properties)),
                dxfattribs={
                    "layer": reason_layer,
                    "color": 1,
                    "char_height": 0.35,
                    "width": 65.0,
                    "insert": (label_x, label_y),
                },
            )

    for index, (layer, geometry) in enumerate((rule_exclusions or {}).items()):
        color = (1, 30, 6, 4, 5, 2)[index % 6]
        for polygon in polygon_parts(geometry):
            add_zone_polygon(modelspace, polygon, layer, color, 0.58)

    # Open the diagnostic drawing with the actual proposal visible.  The
    # explanation layers remain available in the layer manager and can be
    # enabled one at a time without overlapping fills obscuring the result.
    visible_layers = {
        "DEBUG_WORK_BOUNDARY",
        "DEBUG_PLANT_SHRUB",
        "DEBUG_PLANT_HERBACEOUS",
        "DEBUG_PLANT_TREE",
        "DEBUG_REJECTED_TREE",
    }
    for layer in layer_colors:
        if layer not in visible_layers:
            document.layers.get(layer).off()
    candidates = [output_path, *(
        output_path.with_name(
            f"{output_path.stem}_v{version}{output_path.suffix}"
        )
        for version in range(2, 100)
    )]
    last_error: PermissionError | None = None
    for candidate in candidates:
        try:
            document.saveas(candidate)
            if candidate != output_path:
                print(
                    f"WARNING: {output_path} is locked; "
                    f"debug DXF written to {candidate}"
                )
            return candidate
        except PermissionError as error:
            last_error = error
    assert last_error is not None
    raise last_error


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
    cleaned_utilities_path: Path | None = None,
    raw_objects_path: Path | None = None,
    planting_plan_path: Path | None = None,
    base_dxf_path: Path | None = None,
    zone_report_path: Path | None = None,
    reasons_output_path: Path | None = None,
    planting_decisions_path: Path | None = None,
) -> None:
    zones = load_plant_zones(plant_zones_path)
    normalized_objects = load_normalized_objects(normalized_path)
    if cleaned_utilities_path is not None:
        cleaned = load_cleaned_utilities(cleaned_utilities_path)
        for object_type in RAW_UTILITY_CONTEXT_TYPES:
            geometry = cleaned.get(object_type)
            if geometry is not None and not geometry.is_empty:
                normalized_objects[f"clean_{object_type}"] = geometry
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
    base_allowed_area = as_polygonal(
        read_object_geometry(constraint_map_path, "base_allowed_area")
    )
    planting_areas: dict[str, Polygonal] = {}
    planting_points: dict[str, list[tuple[Point, dict[str, Any]]]] = {}
    rejected_points: dict[str, list[tuple[Point, dict[str, Any]]]] = {}
    if planting_plan_path is not None:
        planting_areas, planting_points = load_planting_plan(planting_plan_path)
    if planting_decisions_path is not None:
        rejected_points = load_rejected_decisions(planting_decisions_path)
    if reasons_output_path is not None:
        reason_points = {
            plant_type: list(items)
            for plant_type, items in planting_points.items()
        }
        for plant_type, items in rejected_points.items():
            reason_points.setdefault(plant_type, []).extend(items)
        export_reason_markdown(reasons_output_path, reason_points)
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
    # Buildings must remain whole in the diagnostic drawing. Clipping them to
    # the street work boundary makes valid footprints look like thin strips.
    full_buildings = normalized_objects.get("building")
    if full_buildings is not None and not full_buildings.is_empty:
        context_geometries["building"] = full_buildings
    if raw_objects_path is not None:
        source_building_lines = load_raw_object_linework(
            raw_objects_path, "building"
        )
        if not source_building_lines.is_empty:
            context_geometries["building_source"] = source_building_lines
    rule_exclusions: dict[str, Polygonal] = {}
    if zone_report_path is not None:
        rule_exclusions = build_rule_exclusion_layers(
            zone_report_path,
            base_allowed_area,
            {**normalized_objects, **context_geometries},
        )

    actual_dxf_output = export_dxf(
        dxf_output,
        work_boundary,
        hard_surfaces,
        road_area,
        sidewalks,
        road_edges,
        context_geometries,
        zones,
        base_allowed_area,
        planting_areas,
        planting_points,
        base_dxf_path,
        rule_exclusions,
        rejected_points,
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
    print(f"DXF: {actual_dxf_output}")
    print(f"PNG: {png_output}")
    print(f"Reconstructed road: {road_area.area:.3f} square DXF units")
    print(f"Sidewalks: {sidewalks.area:.3f} square DXF units")
    for object_type, geometry in context_geometries.items():
        layer, _ = DEBUG_CONTEXT_LAYERS[object_type]
        part_count = len(geometry.geoms) if hasattr(geometry, "geoms") else 1
        print(f"  {object_type}: {layer} | {part_count} part(s)")
    for plant_type, geometry in sorted(zones.items()):
        print(f"  {plant_type}: {geometry.area:.3f} square DXF units")
    for plant_type, items in sorted(rejected_points.items()):
        print(f"  rejected {plant_type}: {len(items)} diagnostic point(s)")
    for layer, geometry in rule_exclusions.items():
        print(f"  {layer}: {geometry.area:.3f} square DXF units")
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
    parser.add_argument(
        "--utility-geometries",
        "--cleaned-utilities",
        dest="cleaned_utilities",
        type=Path,
        help=(
            "Accepted cleaned or reconstructed utility GeoJSONL used by "
            "automatic setback rules"
        ),
    )
    parser.add_argument(
        "--raw-objects",
        type=Path,
        help="Extracted JSONL used to draw untouched source building lines",
    )
    parser.add_argument(
        "--planting-plan",
        type=Path,
        help=(
            "Optional planting plan. Adds separate planted, uncovered and "
            "allowed-but-unused diagnostic layers."
        ),
    )
    parser.add_argument(
        "--planting-decisions",
        type=Path,
        help="Optional decisions GeoJSONL; rejected candidates are drawn in red.",
    )
    parser.add_argument(
        "--base-dxf",
        type=Path,
        help="Optional source DXF onto which all DEBUG layers are overlaid.",
    )
    parser.add_argument(
        "--zone-report",
        type=Path,
        help="Optional plant-zone report used to export one layer per applied rule.",
    )
    parser.add_argument(
        "--reasons-output",
        type=Path,
        help="Optional Markdown report with checks for every point planting ID.",
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
            args.cleaned_utilities,
            args.raw_objects,
            args.planting_plan,
            args.base_dxf,
            args.zone_report,
            args.reasons_output,
            args.planting_decisions,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Plant-zone debug export error: {error}") from error


if __name__ == "__main__":
    main()
