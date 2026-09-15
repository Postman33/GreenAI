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

from shapely.geometry import LineString, MultiLineString, MultiPoint, Point, mapping
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
POINT_TYPES = {"existing_tree", "utility_marker"}


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


def normalize_group(object_type: str, geometries: list[Any]):
    if object_type in {"work_boundary", "sidewalk"}:
        return polygonal_geometry_per_primitive(geometries)
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
) -> dict[str, Any]:
    layers = sorted({record["source_layer"] for record in source_records})
    return {
        "type": "Feature",
        "id": object_type,
        "properties": {
            "object_type": object_type,
            "coordinate_reference": "local_dxf_coordinates",
            "source_record_count": len(source_records),
            "source_layers": layers,
            "skipped_without_geometry": skipped,
        },
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
            normalized = normalize_group(object_type, primitives)
            summary = {
                "source_records": records_by_type[object_type],
                "converted_primitives": len(primitives),
                "skipped_without_geometry": skipped,
                "result_geometry": normalized.geom_type if normalized else None,
            }
            if normalized is None or normalized.is_empty:
                summary["status"] = "no_usable_geometry"
                report["warnings"].append(
                    f"{object_type}: no usable geometry was produced"
                )
            else:
                summary["status"] = "ok"
                destination.write(
                    json.dumps(
                        make_feature(object_type, normalized, records, skipped),
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
    normalize(args.input_jsonl, args.output, args.report, args.curve_tolerance, only_types)


if __name__ == "__main__":
    main()
