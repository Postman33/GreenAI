"""Build the base planting area from normalized DXF geometry.

The base stage applies restrictions that do not depend on a plant species::

    base_allowed_area = work_boundary - hard_surfaces - buildings

Network and object setbacks belong to the following, per-plant constraint
stage because their distances come from placement rules.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, TypeAlias

from shapely import Polygon, MultiPolygon, MultiLineString, LineString
from shapely.geometry import GeometryCollection, mapping, shape
from shapely.ops import polygonize, polygonize_full, snap, unary_union
from shapely.validation import make_valid

from normalizer import primitive_to_geometry

ObjectGeometry: TypeAlias = (
        Polygon | MultiPolygon | LineString | MultiLineString
)

SUPPORTED_GEOMETRY_TYPES = {
    "Polygon",
    "MultiPolygon",
    "LineString",
    "MultiLineString",
}

PLANTABLE_WORDS = (
    "газон", "грунт", "растител", "озелен", "цветник", "клумб", "почв",
)
HARD_SURFACE_WORDS = (
    "асфальт", "покрыт", "тротуар", "трот", "плитк", "проезж", "дорог", "проезд",
    "пч", "щебень", "лестниц", "пандус",
)
REFERENCE_WORDS = (
    "граница работ", "красные линии", "борт", "бордюр", "оград",
)


def read_object_geometry(
        path: Path,
        object_type: str,
) -> ObjectGeometry:
    """Read and merge all geometries with the requested object type."""

    geometries = []

    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue

            feature: dict[str, Any] = json.loads(line)

            if feature.get("type") != "Feature":
                raise ValueError(
                    f"Line {line_number}: expected GeoJSON Feature"
                )

            actual_object_type = feature.get(
                "properties", {}
            ).get("object_type")

            if actual_object_type != object_type:
                continue

            geometry = make_valid(shape(feature["geometry"]))

            if geometry.geom_type not in SUPPORTED_GEOMETRY_TYPES:
                raise ValueError(
                    f"Line {line_number}: {object_type} has unsupported "
                    f"geometry type {geometry.geom_type}"
                )

            if not geometry.is_empty:
                geometries.append(geometry)

    if not geometries:
        raise ValueError(
            f"No usable geometry found for object type: {object_type}"
        )

    result = unary_union(geometries)

    if result.geom_type not in SUPPORTED_GEOMETRY_TYPES:
        raise ValueError(
            f"Merging {object_type} produced unsupported "
            f"geometry type {result.geom_type}"
        )

    return result


def read_work_boundary(path: Path) -> Polygon | MultiPolygon:
    """Read every work-boundary feature and merge its polygonal components."""
    boundaries = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            feature: dict[str, Any] = json.loads(line)
            if feature.get("type") != "Feature":
                raise ValueError(f"Line {line_number}: expected GeoJSON Feature")
            if feature.get("properties", {}).get("object_type") != "work_boundary":
                continue
            geometry = shape(feature["geometry"])
            if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError(
                    f"Line {line_number}: work_boundary must be Polygon or MultiPolygon, "
                    f"got {geometry.geom_type}"
                )
            boundaries.append(make_valid(geometry))

    if not boundaries:
        raise ValueError("No polygonal work_boundary feature was found")
    return unary_union(boundaries)


def polygon_parts(geometry: Any) -> Iterable[Polygon]:
    """Yield polygon components and ignore line/point collection members."""
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def as_polygonal(geometry: Any) -> Polygon | MultiPolygon:
    """Validate a result and retain only its polygonal components."""
    parts = list(polygon_parts(make_valid(geometry)))
    if not parts:
        return Polygon()
    return unary_union(parts)


def readable_layer_name(value: str) -> str:
    """Repair UTF-8 layer names that were accidentally decoded as CP1251."""
    if "Р" not in value and "С" not in value:
        return value
    try:
        repaired = value.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired if repaired else value


def classify_surface_layer(layer_name: str) -> str:
    """Classify a surface by its CAD layer name, conservatively."""
    normalized = layer_name.casefold()
    if any(word in normalized for word in REFERENCE_WORDS):
        return "reference_geometry"
    plantable = any(word in normalized for word in PLANTABLE_WORDS)
    hard_surface = any(word in normalized for word in HARD_SURFACE_WORDS)
    if plantable and hard_surface:
        return "ambiguous"
    if hard_surface:
        return "hard_surface"
    if plantable:
        return "plantable_candidate"
    return "unknown"


def surface_record_polygon(
    record: dict[str, Any],
    curve_tolerance: float,
) -> Polygon | MultiPolygon | None:
    """Convert a closed LWPOLYLINE/HATCH record into polygonal geometry."""
    raw = record.get("geometry") or {}
    if raw.get("kind") == "polyline" and not raw.get("closed"):
        return None

    geometry = primitive_to_geometry(record, curve_tolerance)
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, (Polygon, MultiPolygon)):
        polygonal = geometry
    else:
        faces = list(polygonize(geometry))
        if not faces:
            return None
        polygonal = unary_union(faces)
    result = as_polygonal(polygonal)
    return result if not result.is_empty else None


def read_hard_surface_area(
    candidates_path: Path,
    work_boundary: Polygon | MultiPolygon,
    curve_tolerance: float = 0.1,
    min_area: float = 0.01,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Read and merge unambiguous hard-surface polygons inside the work area."""
    polygons: list[Polygon] = []
    records_by_class: Counter[str] = Counter()
    accepted_by_layer: Counter[str] = Counter()
    skipped_invalid = 0

    with candidates_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid surface-candidate JSON"
                ) from error

            raw_layer = record.get("source_layer", "0")
            layer = readable_layer_name(
                record.get("source_layer_tail", raw_layer)
            )
            surface_class = classify_surface_layer(layer)
            records_by_class[surface_class] += 1
            if surface_class != "hard_surface":
                continue

            polygonal = surface_record_polygon(record, curve_tolerance)
            if polygonal is None:
                skipped_invalid += 1
                continue
            clipped = as_polygonal(polygonal.intersection(work_boundary))
            accepted_parts = [
                part for part in polygon_parts(clipped) if part.area >= min_area
            ]
            if accepted_parts:
                polygons.extend(accepted_parts)
                accepted_by_layer[layer] += 1

    hard_surface_area = (
        as_polygonal(unary_union(polygons)) if polygons else Polygon()
    )
    diagnostics = {
        "selection_method": "conservative_layer_name_classification",
        "records_by_class": dict(sorted(records_by_class.items())),
        "accepted_records_by_layer": dict(sorted(accepted_by_layer.items())),
        "skipped_hard_surface_records_without_polygon": skipped_invalid,
        "ambiguous_layers_included": False,
        "requires_visual_confirmation": True,
    }
    return hard_surface_area, diagnostics


def build_road_area(
        road_edges: LineString | MultiLineString,
        work_boundary: Polygon | MultiPolygon,
        known_non_road_areas: Iterable[Polygon | MultiPolygon] = (),
        snap_tolerance: float = 0.05,
        max_non_road_overlap_ratio: float = 0.05,
        min_candidate_area_ratio: float = 0.001,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Build candidate road polygons from curb linework.

    The result is a geometric hypothesis. Curb lines also surround blocks,
    islands and landscaping, so known buildings, sidewalks and vegetation are
    used to reject faces that are likely not carriageway. The two tolerances
    are technical heuristics in source DXF units, not regulatory distances.
    """
    if road_edges.geom_type not in {"LineString", "MultiLineString"}:
        raise ValueError(
            "road_edges must be LineString or MultiLineString, "
            f"got {road_edges.geom_type}"
        )
    if work_boundary.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(
            "work_boundary must be Polygon or MultiPolygon, "
            f"got {work_boundary.geom_type}"
        )
    if snap_tolerance <= 0:
        raise ValueError("snap_tolerance must be greater than zero")
    if not 0 <= max_non_road_overlap_ratio <= 1:
        raise ValueError("max_non_road_overlap_ratio must be between 0 and 1")
    if not 0 <= min_candidate_area_ratio <= 1:
        raise ValueError("min_candidate_area_ratio must be between 0 and 1")

    # Keep nearby outside segments: a curb just beyond the work boundary can
    # still be needed to close a face at the edge of the project territory.
    clipped_edges = road_edges.intersection(
        work_boundary.buffer(snap_tolerance)
    )
    linework = unary_union([
        clipped_edges,
        work_boundary.boundary,
    ])
    linework = snap(linework, linework, snap_tolerance)
    linework = unary_union(linework)

    polygons, cuts, dangles, invalid_rings = polygonize_full(linework)
    non_road_geometries = [
        geometry.intersection(work_boundary)
        for geometry in known_non_road_areas
        if not geometry.is_empty
    ]
    known_non_road = (
        unary_union(non_road_geometries)
        if non_road_geometries
        else None
    )

    accepted_faces = []
    rejected_faces = 0
    for face in polygons.geoms:
        face = face.intersection(work_boundary)
        if face.is_empty or face.area == 0:
            continue
        overlap_ratio = 0.0
        if known_non_road is not None:
            overlap_ratio = face.intersection(known_non_road).area / face.area
        if overlap_ratio > max_non_road_overlap_ratio:
            rejected_faces += 1
            continue
        accepted_faces.append(face)

    if not accepted_faces:
        raise ValueError("Could not build any candidate road polygons")

    road_area = make_valid(unary_union(accepted_faces))
    if road_area.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(
            "Road reconstruction produced unsupported geometry type "
            f"{road_area.geom_type}"
        )

    diagnostics = {
        "candidate_faces": len(accepted_faces),
        "rejected_non_road_faces": rejected_faces,
        "cuts": len(cuts.geoms),
        "dangles": len(dangles.geoms),
        "invalid_rings": len(invalid_rings.geoms),
        "snap_tolerance_in_dxf_units": snap_tolerance,
        "max_non_road_overlap_ratio": max_non_road_overlap_ratio,
        "candidate_area_ratio": road_area.area / work_boundary.area,
        "min_candidate_area_ratio": min_candidate_area_ratio,
        "requires_visual_confirmation": True,
    }
    if diagnostics["candidate_area_ratio"] < min_candidate_area_ratio:
        raise ValueError(
            "Curb linework did not produce a plausible road area: "
            f"{diagnostics['candidate_area_ratio']:.6f} of work area, "
            f"{diagnostics['dangles']} dangling line fragments"
        )
    return road_area, diagnostics


def geometry_feature(
    object_type: str,
    geometry: Polygon | MultiPolygon,
    properties: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": object_type,
        "properties": {
            "object_type": object_type,
            "coordinate_reference": "local_dxf_coordinates",
            "area_in_dxf_square_units": geometry.area,
            **properties,
        },
        "geometry": mapping(geometry),
    }


def build(
    normalized_path: Path,
    surface_candidates_path: Path,
    output_path: Path,
    report_path: Path,
    curve_tolerance: float = 0.1,
    min_area: float = 0.01,
) -> None:
    """Calculate and persist the common base area for all plant types."""
    work_boundary = as_polygonal(
        read_object_geometry(normalized_path, "work_boundary")
    )
    if work_boundary.is_empty:
        raise ValueError("work_boundary is empty after validation")

    all_buildings = as_polygonal(
        read_object_geometry(normalized_path, "building")
    )
    buildings_in_work_area = as_polygonal(
        all_buildings.intersection(work_boundary)
    )
    hard_surface_area, surface_diagnostics = read_hard_surface_area(
        surface_candidates_path,
        work_boundary,
        curve_tolerance,
        min_area,
    )

    absolute_exclusions = as_polygonal(
        unary_union([hard_surface_area, buildings_in_work_area])
    )
    base_allowed_area = as_polygonal(
        work_boundary.difference(absolute_exclusions)
    )
    if base_allowed_area.is_empty:
        raise ValueError(
            "work_boundary - hard_surface_area - buildings produced an empty geometry"
        )

    output_features = [
        geometry_feature(
            "base_allowed_area",
            base_allowed_area,
            {
                "stage": "base_constraint_builder",
                "formula": (
                    "work_boundary - hard_surface_area - buildings_in_work_area"
                ),
                "applied_restrictions": [
                    "hard_surface_area",
                    "building_footprints",
                ],
            },
        ),
        geometry_feature(
            "hard_surface_area",
            hard_surface_area,
            {
                "stage": "base_constraint_builder",
                "source": str(surface_candidates_path),
                "requires_visual_confirmation": True,
            },
        ),
    ]
    if not buildings_in_work_area.is_empty:
        output_features.append(
            geometry_feature(
                "buildings_in_work_area",
                buildings_in_work_area,
                {
                    "stage": "base_constraint_builder",
                    "source_object_type": "building",
                },
            )
        )

    output_path.write_text(
        "".join(
            json.dumps(feature, ensure_ascii=False) + "\n"
            for feature in output_features
        ),
        encoding="utf-8",
    )

    warnings = []
    if hard_surface_area.is_empty:
        warnings.append(
            "No unambiguous hard-surface polygon was found; base area still "
            "contains paved surfaces."
        )
    if buildings_in_work_area.is_empty:
        warnings.append(
            "No normalized building polygon intersects the work boundary. "
            "Building footprints therefore did not reduce the base area; "
            "later setback buffers may still reach into it."
        )

    excluded_area = as_polygonal(
        absolute_exclusions.intersection(work_boundary)
    ).area
    area_balance_error = abs(
        work_boundary.area - base_allowed_area.area - excluded_area
    )
    report = {
        "normalized_input": str(normalized_path),
        "surface_candidates_input": str(surface_candidates_path),
        "output": str(output_path),
        "coordinate_reference": "local_dxf_coordinates",
        "units_confirmed_as_metres": False,
        "formula": "work_boundary - hard_surface_area - buildings_in_work_area",
        "areas_in_dxf_square_units": {
            "work_boundary": work_boundary.area,
            "hard_surface_area": hard_surface_area.area,
            "all_normalized_buildings": all_buildings.area,
            "buildings_in_work_area": buildings_in_work_area.area,
            "absolute_exclusions": excluded_area,
            "base_allowed_area": base_allowed_area.area,
            "area_balance_error": area_balance_error,
        },
        "surface_detection": surface_diagnostics,
        "applied_restrictions": [
            "hard_surface_area",
            "building_footprints",
        ],
        "deferred_restrictions": [
            "building_setbacks",
            "utility_setbacks",
            "existing_tree_spacing",
            "overhead_power_line_protection_zones",
        ],
        "warnings": warnings,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Normalized input: {normalized_path}")
    print(f"Surface candidates: {surface_candidates_path}")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    print(f"Work boundary area: {work_boundary.area:.3f} square DXF units")
    print(f"Hard surface area: {hard_surface_area.area:.3f} square DXF units")
    print(
        "Buildings in work area: "
        f"{buildings_in_work_area.area:.3f} square DXF units"
    )
    print(f"Base allowed area: {base_allowed_area.area:.3f} square DXF units")
    for warning in warnings:
        print(f"WARNING: {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build base allowed area by subtracting hard surfaces and buildings."
        )
    )
    parser.add_argument("normalized_geojsonl", type=Path)
    parser.add_argument("surface_candidates_jsonl", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("constraint_map.geojsonl")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("constraint_report.json")
    )
    parser.add_argument("--curve-tolerance", type=float, default=0.1)
    parser.add_argument("--min-area", type=float, default=0.01)
    args = parser.parse_args()
    if args.curve_tolerance <= 0 or args.min_area < 0:
        raise SystemExit("Tolerance must be positive and minimum area non-negative")
    try:
        build(
            args.normalized_geojsonl,
            args.surface_candidates_jsonl,
            args.output,
            args.report,
            args.curve_tolerance,
            args.min_area,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Constraint builder error: {error}") from error


if __name__ == "__main__":
    main()
