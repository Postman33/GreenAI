"""Build the base planting area from normalized DXF geometry.

The base stage applies restrictions that do not depend on a plant species::

    base_allowed_area = confirmed_plantable_surfaces
        - sidewalks - hard_surfaces - road_area - buildings

Network and object setbacks belong to the following, per-plant constraint
stage because their distances come from placement rules.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, TypeAlias

from shapely import Polygon, MultiPolygon, MultiLineString, LineString, box
from shapely.geometry import GeometryCollection, mapping, shape
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

from core.heat_chamber_detector import detect_dashed_square_chambers
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


def is_project_sidewalk_surface_layer(layer_name: str) -> bool:
    """Recognize the proposed sidewalk, before the former surface in its name.

    In these project layers ``за счет Газона`` describes what the sidewalk
    replaces; it does not make the HATCH a planting surface.
    """
    return bool(re.search(
        r"^дв_пп_тип[567]_[ур][ _]тр(?:_|$)", layer_name.casefold()
    ))


def is_road_surface_layer(layer_name: str) -> bool:
    """Return True for an unambiguous carriageway surface layer.

    Project layer names sometimes describe both sides of a boundary, for
    example ``ПЧ за ТРОТ`` and ``ТРОТ за ПЧ``.  The first material in such a
    name is the area represented by the HATCH, so its position matters.
    """
    normalized = layer_name.casefold()
    if is_project_sidewalk_surface_layer(layer_name):
        return False
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
    return (
        is_project_sidewalk_surface_layer(layer_name)
        or bool(re.search(r"^дв_до_тип.*трот", layer_name.casefold()))
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


def infer_heat_chamber_footprints(
    heat_lines: ObjectGeometry,
    well_footprints: Polygon | MultiPolygon,
    units_per_meter: float,
) -> tuple[Polygon | MultiPolygon, Polygon | MultiPolygon, dict[str, Any]]:
    """Find square heat-network cells and separate unconfirmed examples.

    A pipe line alone must never turn an arbitrary enclosed lawn into a hard
    obstacle. The well footprint provides independent evidence that the cell
    is utility equipment rather than an accidental loop in the linework.
    """
    if heat_lines.is_empty:
        return Polygon(), Polygon(), {
            "candidate_faces": 0, "square_faces": 0,
            "accepted_faces": 0, "review_faces": 0,
        }

    min_area = 4.0 * units_per_meter**2
    max_area = 100.0 * units_per_meter**2
    max_side = 20.0 * units_per_meter
    min_side = 2.0 * units_per_meter
    max_aspect_ratio = 1.6
    min_rectangularity = 0.85
    min_well_overlap = 0.2 * units_per_meter**2
    accepted = []
    review = []
    face_count = 0
    square_count = 0
    for face in polygonize(unary_union(heat_lines)):
        face_count += 1
        if not min_area <= face.area <= max_area:
            continue
        rectangle = face.minimum_rotated_rectangle
        if rectangle.area <= 0:
            continue
        corners = list(rectangle.exterior.coords)
        sides = [
            math.hypot(
                corners[index + 1][0] - corners[index][0],
                corners[index + 1][1] - corners[index][1],
            )
            for index in range(4)
        ]
        short_side, long_side = min(sides), max(sides)
        if short_side < min_side or long_side > max_side:
            continue
        if long_side / short_side > max_aspect_ratio:
            continue
        if face.area / rectangle.area < min_rectangularity:
            continue
        square_count += 1
        overlap = face.intersection(well_footprints).area
        if overlap < min_well_overlap or overlap / face.area < 0.01:
            review.append(face)
            continue
        accepted.append(face)
    return (
        as_polygonal(unary_union(accepted)) if accepted else Polygon(),
        as_polygonal(unary_union(review)) if review else Polygon(),
        {
            "candidate_faces": face_count,
            "square_faces": square_count,
            "accepted_faces": len(accepted),
            "review_faces": len(review),
            "max_aspect_ratio": max_aspect_ratio,
            "min_rectangularity": min_rectangularity,
        },
    )


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
    if "граница покрыт" in normalized:
        return "reference_geometry"
    if any(word in normalized for word in REFERENCE_WORDS):
        return "reference_geometry"
    if is_project_sidewalk_surface_layer(layer_name):
        return "hard_surface"
    # In names like "ТРТ за ГАЗОН" or "ПЧ за ГАЗОН", the first material is
    # the proposed one.  The lawn after "за" is the surface being replaced.
    if re.search(r"(?:^|[_\s])(?:трт|тротуар|пч)\s+за\s+газон", normalized):
        return "hard_surface"
    # A road widening made *at the expense of* lawn replaces the lawn; the
    # word "газон" describes the former surface, not the proposed one.
    if "уширен" in normalized and "за счет" in normalized:
        return "hard_surface"
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
        # Imported CAD HATCH rings can polygonize into self-intersecting
        # faces. Repair each face before GEOS combines them.
        polygonal = unary_union([as_polygonal(face) for face in faces])
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
        terminal_exclusion: Polygon | MultiPolygon | None = None,
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

    raw_extensions = as_polygonal(unary_union(extensions)) if extensions else Polygon()
    excluded_extensions: Polygon | MultiPolygon = Polygon()
    accepted_extensions = raw_extensions
    if terminal_exclusion is not None and not terminal_exclusion.is_empty:
        excluded_extensions = as_polygonal(
            raw_extensions.intersection(terminal_exclusion)
        )
        accepted_extensions = as_polygonal(
            raw_extensions.difference(terminal_exclusion)
        )
    recovered = as_polygonal(unary_union([strict_road, accepted_extensions]))
    return recovered, {
        "method": "outer_terminal_extension_from_relaxed_partition",
        "orientation": "per_work_component",
        "work_components": component_details,
        "endpoint_tolerance_in_dxf_units": endpoint_tolerance,
        "strict_road_area_in_dxf_square_units": strict_road.area,
        "relaxed_road_area_in_dxf_square_units": relaxed_road.area,
        "selected_extensions": extension_details,
        "selected_extension_count": len(extensions),
        "raw_extension_area_in_dxf_square_units": raw_extensions.area,
        "excluded_terminal_area_in_dxf_square_units": excluded_extensions.area,
        "added_area_in_dxf_square_units": recovered.difference(strict_road).area,
        "result_area_in_dxf_square_units": recovered.area,
    }


def _horizontal_spans(geometry: Any, y: float, minx: float, maxx: float) -> list[tuple[float, float]]:
    """Return the nonzero horizontal intervals cut from a polygon."""
    intersection = geometry.intersection(LineString([(minx, y), (maxx, y)]))
    spans: list[tuple[float, float]] = []

    def collect(part: Any) -> None:
        if part.is_empty:
            return
        if isinstance(part, LineString) and part.length > 1e-6:
            spans.append((part.bounds[0], part.bounds[2]))
        elif hasattr(part, "geoms"):
            for child in part.geoms:
                collect(child)

    collect(intersection)
    return sorted(spans)


def recover_unseeded_road_components(
    road_area: Polygon | MultiPolygon,
    work_boundary: Polygon | MultiPolygon,
    plantable_surface: Polygon | MultiPolygon,
    other_non_road: Polygon | MultiPolygon,
    units_per_meter: float = 1.0,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Continue a seeded road through nearby, aligned, unseeded work parcels.

    Curb strokes in another parcel may not close a topological road cell. A
    narrow cross-section corridor follows the preceding road, uses substantial
    lawn strips as side limits, and excludes every confirmed non-road surface.
    This is an explicitly uncertain continuation, never a road HATCH claim.
    """
    if units_per_meter <= 0:
        raise ValueError("units_per_meter must be positive")
    step = units_per_meter
    max_gap = 25 * units_per_meter
    min_lawn_area = 25 * units_per_meter**2
    components = sorted(polygon_parts(work_boundary), key=lambda part: part.bounds[1])
    recovered = road_area
    previous_top: float | None = None
    previous_center: float | None = None
    corridor_width: float | None = None
    details: list[dict[str, Any]] = []

    for component_index, component in enumerate(components, start=1):
        minx, miny, maxx, maxy = component.bounds
        height = maxy - miny
        width = maxx - minx
        existing = as_polygonal(recovered.intersection(component))
        if height < width * 1.5:
            previous_top = previous_center = corridor_width = None
            continue

        if existing.area >= max(units_per_meter**2, component.area * 0.01):
            top_spans = _horizontal_spans(
                existing, maxy - min(step * 0.5, height * 0.01), minx - step, maxx + step
            )
            if not top_spans:
                previous_top = previous_center = corridor_width = None
                continue
            left, right = max(top_spans, key=lambda span: span[1] - span[0])
            previous_center = (left + right) / 2
            widths = []
            for offset in (2, 5, 10, 15, 20):
                if offset * step >= height:
                    continue
                spans = _horizontal_spans(existing, maxy - offset * step, minx - step, maxx + step)
                if spans:
                    nearest = min(spans, key=lambda span: abs((span[0] + span[1]) / 2 - previous_center))
                    widths.append(nearest[1] - nearest[0])
            typical_width = sorted(widths)[len(widths) // 2] if widths else right - left
            corridor_width = max(15 * step, min(25 * step, typical_width * 1.4))
            previous_top = maxy
            continue

        if previous_top is None or previous_center is None or corridor_width is None:
            continue
        gap = miny - previous_top
        if not 0 <= gap <= max_gap:
            previous_top = previous_center = corridor_width = None
            continue
        bottom_spans = _horizontal_spans(
            component, miny + min(step * 0.5, height * 0.01), minx - step, maxx + step
        )
        lateral_gap = min(
            (max(left - previous_center, previous_center - right, 0) for left, right in bottom_spans),
            default=float("inf"),
        )
        if lateral_gap > corridor_width / 2:
            previous_top = previous_center = corridor_width = None
            continue

        major_lawns = [
            part for part in polygon_parts(plantable_surface.intersection(component))
            if part.area >= min_lawn_area
        ]
        if not major_lawns:
            previous_top = previous_center = corridor_width = None
            continue
        center = previous_center
        left_points: list[tuple[float, float]] = []
        right_points: list[tuple[float, float]] = []
        y = miny + step * 0.25
        while y < maxy:
            spans = _horizontal_spans(component, y, minx - step, maxx + step)
            if not spans:
                y += step
                continue
            left_work, right_work = min(
                spans,
                key=lambda span: max(span[0] - center, center - span[1], 0),
            )
            lawns = [
                lawn_span
                for lawn in major_lawns
                for lawn_span in _horizontal_spans(lawn, y, left_work, right_work)
            ]
            left_lawn = max(
                (right for _left, right in lawns
                 if right <= center and center - right <= corridor_width * 0.75),
                default=None,
            )
            right_lawn = min(
                (left for left, _right in lawns
                 if left >= center and left - center <= corridor_width * 0.75),
                default=None,
            )
            desired = center
            if (left_lawn is not None and right_lawn is not None
                    and 4 * step < right_lawn - left_lawn <= corridor_width * 1.5):
                desired = (left_lawn + right_lawn) / 2
            elif right_lawn is not None:
                desired = right_lawn - corridor_width / 2
            elif left_lawn is not None:
                desired = left_lawn + corridor_width / 2
            elif right_work - left_work <= corridor_width * 1.25:
                desired = (left_work + right_work) / 2
            center = max(center - 2 * step, min(center + 2 * step, desired))
            left = max(left_work, center - corridor_width / 2)
            right = min(right_work, center + corridor_width / 2)
            if left_lawn is not None:
                left = max(left, left_lawn)
            if right_lawn is not None:
                right = min(right, right_lawn)
            if right - left >= 4 * step:
                left_points.append((left, y))
                right_points.append((right, y))
            y += step
        if len(left_points) < 2:
            previous_top = previous_center = corridor_width = None
            continue
        outline = Polygon([
            (left_points[0][0], miny), *left_points,
            (left_points[-1][0], maxy), (right_points[-1][0], maxy),
            *reversed(right_points), (right_points[0][0], miny),
        ])
        candidate = as_polygonal(
            make_valid(outline).intersection(component)
            .difference(plantable_surface).difference(other_non_road)
        )
        coverage = candidate.area / component.area
        total_coverage = (recovered.area + candidate.area) / work_boundary.area
        if not 0.05 <= coverage <= 0.65 or total_coverage > 0.65:
            previous_top = previous_center = corridor_width = None
            continue
        recovered = as_polygonal(unary_union([recovered, candidate]))
        top_spans = _horizontal_spans(candidate, maxy - step * 0.5, minx - step, maxx + step)
        if top_spans:
            left, right = max(top_spans, key=lambda span: span[1] - span[0])
            previous_center = (left + right) / 2
            previous_top = maxy
        else:
            previous_top = previous_center = corridor_width = None
        details.append({
            "work_component_index": component_index,
            "gap_from_previous_component_in_dxf_units": gap,
            "inferred_area_in_dxf_square_units": candidate.area,
            "coverage_ratio": coverage,
            "method": "longitudinal_corridor_between_confirmed_lawns",
            "requires_visual_confirmation": True,
        })

    return recovered, {
        "method": "seeded_corridor_continuation",
        "max_inter_component_gap_in_dxf_units": max_gap,
        "inferred_components": details,
        "added_area_in_dxf_square_units": recovered.difference(road_area).area,
        "requires_visual_confirmation": bool(details),
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


def read_road_review_corrections(
    path: Path,
    work_boundary: Polygon | MultiPolygon,
    plantable_surface: Polygon | MultiPolygon,
    buildings: Polygon | MultiPolygon,
) -> tuple[Polygon | MultiPolygon, Polygon | MultiPolygon, dict[str, Any]]:
    """Read explicit, auditable CAD review polygons for one drawing.

    A reviewed road polygon takes precedence over a conflicting sidewalk HATCH;
    a reviewed sidewalk polygon takes precedence over an inferred road. Point
    observations alone never become area classifications.
    """
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if document.get("type") != "FeatureCollection":
        raise ValueError("Road review corrections must be a GeoJSON FeatureCollection")
    road_parts: list[Polygon] = []
    sidewalk_parts: list[Polygon] = []
    details: list[dict[str, Any]] = []
    for index, feature in enumerate(document.get("features", []), start=1):
        if feature.get("type") != "Feature":
            raise ValueError(f"Road correction {index} is not a GeoJSON Feature")
        classification = feature.get("properties", {}).get("classification")
        if classification not in {"road", "sidewalk"}:
            raise ValueError(f"Road correction {index} has an invalid classification")
        raw_geometry = feature.get("geometry")
        if not isinstance(raw_geometry, dict):
            raise ValueError(f"Road correction {index} has no GeoJSON geometry")
        geometry = as_polygonal(make_valid(shape(raw_geometry)))
        if geometry.is_empty:
            raise ValueError(f"Road correction {index} has no polygonal area")
        if geometry.difference(work_boundary).area > 0.01:
            raise ValueError(f"Road correction {index} extends beyond the work boundary")
        if classification == "road" and (
            geometry.intersection(plantable_surface).area > 0.01
            or geometry.intersection(buildings).area > 0.01
        ):
            raise ValueError(
                f"Road correction {index} conflicts with a confirmed lawn or building"
            )
        parts = road_parts if classification == "road" else sidewalk_parts
        parts.extend(polygon_parts(geometry))
        details.append({
            "id": feature.get("id", index),
            "classification": classification,
            "area_in_dxf_square_units": geometry.area,
            "evidence": feature.get("properties", {}).get("evidence"),
        })
    road = as_polygonal(unary_union(road_parts)) if road_parts else Polygon()
    sidewalk = as_polygonal(unary_union(sidewalk_parts)) if sidewalk_parts else Polygon()
    if road.intersection(sidewalk).area > 0.01:
        raise ValueError("Reviewed road and sidewalk corrections overlap")
    return road, sidewalk, {"source": str(path), "features": details}


def build(
    normalized_path: Path,
    surface_candidates_path: Path,
    output_path: Path,
    report_path: Path,
    curve_tolerance: float = 0.1,
    min_area: float = 0.01,
    road_stitch_tolerance: float = 0.30,
    road_min_seed_overlap_area: float = 0.50,
    unit_metadata_path: Path | None = None,
    road_corrections_path: Path | None = None,
    reconstructed_utilities_path: Path | None = None,
) -> None:
    """Calculate and persist the common base area for all plant types."""
    unit_metadata: dict[str, Any] = {}
    if unit_metadata_path is not None:
        unit_metadata = json.loads(unit_metadata_path.read_text(encoding="utf-8-sig"))
        if not unit_metadata.get("unit_scale_confirmed", False):
            raise ValueError("DXF unit metadata does not confirm the drawing scale")
    units_per_meter = float(unit_metadata.get("dxf_units_per_meter") or 1.0)
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
    all_utility_well_footprints: Polygon | MultiPolygon = Polygon()
    utility_well_footprints: Polygon | MultiPolygon = Polygon()
    try:
        all_utility_well_footprints = as_polygonal(
            read_object_geometry(normalized_path, "utility_well_footprint")
        )
        utility_well_footprints = as_polygonal(
            all_utility_well_footprints.intersection(work_boundary)
        )
    except ValueError:
        # Older normalized files and streets without wells remain supported.
        utility_well_footprints = Polygon()
    heat_chamber_footprints: Polygon | MultiPolygon = Polygon()
    heat_chamber_full_footprints: Polygon | MultiPolygon = Polygon()
    heat_chamber_review_footprints: Polygon | MultiPolygon = Polygon()
    heat_chamber_detection = {
        "candidate_faces": 0, "square_faces": 0,
        "accepted_faces": 0, "review_faces": 0,
    }
    if reconstructed_utilities_path is not None:
        try:
            heat_lines = read_object_geometry(
                reconstructed_utilities_path, "heat_pipe"
            )
        except ValueError:
            heat_lines = MultiLineString([])
        (
            heat_chamber_full_footprints,
            heat_chamber_review_footprints,
            heat_chamber_detection,
        ) = (
            infer_heat_chamber_footprints(
                heat_lines, all_utility_well_footprints, units_per_meter
            )
        )
        heat_chamber_review_footprints = as_polygonal(
            heat_chamber_review_footprints.intersection(work_boundary)
        )
        try:
            raw_heat_lines = read_object_geometry(normalized_path, "heat_pipe")
        except ValueError:
            raw_heat_lines = MultiLineString([])
        dashed_chambers, dashed_report = detect_dashed_square_chambers(
            raw_heat_lines,
            all_utility_well_footprints,
            work_boundary,
            units_per_meter,
        )
        heat_chamber_full_footprints = as_polygonal(unary_union([
            heat_chamber_full_footprints, dashed_chambers,
        ]))
        heat_chamber_footprints = as_polygonal(
            heat_chamber_full_footprints.intersection(work_boundary)
        )
        heat_chamber_detection["dashed_square_detection"] = dashed_report
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
    # A directly polygonized HATCH is a stable topological barrier. Some
    # self-intersecting sidewalk HATCHes need repair to recover their actual
    # area, but those repaired faces can split a road terminal into fragments.
    # Use the direct faces to find terminal candidates, then subtract the full
    # normalized sidewalk geometry from the candidates below.
    direct_sidewalk_area, direct_sidewalk_diagnostics = (
        read_surface_area_by_predicate(
            surface_candidates_path,
            work_boundary,
            is_sidewalk_partition_layer,
            curve_tolerance,
            min_area,
        )
    )

    road_area: Polygon | MultiPolygon = Polygon()
    sidewalks: Polygon | MultiPolygon = Polygon()
    try:
        sidewalks = as_polygonal(
            read_object_geometry(normalized_path, "sidewalk").intersection(
                work_boundary
            )
        )
    except ValueError:
        sidewalks = Polygon()
    if not direct_sidewalk_area.is_empty:
        sidewalks = as_polygonal(unary_union([sidewalks, direct_sidewalk_area]))
    road_reconstruction: dict[str, Any]
    try:
        road_edges = read_object_geometry(normalized_path, "road_edge")
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
                direct_sidewalk_area,
                plantable_surface_area,
            ),
            stitch_tolerance=road_stitch_tolerance,
            min_seed_overlap_area=road_min_seed_overlap_area,
        )
        road_area, terminal_recovery = recover_outer_terminal_road(
            road_area,
            relaxed_road_area,
            work_boundary,
            terminal_exclusion=sidewalks,
        )
        road_reconstruction["terminal_recovery"] = terminal_recovery
        road_area, continuation = recover_unseeded_road_components(
            road_area,
            work_boundary,
            plantable_surface_area,
            as_polygonal(unary_union([hard_surface_area, sidewalks, buildings_in_work_area])),
            units_per_meter,
        )
        road_reconstruction["unseeded_continuation"] = continuation
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
        # Keep explicitly drawn carriageway surfaces even when curb-based
        # reconstruction has no usable faces.  They are confirmed road area,
        # and downstream verification expects a road feature when one exists.
        road_area = road_seed_area
        road_reconstruction = {
            "status": "explicit_surface_fallback" if not road_seed_area.is_empty else "unavailable",
            "reason": str(error),
            "method": "explicit_surface_only" if not road_seed_area.is_empty else None,
            "requires_visual_confirmation": True,
        }

    road_review: dict[str, Any] = {"status": "not_provided"}
    if road_corrections_path is not None:
        reviewed_road, reviewed_sidewalk, road_review = (
            read_road_review_corrections(
                road_corrections_path,
                work_boundary,
                plantable_surface_area,
                buildings_in_work_area,
            )
        )
        road_review["status"] = "applied"
        road_review["road_added_area_in_dxf_square_units"] = (
            reviewed_road.difference(road_area).area
        )
        road_review["sidewalk_added_area_in_dxf_square_units"] = (
            reviewed_sidewalk.difference(sidewalks).area
        )
        sidewalks = as_polygonal(
            unary_union([sidewalks, reviewed_sidewalk]).difference(reviewed_road)
        )
        road_area = as_polygonal(
            unary_union([road_area, reviewed_road]).difference(reviewed_sidewalk)
        )
        road_reconstruction["candidate_area_ratio"] = (
            road_area.area / work_boundary.area
        )

    exclusions_without_utility_wells = as_polygonal(
        unary_union([
            hard_surface_area,
            road_area,
            buildings_in_work_area,
            sidewalks,
        ])
    )
    absolute_exclusions = as_polygonal(
        unary_union([
            exclusions_without_utility_wells,
            utility_well_footprints,
            heat_chamber_footprints,
        ])
    )

    # A subtraction-only mask treats every unclassified part of the drawing as
    # plantable.  On real CAD plans that is unsafe: a sidewalk whose layer was
    # not recognized becomes a false-positive green zone.  Prefer positive
    # evidence (explicit lawn/soil/planting HATCH polygons) and use the whole
    # work boundary only as a clearly reported fallback for poorer inputs.
    if not plantable_surface_area.is_empty:
        planting_candidate_area = as_polygonal(
            plantable_surface_area.intersection(work_boundary)
        )
        planting_candidate_source = "confirmed_plantable_surface"
    else:
        planting_candidate_area = work_boundary
        planting_candidate_source = "work_boundary_fallback"

    base_allowed_area = as_polygonal(
        planting_candidate_area.difference(absolute_exclusions)
    )
    review_parts = [
        face for face in polygon_parts(heat_chamber_review_footprints)
        if face.intersection(base_allowed_area).area >= 0.1 * units_per_meter**2
    ]
    heat_chamber_review_footprints = (
        as_polygonal(unary_union(review_parts)) if review_parts else Polygon()
    )
    heat_chamber_detection["review_faces_with_planting_potential"] = len(review_parts)
    if base_allowed_area.is_empty:
        raise ValueError(
            "planting candidate area - sidewalks - hard_surface_area - "
            "road_area - buildings - utility_well_footprints - heat_chamber_footprints "
            "produced an empty geometry"
        )

    formula = (
        f"{planting_candidate_source} - sidewalk_area - "
        "hard_surface_area - road_area - buildings_in_work_area - "
        "utility_well_footprints - heat_chamber_footprints"
    )

    output_features = [
        geometry_feature(
            "base_allowed_area",
            base_allowed_area,
            {
                "stage": "base_constraint_builder",
                "formula": formula,
                "planting_candidate_source": planting_candidate_source,
                "applied_restrictions": [
                    "confirmed_plantable_surface_mask",
                    "sidewalk_area",
                    "hard_surface_area",
                    "reconstructed_road_area",
                    "verified_building_footprints",
                    "utility_well_footprints",
                    "heat_chamber_footprints",
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
                    "source_object_type": (
                        "road_surface_candidate"
                        if road_reconstruction["status"] == "explicit_surface_fallback"
                        else "road_edge"
                    ),
                    "road_seed_source": str(surface_candidates_path),
                    "reconstruction_method": road_reconstruction.get("method"),
                    "requires_visual_confirmation": True,
                },
            )
        )
    if not sidewalks.is_empty:
        output_features.append(
            geometry_feature(
                "sidewalk_area",
                sidewalks,
                {
                    "stage": "base_constraint_builder",
                    "source_object_type": "sidewalk",
                },
            )
        )
    if not plantable_surface_area.is_empty:
        output_features.append(
            geometry_feature(
                "confirmed_plantable_surface",
                plantable_surface_area,
                {
                    "stage": "base_constraint_builder",
                    "source": str(surface_candidates_path),
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
    if not utility_well_footprints.is_empty:
        output_features.append(
            geometry_feature(
                "utility_well_footprints",
                utility_well_footprints,
                {
                    "stage": "base_constraint_builder",
                    "source_object_type": "utility_well_footprint",
                    "role": "physical_hard_obstacle",
                    "extra_clearance_in_dxf_units": 0.0,
                },
            )
        )
    if not heat_chamber_footprints.is_empty:
        output_features.append(
            geometry_feature(
                "heat_chamber_footprints",
                heat_chamber_footprints,
                {
                    "stage": "base_constraint_builder",
                    "source": str(reconstructed_utilities_path),
                    "evidence": "closed_or_dashed_square_heat_network_with_well_footprint",
                    "role": "physical_hard_obstacle",
                    "requires_visual_confirmation": True,
                },
            )
        )
    if not heat_chamber_full_footprints.is_empty:
        output_features.append(
            geometry_feature(
                "heat_chamber_full_footprints",
                heat_chamber_full_footprints,
                {
                    "stage": "base_constraint_builder",
                    "role": "diagnostic_complete_reconstruction",
                    "constraint_geometry": "heat_chamber_footprints",
                    "requires_visual_confirmation": True,
                },
            )
        )
    if not heat_chamber_review_footprints.is_empty:
        output_features.append(
            geometry_feature(
                "heat_chamber_review_footprints",
                heat_chamber_review_footprints,
                {
                    "stage": "base_constraint_builder",
                    "source": str(reconstructed_utilities_path),
                    "evidence": "square_heat_network_face_without_well_footprint",
                    "role": "visual_review_only",
                    "excluded_from_planting": False,
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
    if planting_candidate_source == "work_boundary_fallback":
        warnings.append(
            "No confirmed plantable surface was found. The base area uses the "
            "work boundary fallback and may contain unclassified paving."
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
    excluded_from_candidate_area = as_polygonal(
        absolute_exclusions.intersection(planting_candidate_area)
    ).area
    incremental_utility_well_exclusion = as_polygonal(
        utility_well_footprints
        .intersection(planting_candidate_area)
        .difference(exclusions_without_utility_wells)
    ).area
    incremental_heat_chamber_exclusion = as_polygonal(
        heat_chamber_footprints
        .intersection(planting_candidate_area)
        .difference(unary_union([
            exclusions_without_utility_wells, utility_well_footprints
        ]))
    ).area
    area_balance_error = abs(
        planting_candidate_area.area
        - base_allowed_area.area
        - excluded_from_candidate_area
    )
    report = {
        "normalized_input": str(normalized_path),
        "surface_candidates_input": str(surface_candidates_path),
        "output": str(output_path),
        "coordinate_reference": "local_dxf_coordinates",
        "dxf_units_per_meter": unit_metadata.get("dxf_units_per_meter"),
        "unit_scale_confirmed": bool(unit_metadata.get("unit_scale_confirmed", False)),
        "unit_scale_source": unit_metadata.get("unit_scale_source"),
        "insert_units_code": unit_metadata.get("insert_units_code"),
        "insert_units_name": unit_metadata.get("insert_units_name"),
        "units_confirmed_as_metres": bool(
            unit_metadata.get("units_confirmed_as_metres", False)
        ),
        "formula": formula,
        "planting_candidate_source": planting_candidate_source,
        "areas_in_dxf_square_units": {
            "work_boundary": work_boundary.area,
            "planting_candidate_area": planting_candidate_area.area,
            "hard_surface_area": hard_surface_area.area,
            "road_seed_area": road_seed_area.area,
            "reconstructed_road_area": road_area.area,
            "unambiguous_plantable_surface_area": plantable_surface_area.area,
            "sidewalk_area": sidewalks.area,
            "sidewalk_partition_barrier_area": sidewalk_partition_area.area,
            "direct_sidewalk_barrier_area": direct_sidewalk_area.area,
            "all_normalized_buildings": all_buildings.area,
            "buildings_in_work_area": buildings_in_work_area.area,
            "utility_well_footprints": utility_well_footprints.area,
            "heat_chamber_footprints": heat_chamber_footprints.area,
            "heat_chamber_full_footprints": heat_chamber_full_footprints.area,
            "heat_chamber_review_footprints": heat_chamber_review_footprints.area,
            "incremental_utility_well_exclusion_inside_planting_candidate": (
                incremental_utility_well_exclusion
            ),
            "incremental_heat_chamber_exclusion_inside_planting_candidate": (
                incremental_heat_chamber_exclusion
            ),
            "absolute_exclusions": excluded_area,
            "exclusions_inside_planting_candidate": excluded_from_candidate_area,
            "base_allowed_area": base_allowed_area.area,
            "area_balance_error": area_balance_error,
        },
        "surface_detection": surface_diagnostics,
        "road_seed_detection": road_seed_diagnostics,
        "plantable_surface_detection": plantable_surface_diagnostics,
        "sidewalk_partition": sidewalk_partition_diagnostics,
        "direct_sidewalk_detection": direct_sidewalk_diagnostics,
        "road_reconstruction": road_reconstruction,
        "road_review_corrections": road_review,
        "heat_chamber_detection": heat_chamber_detection,
        "applied_restrictions": [
            "confirmed_plantable_surface_mask",
            "sidewalk_area",
            "hard_surface_area",
            "reconstructed_road_area",
            "verified_building_footprints",
            "utility_well_footprints",
            "heat_chamber_footprints",
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
    print(
        "Utility well footprints in work area: "
        f"{utility_well_footprints.area:.3f} square DXF units"
    )
    print(
        "Heat chamber footprints in work area: "
        f"{heat_chamber_footprints.area:.3f} square DXF units"
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
    parser.add_argument(
        "--unit-metadata",
        type=Path,
        help="JSON report produced by scripts/detect_dxf_units.py",
    )
    parser.add_argument(
        "--road-corrections",
        type=Path,
        help="Optional reviewed road/sidewalk GeoJSON polygons for this drawing",
    )
    parser.add_argument(
        "--reconstructed-utilities",
        type=Path,
        help="Accepted reconstructed utility GeoJSONL used to identify heat chambers",
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
            args.unit_metadata,
            args.road_corrections,
            args.reconstructed_utilities,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Constraint builder error: {error}") from error


if __name__ == "__main__":
    main()
