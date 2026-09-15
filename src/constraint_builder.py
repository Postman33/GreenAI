"""Build the base planting area from normalized DXF geometry.

The base stage applies restrictions that do not depend on a plant species::

    base_allowed_area = work_boundary - hard_surfaces - road_area - buildings

Network and object setbacks belong to the following, per-plant constraint
stage because their distances come from placement rules.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, TypeAlias

from shapely import Polygon, MultiPolygon, MultiLineString, LineString, box
from shapely.geometry import GeometryCollection, mapping, shape
from shapely.ops import polygonize, unary_union
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


def is_road_surface_layer(layer_name: str) -> bool:
    """Return True for an unambiguous carriageway surface layer.

    Project layer names sometimes describe both sides of a boundary, for
    example ``ПЧ за ТРОТ`` and ``ТРОТ за ПЧ``.  The first material in such a
    name is the area represented by the HATCH, so its position matters.
    """
    normalized = layer_name.casefold()
    if "пч" not in normalized and "проезж" not in normalized:
        return False
    if any(word in normalized for word in PLANTABLE_WORDS):
        return False

    road_position = normalized.find("пч")
    sidewalk_positions = [
        position
        for word in ("тротуар", "трот")
        if (position := normalized.find(word)) >= 0
    ]
    if not sidewalk_positions or road_position < 0:
        return True
    return road_position < min(sidewalk_positions)


def is_sidewalk_partition_layer(layer_name: str) -> bool:
    """Match project HATCH layers whose rings partition sidewalk/road space."""
    return bool(re.search(r"^дв_до_тип.*трот", layer_name.casefold()))


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


def read_surface_area_by_predicate(
        candidates_path: Path,
        work_boundary: Polygon | MultiPolygon,
        predicate: Any,
        curve_tolerance: float = 0.1,
        min_area: float = 0.01,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Read and merge candidate polygons whose layer matches predicate."""
    accepted_parts: list[Polygon] = []
    accepted_by_layer: Counter[str] = Counter()
    matched_records = 0
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
            if not predicate(layer):
                continue
            matched_records += 1

            polygonal = surface_record_polygon(record, curve_tolerance)
            if polygonal is None:
                skipped_invalid += 1
                continue
            clipped = as_polygonal(polygonal.intersection(work_boundary))
            parts = [
                part for part in polygon_parts(clipped)
                if part.area >= min_area
            ]
            if parts:
                accepted_parts.extend(parts)
                accepted_by_layer[layer] += 1

    area = (
        as_polygonal(unary_union(accepted_parts))
        if accepted_parts else Polygon()
    )
    return area, {
        "matched_records": matched_records,
        "accepted_records_by_layer": dict(sorted(accepted_by_layer.items())),
        "skipped_records_without_polygon": skipped_invalid,
        "area_in_dxf_square_units": area.area,
    }


def read_surface_partition_area(
        candidates_path: Path,
        work_boundary: Polygon | MultiPolygon,
        predicate: Any,
        curve_tolerance: float = 0.1,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Build topological faces from all rings of matching surface records.

    This geometry is only a barrier for road-region propagation. It must not
    be interpreted as the actual area of the named surface: intersecting HATCH
    boundaries can create additional faces when polygonized together.
    """
    linework = []
    matched_by_layer: Counter[str] = Counter()
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
            if not predicate(layer):
                continue
            geometry = primitive_to_geometry(record, curve_tolerance)
            if geometry is None or geometry.is_empty:
                continue
            linework.append(geometry)
            matched_by_layer[layer] += 1

    faces = list(polygonize(unary_union(linework))) if linework else []
    area = as_polygonal(unary_union(faces).intersection(work_boundary)) \
        if faces else Polygon()
    return area, {
        "purpose": "topological_barrier_only",
        "matched_records_by_layer": dict(sorted(matched_by_layer.items())),
        "polygonized_faces": len(faces),
        "area_in_dxf_square_units": area.area,
    }


def build_road_area(
        road_edges: LineString | MultiLineString,
        work_boundary: Polygon | MultiPolygon,
        road_seed_area: Polygon | MultiPolygon,
        known_non_road_areas: Iterable[Polygon | MultiPolygon] = (),
        stitch_tolerance: float = 0.30,
        min_seed_overlap_area: float = 0.50,
        min_candidate_area_ratio: float = 0.001,
        max_candidate_area_ratio: float = 0.65,
        fallback_max_component_coverage: float = 0.10,
        fallback_min_face_area_ratio: float = 0.05,
        fallback_seed_distance_factor: float = 1.10,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Reconstruct a carriageway from dashed curb lines and road HATCH seeds.

    The geobase represents curbs as short LINE strokes separated by roughly
    0.5 source units. Buffering every stroke by ``stitch_tolerance`` closes
    those gaps and turns the curb into a barrier. The work area minus those
    barriers is split into cells; only cells touched by an explicit road HATCH
    are retained. Known sidewalks, buildings and planting surfaces stop a cell
    from leaking into a non-road area. If an artificial curb buffer separates
    a road HATCH from the main road cell, a conservative fallback may attach
    one large adjacent cell in an otherwise under-covered work component.

    The tolerances are technical heuristics in source DXF units, not
    regulatory distances. The result must therefore remain visible in debug
    output and be confirmed in CAD.
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
    if road_seed_area.is_empty:
        raise ValueError("No explicit road-surface HATCH is available as a seed")
    if stitch_tolerance <= 0:
        raise ValueError("stitch_tolerance must be greater than zero")
    if min_seed_overlap_area <= 0:
        raise ValueError("min_seed_overlap_area must be greater than zero")
    if not 0 <= min_candidate_area_ratio <= max_candidate_area_ratio <= 1:
        raise ValueError("Road candidate area ratios are invalid")
    if not 0 <= fallback_max_component_coverage <= 1:
        raise ValueError("fallback_max_component_coverage must be between 0 and 1")
    if not 0 <= fallback_min_face_area_ratio <= 1:
        raise ValueError("fallback_min_face_area_ratio must be between 0 and 1")
    if fallback_seed_distance_factor <= 0:
        raise ValueError("fallback_seed_distance_factor must be greater than zero")

    clipped_edges = road_edges.intersection(
        work_boundary.buffer(stitch_tolerance)
    )
    edge_barrier = as_polygonal(
        clipped_edges.buffer(
            stitch_tolerance,
            cap_style="square",
            join_style="round",
            quad_segs=4,
        ).intersection(work_boundary)
    )

    non_road_parts = [
        geometry.intersection(work_boundary)
        for geometry in known_non_road_areas
        if not geometry.is_empty
    ]
    known_non_road = as_polygonal(unary_union(non_road_parts)) \
        if non_road_parts else Polygon()

    blocked_area = as_polygonal(unary_union([edge_barrier, known_non_road]))
    cells = as_polygonal(work_boundary.difference(blocked_area))
    accepted_faces: list[Polygon] = []
    rejected_faces = 0
    seed_overlap_area = 0.0
    clipped_seed = as_polygonal(road_seed_area.intersection(work_boundary))
    for face in polygon_parts(cells):
        overlap_area = face.intersection(clipped_seed).area
        if overlap_area < min_seed_overlap_area:
            rejected_faces += 1
            continue
        accepted_faces.append(face)
        seed_overlap_area += overlap_area

    if not accepted_faces:
        raise ValueError("Could not build any candidate road polygons")

    fallback_faces: list[Polygon] = []
    fallback_components: list[dict[str, Any]] = []
    initially_selected = as_polygonal(unary_union(accepted_faces))
    max_seed_distance = stitch_tolerance * fallback_seed_distance_factor
    for component_index, component in enumerate(
        polygon_parts(work_boundary), start=1
    ):
        component_seed = clipped_seed.intersection(component)
        if component_seed.area < min_seed_overlap_area:
            continue
        initial_coverage = initially_selected.intersection(component).area / component.area
        if initial_coverage >= fallback_max_component_coverage:
            continue

        candidates: list[tuple[float, float, Polygon]] = []
        for face in polygon_parts(cells):
            candidate_geometry = as_polygonal(face.intersection(component))
            for candidate in polygon_parts(candidate_geometry):
                if candidate.intersection(component_seed).area >= min_seed_overlap_area:
                    continue
                face_area_ratio = candidate.area / component.area
                if face_area_ratio < fallback_min_face_area_ratio:
                    continue
                seed_distance = candidate.distance(component_seed)
                if seed_distance <= max_seed_distance:
                    candidates.append((candidate.area, seed_distance, candidate))

        if not candidates:
            continue
        area, seed_distance, fallback_face = max(candidates, key=lambda item: item[0])
        fallback_faces.append(fallback_face)
        accepted_faces.append(fallback_face)
        fallback_components.append({
            "component_index": component_index,
            "work_component_area_in_dxf_square_units": component.area,
            "initial_road_coverage_ratio": initial_coverage,
            "selected_face_area_in_dxf_square_units": area,
            "selected_face_area_ratio": area / component.area,
            "distance_to_road_seed_in_dxf_units": seed_distance,
        })

    inferred_core = as_polygonal(unary_union(accepted_faces))
    # Restore the narrow strip occupied by the artificial curb barrier. The
    # explicit seed is authoritative and is retained even where CAD surfaces
    # overlap slightly at their boundaries.
    inferred_to_curb = as_polygonal(
        inferred_core.buffer(
            stitch_tolerance,
            join_style="round",
            quad_segs=4,
        ).intersection(work_boundary).difference(known_non_road)
    )
    road_area = make_valid(unary_union([inferred_to_curb, clipped_seed]))
    if road_area.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(
            "Road reconstruction produced unsupported geometry type "
            f"{road_area.geom_type}"
        )

    diagnostics = {
        "method": "curb_barrier_cells_selected_by_road_hatch",
        "all_cells": sum(1 for _ in polygon_parts(cells)),
        "accepted_cells": len(accepted_faces),
        "seed_intersecting_cells": len(accepted_faces) - len(fallback_faces),
        "fallback_cells": len(fallback_faces),
        "fallback_components": fallback_components,
        "rejected_cells_without_seed": rejected_faces - len(fallback_faces),
        "curb_barrier_area_in_dxf_square_units": edge_barrier.area,
        "road_seed_area_in_dxf_square_units": clipped_seed.area,
        "road_seed_overlap_with_selected_cells": seed_overlap_area,
        "known_non_road_area_in_dxf_square_units": known_non_road.area,
        "stitch_tolerance_in_dxf_units": stitch_tolerance,
        "min_seed_overlap_area_in_dxf_square_units": min_seed_overlap_area,
        "fallback_max_component_coverage": fallback_max_component_coverage,
        "fallback_min_face_area_ratio": fallback_min_face_area_ratio,
        "fallback_max_seed_distance_in_dxf_units": max_seed_distance,
        "candidate_area_ratio": road_area.area / work_boundary.area,
        "min_candidate_area_ratio": min_candidate_area_ratio,
        "max_candidate_area_ratio": max_candidate_area_ratio,
        "requires_visual_confirmation": True,
    }
    if not (
        min_candidate_area_ratio
        <= diagnostics["candidate_area_ratio"]
        <= max_candidate_area_ratio
    ):
        raise ValueError(
            "Road reconstruction produced an implausible area ratio: "
            f"{diagnostics['candidate_area_ratio']:.6f}"
        )
    return road_area, diagnostics


def recover_outer_terminal_road(
        strict_road: Polygon | MultiPolygon,
        relaxed_road: Polygon | MultiPolygon,
        work_boundary: Polygon | MultiPolygon,
        terminal_depth: float = 60.0,
        endpoint_tolerance: float = 1.0,
        min_extension_area_ratio: float = 0.001,
        max_extension_area_ratio: float = 0.20,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Recover road pieces hidden by a soft surface-partition barrier.

    Some project HATCH boundaries create false filled faces when used as a
    topological partition. A second, relaxed reconstruction omits that soft
    barrier. Each disconnected work-boundary polygon has its own terminal
    ends. Only pieces reaching one of those local ends, touching the strict
    road in the same component and having a controlled area are restored.
    This prevents a gap between work components from hiding a real road end.
    """
    if terminal_depth <= 0 or endpoint_tolerance < 0:
        raise ValueError("Terminal recovery tolerances are invalid")
    if not 0 <= min_extension_area_ratio <= max_extension_area_ratio <= 1:
        raise ValueError("Terminal extension area ratios are invalid")

    extensions: list[Polygon] = []
    extension_details: list[dict[str, Any]] = []
    relaxed_only = as_polygonal(relaxed_road.difference(strict_road))
    component_details: list[dict[str, Any]] = []
    for component_index, component in enumerate(
        polygon_parts(work_boundary), start=1
    ):
        minx, miny, maxx, maxy = component.bounds
        vertical = (maxy - miny) >= (maxx - minx)
        component_length = (maxy - miny) if vertical else (maxx - minx)
        depth = min(terminal_depth, component_length * 0.15)
        if vertical:
            terminal_bands = (
                box(minx - 1, miny - 1, maxx + 1, miny + depth),
                box(minx - 1, maxy - depth, maxx + 1, maxy + 1),
            )
        else:
            terminal_bands = (
                box(minx - 1, miny - 1, minx + depth, maxy + 1),
                box(maxx - depth, miny - 1, maxx + 1, maxy + 1),
            )

        strict_component = strict_road.intersection(component)
        selected_for_component = 0
        component_relaxed_only = as_polygonal(
            relaxed_only.intersection(component)
        )
        for candidate in polygon_parts(component_relaxed_only):
            area_ratio = candidate.area / component.area
            if not (
                min_extension_area_ratio
                <= area_ratio
                <= max_extension_area_ratio
            ):
                continue
            if candidate.distance(strict_component) > 1e-6:
                continue
            if not any(candidate.intersects(band) for band in terminal_bands):
                continue
            bounds = candidate.bounds
            reaches_outer_endpoint = (
                bounds[1] <= miny + endpoint_tolerance
                or bounds[3] >= maxy - endpoint_tolerance
            ) if vertical else (
                bounds[0] <= minx + endpoint_tolerance
                or bounds[2] >= maxx - endpoint_tolerance
            )
            if not reaches_outer_endpoint:
                continue
            extensions.append(candidate)
            selected_for_component += 1
            extension_details.append({
                "work_component_index": component_index,
                "orientation": "vertical" if vertical else "horizontal",
                "area_in_dxf_square_units": candidate.area,
                "area_ratio_of_work_component": area_ratio,
                "bounds": list(bounds),
            })
        component_details.append({
            "work_component_index": component_index,
            "orientation": "vertical" if vertical else "horizontal",
            "bounds": list(component.bounds),
            "terminal_depth_in_dxf_units": depth,
            "selected_extension_count": selected_for_component,
        })

    recovered = as_polygonal(unary_union([strict_road, *extensions]))
    return recovered, {
        "method": "outer_terminal_extension_from_relaxed_partition",
        "orientation": "per_work_component",
        "work_components": component_details,
        "endpoint_tolerance_in_dxf_units": endpoint_tolerance,
        "strict_road_area_in_dxf_square_units": strict_road.area,
        "relaxed_road_area_in_dxf_square_units": relaxed_road.area,
        "selected_extensions": extension_details,
        "selected_extension_count": len(extensions),
        "added_area_in_dxf_square_units": recovered.difference(strict_road).area,
        "result_area_in_dxf_square_units": recovered.area,
    }


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
    road_stitch_tolerance: float = 0.30,
    road_min_seed_overlap_area: float = 0.50,
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

    road_seed_area, road_seed_diagnostics = read_surface_area_by_predicate(
        surface_candidates_path,
        work_boundary,
        is_road_surface_layer,
        curve_tolerance,
        min_area,
    )
    plantable_surface_area, plantable_surface_diagnostics = (
        read_surface_area_by_predicate(
            surface_candidates_path,
            work_boundary,
            lambda layer: classify_surface_layer(layer) == "plantable_candidate",
            curve_tolerance,
            min_area,
        )
    )
    sidewalk_partition_area, sidewalk_partition_diagnostics = (
        read_surface_partition_area(
            surface_candidates_path,
            work_boundary,
            is_sidewalk_partition_layer,
            curve_tolerance,
        )
    )

    road_area: Polygon | MultiPolygon = Polygon()
    sidewalks: Polygon | MultiPolygon = Polygon()
    road_reconstruction: dict[str, Any]
    try:
        road_edges = read_object_geometry(normalized_path, "road_edge")
        try:
            sidewalks = as_polygonal(
                read_object_geometry(normalized_path, "sidewalk").intersection(
                    work_boundary
                )
            )
        except ValueError:
            sidewalks = Polygon()
        road_area, road_reconstruction = build_road_area(
            road_edges,
            work_boundary,
            road_seed_area,
            known_non_road_areas=(
                buildings_in_work_area,
                sidewalks,
                sidewalk_partition_area,
                plantable_surface_area,
            ),
            stitch_tolerance=road_stitch_tolerance,
            min_seed_overlap_area=road_min_seed_overlap_area,
        )
        relaxed_road_area, _relaxed_diagnostics = build_road_area(
            road_edges,
            work_boundary,
            road_seed_area,
            known_non_road_areas=(
                buildings_in_work_area,
                sidewalks,
                plantable_surface_area,
            ),
            stitch_tolerance=road_stitch_tolerance,
            min_seed_overlap_area=road_min_seed_overlap_area,
        )
        road_area, terminal_recovery = recover_outer_terminal_road(
            road_area,
            relaxed_road_area,
            work_boundary,
        )
        road_reconstruction["terminal_recovery"] = terminal_recovery
        road_reconstruction["candidate_area_ratio"] = (
            road_area.area / work_boundary.area
        )
        road_reconstruction["status"] = "reconstructed"
        road_reconstruction["non_road_inputs"] = [
            "building",
            "sidewalk",
            "sidewalk_boundary_partition",
            "unambiguous_plantable_surface",
        ]
    except ValueError as error:
        # Explicit road HATCH polygons still remain part of hard_surface_area.
        # Reconstruction is an enhancement and must not make the base stage
        # unusable for a DXF that lacks suitable curb or seed geometry.
        road_reconstruction = {
            "status": "unavailable",
            "reason": str(error),
            "requires_visual_confirmation": True,
        }

    absolute_exclusions = as_polygonal(
        unary_union([
            hard_surface_area,
            road_area,
            buildings_in_work_area,
        ])
    )
    base_allowed_area = as_polygonal(
        work_boundary.difference(absolute_exclusions)
    )
    if base_allowed_area.is_empty:
        raise ValueError(
            "work_boundary - hard_surface_area - road_area - buildings "
            "produced an empty geometry"
        )

    output_features = [
        geometry_feature(
            "base_allowed_area",
            base_allowed_area,
            {
                "stage": "base_constraint_builder",
                "formula": (
                    "work_boundary - hard_surface_area - road_area - "
                    "buildings_in_work_area"
                ),
                "applied_restrictions": [
                    "hard_surface_area",
                    "reconstructed_road_area",
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
    if not road_area.is_empty:
        output_features.append(
            geometry_feature(
                "road_area",
                road_area,
                {
                    "stage": "base_constraint_builder",
                    "source_object_type": "road_edge",
                    "road_seed_source": str(surface_candidates_path),
                    "reconstruction_method": road_reconstruction.get("method"),
                    "requires_visual_confirmation": True,
                },
            )
        )
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
    if road_reconstruction["status"] != "reconstructed":
        warnings.append(
            "Road reconstruction was unavailable: "
            f"{road_reconstruction.get('reason', 'unknown reason')}. "
            "Only explicit road-surface polygons remain excluded."
        )
    else:
        warnings.append(
            "Road area was reconstructed heuristically from dashed curb lines "
            "and must be visually confirmed in CAD."
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
        "formula": (
            "work_boundary - hard_surface_area - road_area - "
            "buildings_in_work_area"
        ),
        "areas_in_dxf_square_units": {
            "work_boundary": work_boundary.area,
            "hard_surface_area": hard_surface_area.area,
            "road_seed_area": road_seed_area.area,
            "reconstructed_road_area": road_area.area,
            "unambiguous_plantable_surface_area": plantable_surface_area.area,
            "sidewalk_area": sidewalks.area,
            "sidewalk_partition_barrier_area": sidewalk_partition_area.area,
            "all_normalized_buildings": all_buildings.area,
            "buildings_in_work_area": buildings_in_work_area.area,
            "absolute_exclusions": excluded_area,
            "base_allowed_area": base_allowed_area.area,
            "area_balance_error": area_balance_error,
        },
        "surface_detection": surface_diagnostics,
        "road_seed_detection": road_seed_diagnostics,
        "plantable_surface_detection": plantable_surface_diagnostics,
        "sidewalk_partition": sidewalk_partition_diagnostics,
        "road_reconstruction": road_reconstruction,
        "applied_restrictions": [
            "hard_surface_area",
            "reconstructed_road_area",
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
    print(f"Road seed area: {road_seed_area.area:.3f} square DXF units")
    print(f"Road area: {road_area.area:.3f} square DXF units")
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
    parser.add_argument(
        "--road-stitch-tolerance",
        type=float,
        default=0.30,
        help="Half-width used to connect dashed curb strokes in DXF units",
    )
    parser.add_argument(
        "--road-min-seed-overlap-area",
        type=float,
        default=0.50,
        help="Minimum road-HATCH overlap required to classify a cell",
    )
    args = parser.parse_args()
    if (
        args.curve_tolerance <= 0
        or args.min_area < 0
        or args.road_stitch_tolerance <= 0
        or args.road_min_seed_overlap_area <= 0
    ):
        raise SystemExit("Tolerance must be positive and minimum area non-negative")
    try:
        build(
            args.normalized_geojsonl,
            args.surface_candidates_jsonl,
            args.output,
            args.report,
            args.curve_tolerance,
            args.min_area,
            args.road_stitch_tolerance,
            args.road_min_seed_overlap_area,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Constraint builder error: {error}") from error


if __name__ == "__main__":
    main()
