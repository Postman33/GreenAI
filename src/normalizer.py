"""Normalize extracted CAD primitives into GeoJSON-like local geometries.

Input is JSONL produced by either ``src/loader.py`` or the Go extractor. The
output deliberately has no CRS: DXF coordinates stay in their native local
coordinate system. It is the input for the following constraint-map stage.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import LineString, MultiLineString, MultiPoint, Point, Polygon, mapping
from shapely.ops import linemerge, polygonize, unary_union


LINE_TYPES = {
    "road_edge",
    "street_boundary",
    "water_pipe",
    "storm_drain",
    "gas_pipe",
    "heat_pipe",
    "sewer_pipe",
    "power_cable",
    "telecom_cable",
    "overhead_power_line",
}
POLYGON_TYPES = {
    "work_boundary",
    "building",
    "sidewalk",
    "existing_tree_belt",
    "vegetation_boundary",
}
POINT_TYPES = {"existing_tree", "utility_marker", "utility_well"}
BUILDING_MAX_ENDPOINT_GAP_DXF_UNITS = 0.1
BUILDING_MAX_RELATIVE_GAP_DXF_UNITS = 1.0
BUILDING_MAX_RELATIVE_GAP_RATIO = 0.01
BUILDING_MISSING_WALL_ANGLE_TOLERANCE_DEG = 18.0
BUILDING_MIN_REPAIRED_AREA_DXF_SQ_UNITS = 10.0
BUILDING_MIN_REPAIRED_WIDTH_DXF_UNITS = 3.0
UTILITY_WELL_MIN_FULL_CIRCLE_SWEEP_DEG = 350.0


def xy(point: list[float] | tuple[float, ...]) -> tuple[float, float]:
    """Discard DXF Z and polyline width/bulge values."""
    return float(point[0]), float(point[1])


def arc_points(
    center: list[float], radius: float, start_angle: float, end_angle: float, tolerance: float
) -> list[tuple[float, float]]:
    """Approximate a DXF arc with chords no farther than tolerance from it."""
    if radius <= 0:
        return []
    if end_angle < start_angle:
        end_angle += 360
    sweep = math.radians(end_angle - start_angle)
    length = abs(sweep * radius)
    steps = max(8, min(720, math.ceil(length / tolerance)))
    start = math.radians(start_angle)
    return [
        (
            center[0] + radius * math.cos(start + sweep * index / steps),
            center[1] + radius * math.sin(start + sweep * index / steps),
        )
        for index in range(steps + 1)
    ]


def ellipse_points(raw: dict[str, Any], tolerance: float) -> list[tuple[float, float]]:
    """Approximate a DXF ellipse from its centre, major-axis vector and ratio."""
    center = xy(raw["center"])
    major_x, major_y = xy(raw["major_axis"])
    ratio = float(raw["ratio"])
    major_length = math.hypot(major_x, major_y)
    if major_length == 0 or ratio <= 0:
        return []
    start = float(raw.get("start_param", 0))
    end = float(raw.get("end_param", math.tau))
    if end < start:
        end += math.tau
    perimeter_hint = math.tau * max(major_length, major_length * ratio)
    steps = max(12, min(720, math.ceil(perimeter_hint / tolerance)))
    # DXF defines the minor axis as the perpendicular to the major axis.
    minor_x, minor_y = -major_y * ratio, major_x * ratio
    return [
        (
            center[0] + major_x * math.cos(start + (end - start) * index / steps)
            + minor_x * math.sin(start + (end - start) * index / steps),
            center[1] + major_y * math.cos(start + (end - start) * index / steps)
            + minor_y * math.sin(start + (end - start) * index / steps),
        )
        for index in range(steps + 1)
    ]


def primitive_to_geometry(record: dict[str, Any], tolerance: float):
    """Turn one raw CAD primitive into a Shapely geometry, where possible."""
    raw = record["geometry"]
    kind = raw["kind"]

    if kind == "line":
        return LineString([xy(raw["start"]), xy(raw["end"])])
    if kind in {"point", "insert_point"}:
        return Point(xy(raw["location"]))
    if kind == "polyline":
        coordinates = [xy(point) for point in raw["points"]]
        if len(coordinates) < 2:
            return None
        if raw.get("closed") and coordinates[0] != coordinates[-1]:
            coordinates.append(coordinates[0])
        return LineString(coordinates)
    if kind == "arc":
        coordinates = arc_points(
            xy(raw["center"]),
            float(raw["radius"]),
            float(raw["start_angle"]),
            float(raw["end_angle"]),
            tolerance,
        )
        return LineString(coordinates) if len(coordinates) > 1 else None
    if kind == "circle":
        coordinates = arc_points(xy(raw["center"]), float(raw["radius"]), 0, 360, tolerance)
        return LineString(coordinates) if len(coordinates) > 1 else None
    if kind == "ellipse":
        coordinates = ellipse_points(raw, tolerance)
        return LineString(coordinates) if len(coordinates) > 1 else None
    if kind == "hatch":
        boundaries = []
        for path in raw.get("boundary_paths", []):
            coordinates = [xy(point) for point in path]
            if len(coordinates) < 3:
                continue
            if coordinates[0] != coordinates[-1]:
                coordinates.append(coordinates[0])
            boundaries.append(LineString(coordinates))
        if len(boundaries) == 1:
            return boundaries[0]
        if boundaries:
            return MultiLineString(boundaries)
    return None


def polygonal_geometry(lines: Iterable[Any]):
    """Build valid polygonal faces from closed and fragmented boundary lines."""
    line_list = [line for line in lines if line is not None and not line.is_empty]
    if not line_list:
        return None
    faces = list(polygonize(unary_union(line_list)))
    if not faces:
        return None
    # Disjoint work areas remain a MultiPolygon; intersecting fragments dissolve.
    return unary_union(faces)


def line_parts(geometry: Any) -> Iterable[LineString]:
    """Yield line components from a noded Shapely geometry."""
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from line_parts(part)


def angle_between_vectors_deg(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    denominator = math.hypot(*first) * math.hypot(*second)
    if denominator == 0:
        return math.nan
    cosine = max(
        -1.0,
        min(1.0, (first[0] * second[0] + first[1] * second[1]) / denominator),
    )
    return math.degrees(math.acos(cosine))


def minimum_rotated_width(geometry: Polygon) -> float:
    rectangle = geometry.minimum_rotated_rectangle
    coordinates = list(rectangle.exterior.coords)
    lengths = [
        math.dist(coordinates[index], coordinates[index + 1])
        for index in range(len(coordinates) - 1)
    ]
    positive = [length for length in lengths if length > 1e-9]
    return min(positive) if positive else 0.0


def polygonal_geometry_with_endpoint_closure(
    lines: Iterable[Any],
    max_endpoint_gap: float = BUILDING_MAX_ENDPOINT_GAP_DXF_UNITS,
) -> tuple[Any, dict[str, Any]]:
    """Polygonize building outlines and repair geometrically supported gaps.

    CAD building outlines are often split into unordered LINE primitives. Their
    exact start/end coordinates define a graph. A connector is accepted for a
    tiny drafting gap, for a sub-metre gap negligible relative to the outline,
    or for a missing wall meeting both end segments approximately at right
    angles. Branched and otherwise ambiguous components remain linework.
    """
    line_list = [line for line in lines if line is not None and not line.is_empty]
    diagnostics: dict[str, Any] = {
        "line_component_count": 0,
        "closed_component_count": 0,
        "open_chain_count": 0,
        "branched_component_count": 0,
        "repaired_chain_count": 0,
        "orthogonal_missing_wall_count": 0,
        "rejected_open_chain_count": 0,
        "rejection_reason_counts": {},
        "max_endpoint_gap_in_dxf_units": max_endpoint_gap,
        "repaired_total_length_in_dxf_units": 0.0,
        "repaired_chains": [],
        "rejected_chains": [],
    }
    if not line_list:
        return None, diagnostics

    noded = unary_union(line_list)
    parts = list(line_parts(noded))
    endpoint_to_parts: dict[tuple[float, float], list[int]] = defaultdict(list)
    endpoint_coordinates: dict[tuple[float, float], tuple[float, float]] = {}
    part_endpoints: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for part_index, part in enumerate(parts):
        coordinates = list(part.coords)
        if len(coordinates) < 2:
            part_endpoints.append(((math.nan, math.nan), (math.nan, math.nan)))
            continue
        keys = []
        for endpoint in (coordinates[0], coordinates[-1]):
            point = float(endpoint[0]), float(endpoint[1])
            key = round(point[0], 8), round(point[1], 8)
            endpoint_to_parts[key].append(part_index)
            endpoint_coordinates[key] = point
            keys.append(key)
        part_endpoints.append((keys[0], keys[1]))

    neighbours: dict[int, set[int]] = {index: set() for index in range(len(parts))}
    for incident_parts in endpoint_to_parts.values():
        for part_index in incident_parts:
            neighbours[part_index].update(
                other for other in incident_parts if other != part_index
            )

    components: list[list[int]] = []
    remaining = set(range(len(parts)))
    while remaining:
        start = remaining.pop()
        component = [start]
        stack = [start]
        while stack:
            current = stack.pop()
            connected = neighbours[current] & remaining
            remaining.difference_update(connected)
            component.extend(connected)
            stack.extend(connected)
        components.append(component)

    diagnostics["line_component_count"] = len(components)

    connectors: list[Any] = []
    for component in components:
        degree: Counter[tuple[float, float]] = Counter()
        known_length = 0.0
        for part_index in component:
            start_key, end_key = part_endpoints[part_index]
            degree[start_key] += 1
            degree[end_key] += 1
            known_length += parts[part_index].length

        if degree and all(value == 2 for value in degree.values()):
            diagnostics["closed_component_count"] += 1
            continue
        if any(value > 2 for value in degree.values()):
            diagnostics["branched_component_count"] += 1
            continue
        loose_ends = [key for key, value in degree.items() if value == 1]
        if len(loose_ends) != 2 or any(value != 2 for value in degree.values() if value != 1):
            diagnostics["rejected_open_chain_count"] += 1
            continue

        diagnostics["open_chain_count"] += 1
        start = endpoint_coordinates[loose_ends[0]]
        end = endpoint_coordinates[loose_ends[1]]
        closure_length = math.dist(start, end)
        closure_ratio = closure_length / known_length if known_length > 0 else math.inf
        connector = LineString([start, end])
        component_linework = unary_union(
            [*[parts[part_index] for part_index in component], connector]
        )
        candidate_faces = list(polygonize(component_linework))
        candidate_polygon = unary_union(candidate_faces) if candidate_faces else None
        candidate_is_valid = (
            closure_length > 0
            and isinstance(candidate_polygon, Polygon)
            and not candidate_polygon.is_empty
            and candidate_polygon.area > 0
            and candidate_polygon.is_valid
        )
        repair_mode = None
        endpoint_angles: list[float] = []
        candidate_width = 0.0
        angles_are_orthogonal = False
        connector_crosses_linework = False
        if candidate_is_valid and closure_length <= max_endpoint_gap:
            repair_mode = "tiny_endpoint_gap"
        elif (
            candidate_is_valid
            and closure_length <= BUILDING_MAX_RELATIVE_GAP_DXF_UNITS
            and closure_ratio <= BUILDING_MAX_RELATIVE_GAP_RATIO
        ):
            repair_mode = "small_relative_gap"
        elif candidate_is_valid:
            incident_parts: dict[tuple[float, float], int] = {}
            for part_index in component:
                first_key, last_key = part_endpoints[part_index]
                if first_key in loose_ends:
                    incident_parts[first_key] = part_index
                if last_key in loose_ends:
                    incident_parts[last_key] = part_index

            for endpoint_key, other_point in (
                (loose_ends[0], end),
                (loose_ends[1], start),
            ):
                part_index = incident_parts[endpoint_key]
                coordinates = list(parts[part_index].coords)
                if endpoint_key == part_endpoints[part_index][0]:
                    inward = (
                        coordinates[1][0] - coordinates[0][0],
                        coordinates[1][1] - coordinates[0][1],
                    )
                    endpoint_point = coordinates[0]
                else:
                    inward = (
                        coordinates[-2][0] - coordinates[-1][0],
                        coordinates[-2][1] - coordinates[-1][1],
                    )
                    endpoint_point = coordinates[-1]
                toward_other = (
                    other_point[0] - endpoint_point[0],
                    other_point[1] - endpoint_point[1],
                )
                endpoint_angles.append(
                    angle_between_vectors_deg(inward, toward_other)
                )

            candidate_width = minimum_rotated_width(candidate_polygon)
            angles_are_orthogonal = all(
                math.isfinite(angle)
                and abs(angle - 90.0)
                <= BUILDING_MISSING_WALL_ANGLE_TOLERANCE_DEG
                for angle in endpoint_angles
            )
            connector_crosses_linework = connector.crosses(noded)
            if (
                angles_are_orthogonal
                and not connector_crosses_linework
                and candidate_polygon.area >= BUILDING_MIN_REPAIRED_AREA_DXF_SQ_UNITS
                and candidate_width >= BUILDING_MIN_REPAIRED_WIDTH_DXF_UNITS
            ):
                repair_mode = "orthogonal_missing_wall"

        if repair_mode is not None:
            connectors.append(connector)
            diagnostics["repaired_chain_count"] += 1
            if repair_mode == "orthogonal_missing_wall":
                diagnostics["orthogonal_missing_wall_count"] += 1
            diagnostics["repaired_total_length_in_dxf_units"] += closure_length
            diagnostics["repaired_chains"].append(
                {
                    "mode": repair_mode,
                    "start": [round(start[0], 6), round(start[1], 6)],
                    "end": [round(end[0], 6), round(end[1], 6)],
                    "endpoint_distance": round(closure_length, 6),
                    "added_length": round(closure_length, 6),
                    "known_chain_length": round(known_length, 6),
                    "closure_ratio": round(closure_ratio, 8),
                    "endpoint_angles_deg": [
                        round(angle, 4) for angle in endpoint_angles
                    ],
                    "candidate_area_in_dxf_square_units": round(
                        candidate_polygon.area, 6
                    ),
                    "candidate_min_width_in_dxf_units": round(
                        candidate_width, 6
                    ),
                }
            )
        else:
            diagnostics["rejected_open_chain_count"] += 1
            rejection_reasons = []
            if not candidate_is_valid:
                rejection_reasons.append("invalid_polygon")
            else:
                if not angles_are_orthogonal:
                    rejection_reasons.append("non_orthogonal_endpoints")
                if connector_crosses_linework:
                    rejection_reasons.append("connector_crosses_linework")
                if candidate_polygon.area < BUILDING_MIN_REPAIRED_AREA_DXF_SQ_UNITS:
                    rejection_reasons.append("area_below_minimum")
                if candidate_width < BUILDING_MIN_REPAIRED_WIDTH_DXF_UNITS:
                    rejection_reasons.append("width_below_minimum")
            reason_counts = Counter(diagnostics["rejection_reason_counts"])
            reason_counts.update(rejection_reasons)
            diagnostics["rejection_reason_counts"] = dict(reason_counts)
            diagnostics["rejected_chains"].append(
                {
                    "start": [round(start[0], 6), round(start[1], 6)],
                    "end": [round(end[0], 6), round(end[1], 6)],
                    "endpoint_distance": round(closure_length, 6),
                    "known_chain_length": round(known_length, 6),
                    "closure_ratio": round(closure_ratio, 8),
                    "endpoint_angles_deg": [round(angle, 4) for angle in endpoint_angles],
                    "candidate_area_in_dxf_square_units": round(
                        candidate_polygon.area, 6
                    ) if candidate_is_valid else 0.0,
                    "candidate_min_width_in_dxf_units": round(candidate_width, 6),
                    "reasons": rejection_reasons,
                }
            )

    diagnostics["repaired_total_length_in_dxf_units"] = round(
        diagnostics["repaired_total_length_in_dxf_units"], 6
    )
    repaired_linework = unary_union([noded, *connectors]) if connectors else noded
    faces = list(polygonize(repaired_linework))
    return (unary_union(faces) if faces else None), diagnostics


def polygonal_geometry_per_primitive(geometries: Iterable[Any]):
    """Polygonize each closed CAD object before merging the results.

    HATCH records and explicit work-boundary polylines are already independent
    area objects. Polygonizing all of their rings together can create faces in
    overlaps and gaps that were never present in the DXF. Open annotation
    strokes are ignored because they do not form a face by themselves.
    """
    faces = []
    for geometry in geometries:
        if geometry is None or geometry.is_empty:
            continue
        faces.extend(polygonize(geometry))
    return unary_union(faces) if faces else None


def lineal_geometry(lines: Iterable[Any]):
    """Join connected segments while preserving disconnected network branches."""
    line_list = [line for line in lines if line is not None and not line.is_empty]
    if not line_list:
        return None
    combined = unary_union(line_list)
    try:
        return linemerge(combined)
    except ValueError:
        # A rare mixed GeometryCollection is still valid as a constraint source.
        return combined


def point_geometry(geometries: Iterable[Any]):
    """Represent markers and tree-symbol parts as a single MultiPoint layer."""
    points = []
    for geometry in geometries:
        if geometry is None or geometry.is_empty:
            continue
        points.append(geometry if geometry.geom_type == "Point" else geometry.centroid)
    return MultiPoint(points) if points else None


def utility_well_footprint(
    records: Iterable[dict[str, Any]],
) -> tuple[Any | None, dict[str, Any]]:
    """Restore physical well/manhole disks from full-circle CAD primitives.

    In the supplied geobases the ``Колодцы`` symbols are stored as ARC objects
    with an almost 360-degree sweep and a stable radius.  Their centre is useful
    for context, while the disk itself is the hard physical obstacle that must
    be removed from a planting zone.  Lines and partial arcs are deliberately
    ignored because their footprint cannot be inferred unambiguously.
    """
    disks: dict[tuple[float, float, float], Any] = {}
    ignored_partial_arcs = 0
    ignored_other_primitives = 0
    for record in records:
        raw = record.get("geometry") or {}
        kind = raw.get("kind")
        if kind not in {"arc", "circle"}:
            ignored_other_primitives += 1
            continue
        radius = float(raw.get("radius", 0.0))
        center = raw.get("center")
        if radius <= 0 or not center:
            ignored_other_primitives += 1
            continue
        if kind == "arc":
            start = float(raw.get("start_angle", 0.0))
            end = float(raw.get("end_angle", 0.0))
            sweep = (end - start) % 360.0
            if sweep < UTILITY_WELL_MIN_FULL_CIRCLE_SWEEP_DEG:
                ignored_partial_arcs += 1
                continue
        center_x, center_y = xy(center)
        key = (round(center_x, 6), round(center_y, 6), round(radius, 6))
        disks[key] = Point(center_x, center_y).buffer(radius, quad_segs=16)

    footprint = unary_union(list(disks.values())) if disks else None
    diagnostics = {
        "method": "full_circle_arc_or_circle_footprint",
        "source_symbol_count": len(disks),
        "ignored_partial_arc_count": ignored_partial_arcs,
        "ignored_other_primitive_count": ignored_other_primitives,
        "uses_drawn_radius_without_extra_clearance": True,
    }
    return footprint, diagnostics


def normalize_group(
    object_type: str,
    geometries: list[Any],
):
    if object_type in {"work_boundary", "sidewalk"}:
        return polygonal_geometry_per_primitive(geometries)
    if object_type == "building":
        geometry, _ = polygonal_geometry_with_endpoint_closure(geometries)
        return geometry
    if object_type in POLYGON_TYPES:
        return polygonal_geometry(geometries)
    if object_type in LINE_TYPES:
        return lineal_geometry(geometries)
    if object_type in POINT_TYPES:
        return point_geometry(geometries)
    return None


def make_feature(
    object_type: str,
    geometry: Any,
    source_records: list[dict[str, Any]],
    skipped: int,
    extra_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    layers = sorted({record["source_layer"] for record in source_records})
    properties = {
        "object_type": object_type,
        "coordinate_reference": "local_dxf_coordinates",
        "source_record_count": len(source_records),
        "source_layers": layers,
        "skipped_without_geometry": skipped,
    }
    if extra_properties:
        properties.update(extra_properties)
    return {
        "type": "Feature",
        "id": object_type,
        "properties": properties,
        "geometry": mapping(geometry),
    }


def normalize(
    input_path: Path,
    output_path: Path,
    report_path: Path,
    curve_tolerance: float,
    only_types: set[str] | None,
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records_by_type: Counter[str] = Counter()
    with input_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            object_type = record["object_type"]
            if only_types is not None and object_type not in only_types:
                continue
            grouped[object_type].append(record)
            records_by_type[object_type] += 1

    report: dict[str, Any] = {
        "input": str(input_path),
        "coordinate_reference": "local_dxf_coordinates",
        "curve_tolerance_in_dxf_units": curve_tolerance,
        "object_types": {},
        "warnings": [],
    }
    with output_path.open("w", encoding="utf-8") as destination:
        for object_type in sorted(grouped):
            records = grouped[object_type]
            primitives = []
            skipped = 0
            for record in records:
                geometry = primitive_to_geometry(record, curve_tolerance)
                if geometry is None:
                    skipped += 1
                else:
                    primitives.append(geometry)
            extra_properties: dict[str, Any] = {}
            if object_type == "building":
                normalized, closure_diagnostics = polygonal_geometry_with_endpoint_closure(
                    primitives
                )
                summary_closure_diagnostics = closure_diagnostics
                extra_properties.update(closure_diagnostics)
            else:
                normalized = normalize_group(object_type, primitives)
                summary_closure_diagnostics = None
            summary = {
                "source_records": records_by_type[object_type],
                "converted_primitives": len(primitives),
                "skipped_without_geometry": skipped,
                "result_geometry": normalized.geom_type if normalized else None,
            }
            if summary_closure_diagnostics is not None:
                summary["endpoint_chain_closure"] = summary_closure_diagnostics
            if normalized is None or normalized.is_empty:
                summary["status"] = "no_usable_geometry"
                report["warnings"].append(
                    f"{object_type}: no usable geometry was produced"
                )
            else:
                summary["status"] = "ok"
                destination.write(
                    json.dumps(
                        make_feature(
                            object_type,
                            normalized,
                            records,
                            skipped,
                            extra_properties,
                        ),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                if object_type == "building":
                    source_linework = lineal_geometry(primitives)
                    if source_linework is not None and not source_linework.is_empty:
                        destination.write(
                            json.dumps(
                                make_feature(
                                    "building_linework",
                                    source_linework,
                                    records,
                                    skipped,
                                    {
                                        "role": (
                                            "source_edges_for_setbacks_and_manual_review"
                                        )
                                    },
                                ),
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                if object_type == "utility_well":
                    footprint, footprint_diagnostics = utility_well_footprint(records)
                    summary["physical_footprint"] = footprint_diagnostics
                    if footprint is not None and not footprint.is_empty:
                        destination.write(
                            json.dumps(
                                make_feature(
                                    "utility_well_footprint",
                                    footprint,
                                    records,
                                    skipped,
                                    {
                                        "role": "physical_hard_obstacle",
                                        **footprint_diagnostics,
                                    },
                                ),
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
            report["object_types"][object_type] = summary

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Read: {input_path}")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    for object_type, result in sorted(report["object_types"].items()):
        print(
            f"  {object_type}: {result['source_records']} raw -> "
            f"{result['result_geometry'] or 'no geometry'}"
        )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize extracted DXF primitives into local GeoJSON-like JSONL."
    )
    parser.add_argument("input_jsonl", type=Path, help="JSONL from an extractor")
    parser.add_argument(
        "--output", type=Path, default=Path("normalized_objects.geojsonl")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("normalization_report.json")
    )
    parser.add_argument(
        "--curve-tolerance",
        type=float,
        default=0.1,
        help="Maximum chord length in source DXF units while approximating curves.",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated object types; useful for inspecting one type first.",
    )
    args = parser.parse_args()
    if args.curve_tolerance <= 0:
        raise SystemExit("--curve-tolerance must be greater than zero")
    only_types = (
        {value.strip() for value in args.only.split(",") if value.strip()}
        if args.only
        else None
    )
    normalize(
        args.input_jsonl,
        args.output,
        args.report,
        args.curve_tolerance,
        only_types,
    )


if __name__ == "__main__":
    main()
