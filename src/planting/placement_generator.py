"""Generate an auditable planting plan from calculated allow zones.

The module is deliberately independent from nanoCAD so the mandatory
DXF -> calculation -> DXF path can run on Linux/MosTech.OS.  It consumes the
same zones and normative report as the rule engine, supports aligned rows and
area layouts, protects existing vegetation and writes one GeoJSON Feature per
proposed planting plus a machine-readable verification report.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

from shapely import from_geojson
from shapely.geometry import GeometryCollection, MultiPoint, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.validation import make_valid

from ..domain.models import PlantingProfile
from ..geometry.parts import polygon_parts
from ..geometry.sidewalk_geometry import relevant_sidewalk_geometry


LINEAR_MIN_ASPECT_RATIO = 2.5
LINEAR_MIN_ENVELOPE_FILL = 0.55
LINEAR_COUNT_RETENTION = 0.9


def load_geojsonl_by_object_type(path: Path) -> dict[str, Any]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature = json.loads(line)
                object_type = feature.get("properties", {}).get("object_type")
                if object_type and feature.get("geometry"):
                    # Let GEOS construct coordinate arrays directly.  Going
                    # through ``shape()`` first creates one Python object per
                    # coordinate sequence and dominates loading for large CAD
                    # networks with hundreds of thousands of segments.
                    geometry = from_geojson(line)
                    if geometry is None:
                        raise ValueError("GeoJSON geometry is null")
                    grouped[object_type].append(make_valid(geometry))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path}, line {line_number}: invalid GeoJSON feature") from error
    return {key: unary_union(items) for key, items in grouped.items()}


def load_zones(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature = json.loads(line)
                properties = feature.get("properties", {})
                if properties.get("object_type") != "plant_allow_zone":
                    continue
                plant_type = str(properties["plant_type"])
                geometry = make_valid(shape(feature["geometry"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path}, line {line_number}: invalid plant zone") from error
            result[plant_type] = {"geometry": geometry, "properties": properties}
    if not result:
        raise ValueError(f"No plant_allow_zone features found in {path}")
    return result


def load_profiles(path: Path, default_max_count: int = 5000) -> dict[str, PlantingProfile]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    profiles: dict[str, PlantingProfile] = {}
    for item in data.get("plantingProfiles", []):
        if item.get("geometryKind") != "point":
            continue
        profile = PlantingProfile(
            plant_type=str(item["plantType"]),
            species=str(item["species"]),
            spacing_m=float(item["spacingM"]),
            footprint_radius_m=float(item["footprintRadiusM"]),
            symbol_radius_m=float(item["symbolRadiusM"]),
            avoid_other_plantings_m=float(item.get("avoidOtherPlantingsM", 0.0)),
            max_count=int(item.get("maxCount") or data.get("maxPlacementsPerType") or default_max_count),
            catalog_reference=str(item.get("catalogReference", "plant catalog")),
            selection_reasons=tuple(str(value) for value in item.get("selectionReasons", [])),
        )
        if profile.spacing_m <= 0 or profile.footprint_radius_m < 0:
            raise ValueError(f"Invalid planting profile: {profile.plant_type}")
        profiles[profile.plant_type] = profile
    if not profiles:
        raise ValueError(f"No point planting profiles found in {path}")
    return profiles


def grid_candidates(
    polygon: Polygon,
    spacing: float,
    angle: float,
    phase_x: float,
    phase_y: float,
) -> Iterator[tuple[float, float]]:
    prepared = prep(polygon)
    min_x, min_y, max_x, max_y = polygon.bounds
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    projections = []
    for x, y in ((min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)):
        dx, dy = x - center_x, y - center_y
        projections.append((dx * cos_angle + dy * sin_angle, -dx * sin_angle + dy * cos_angle))
    row_step = spacing * math.sqrt(3.0) / 2.0
    min_u = min(value[0] for value in projections) - spacing
    max_u = max(value[0] for value in projections) + spacing
    min_v = min(value[1] for value in projections) - row_step
    max_v = max(value[1] for value in projections) + row_step
    row = 0
    v = min_v + phase_y * row_step
    while v <= max_v + 1e-9:
        row_offset = 0.0 if row % 2 == 0 else spacing / 2.0
        u = min_u + phase_x * spacing + row_offset
        while u <= max_u + 1e-9:
            x = center_x + u * cos_angle - v * sin_angle
            y = center_y + u * sin_angle + v * cos_angle
            point = Point(x, y)
            if prepared.covers(point):
                yield x, y
            u += spacing
        v += row_step
        row += 1


def required_spacing(first: PlantingProfile, second: PlantingProfile) -> float:
    footprint_spacing = first.footprint_radius_m + second.footprint_radius_m
    if first.plant_type == second.plant_type:
        return max(first.spacing_m, second.spacing_m, footprint_spacing)
    return max(first.avoid_other_plantings_m, second.avoid_other_plantings_m, footprint_spacing)


def pack_candidates(
    candidates: Iterable[tuple[float, float]],
    profile: PlantingProfile,
    profiles: dict[str, PlantingProfile],
    occupied: list[tuple[str, float, float]],
    max_count: int,
    audit: list[dict[str, Any]] | None = None,
) -> list[tuple[float, float]]:
    maximum_spacing = max(
        required_spacing(profile, other)
        for other in profiles.values()
    )
    cell_size = max(maximum_spacing, 0.001)
    grid: dict[tuple[int, int], list[tuple[PlantingProfile, float, float]]] = defaultdict(list)

    def add(item_profile: PlantingProfile, x: float, y: float) -> None:
        grid[(math.floor(x / cell_size), math.floor(y / cell_size))].append((item_profile, x, y))

    for key, x, y in occupied:
        if key in profiles:
            add(profiles[key], x, y)
    accepted: list[tuple[float, float]] = []
    for x, y in candidates:
        if len(accepted) >= max_count:
            if audit is None:
                break
            audit.append({"point": [x, y], "reason": "max_count", "limit": max_count})
            continue
        cell_x, cell_y = math.floor(x / cell_size), math.floor(y / cell_size)
        conflict: tuple[PlantingProfile, float, float, float] | None = None
        for offset_x in (-1, 0, 1):
            for offset_y in (-1, 0, 1):
                for other_profile, other_x, other_y in grid.get((cell_x + offset_x, cell_y + offset_y), []):
                    distance = required_spacing(profile, other_profile)
                    if (x - other_x) ** 2 + (y - other_y) ** 2 + 1e-9 < distance ** 2:
                        conflict = (other_profile, other_x, other_y, distance)
                        break
                if conflict is not None:
                    break
            if conflict is not None:
                break
        if conflict is not None:
            if audit is not None:
                other_profile, other_x, other_y, required = conflict
                audit.append({"point": [x, y], "reason": "spacing",
                              "other_point": [other_x, other_y],
                              "other_species": other_profile.species,
                              "actual_distance": math.hypot(x - other_x, y - other_y),
                              "required_distance": required})
            continue
        accepted.append((x, y))
        add(profile, x, y)
    return accepted


def best_component_layout(
    component: Polygon,
    profile: PlantingProfile,
    profiles: dict[str, PlantingProfile],
    occupied: list[tuple[str, float, float]],
    max_count: int,
    trace: dict[str, Any] | None = None,
) -> list[tuple[float, float]]:
    best: list[tuple[float, float]] = []
    winner: dict[str, Any] | None = None
    winner_angle: float | None = None
    variants: list[dict[str, Any]] = []
    phases = (0.0, 0.25, 0.5, 0.75)
    for angle_index in range(6):
        angle = angle_index * math.pi / 18.0
        for phase_x in phases:
            for phase_y in phases:
                packed = pack_candidates(
                    grid_candidates(component, profile.spacing_m, angle, phase_x, phase_y),
                    profile,
                    profiles,
                    occupied,
                    max_count,
                )
                variant = {"angle_deg": round(math.degrees(angle), 3),
                           "phase_x": phase_x, "phase_y": phase_y,
                           "accepted_count": len(packed)}
                if trace is not None:
                    variants.append(variant)
                if len(packed) > len(best):
                    best = packed
                    winner = variant
                    winner_angle = angle
    if trace is not None:
        rejected: list[dict[str, Any]] = []
        if winner is not None and winner_angle is not None:
            replay = pack_candidates(
                grid_candidates(component, profile.spacing_m,
                                winner_angle,
                                winner["phase_x"], winner["phase_y"]),
                profile, profiles, occupied, max_count, rejected,
            )
            if replay != best:
                raise ValueError("Layout audit replay differs from selected hex-grid layout")
        trace.update({"method": "hex_grid", "objective": "maximum accepted count; first variant wins ties",
                      "variants": variants, "winner": winner,
                      "winning_grid_rejections": rejected,
                      "audit_scope": "grid points inside the winning safe-scope component"})
    return best


def linear_reference(geometry: Polygon, spacing: float) -> tuple[float, float, float, float, float] | None:
    """Return a stable long-axis frame for an elongated planting band."""
    rectangle = geometry.minimum_rotated_rectangle
    if not isinstance(rectangle, Polygon) or rectangle.is_empty:
        return None
    corners = list(rectangle.exterior.coords)
    edges = [
        (math.hypot(corners[i + 1][0] - corners[i][0], corners[i + 1][1] - corners[i][1]), i)
        for i in range(4)
    ]
    length, longest = max(edges)
    width = min(value for value, _index in edges)
    # A long bounding box alone is not enough: L-shaped plots need separate
    # local layouts, not a single axis cutting through empty space.
    if (
        length < 2 * spacing or width <= 0 or length / width < LINEAR_MIN_ASPECT_RATIO
        or geometry.area / rectangle.area < LINEAR_MIN_ENVELOPE_FILL
    ):
        return None
    start, end = corners[longest], corners[longest + 1]
    angle = math.atan2(end[1] - start[1], end[0] - start[0]) % math.pi
    cos_angle, sin_angle = math.cos(angle), math.sin(angle)
    projections = [
        (x * cos_angle + y * sin_angle, -x * sin_angle + y * cos_angle)
        for x, y in corners[:-1]
    ]
    return angle, min(u for u, _ in projections), max(u for u, _ in projections), min(v for _, v in projections), max(v for _, v in projections)


def linear_grid_candidates(
    scope: Any,
    frame: tuple[float, float, float, float, float],
    spacing: float,
    phase_u: float,
    phase_v: float,
) -> Iterator[tuple[float, float]]:
    """Place aligned rows using one phase across all safe pieces of a band."""
    angle, min_u, max_u, min_v, max_v = frame
    cos_angle, sin_angle = math.cos(angle), math.sin(angle)
    prepared = prep(scope)
    first_u = math.ceil(min_u / spacing - phase_u)
    last_u = math.floor(max_u / spacing - phase_u)
    first_v = math.ceil(min_v / spacing - phase_v)
    last_v = math.floor(max_v / spacing - phase_v)
    for row in range(first_v, last_v + 1):
        v = (row + phase_v) * spacing
        for column in range(first_u, last_u + 1):
            u = (column + phase_u) * spacing
            x, y = u * cos_angle - v * sin_angle, u * sin_angle + v * cos_angle
            if prepared.covers(Point(x, y)):
                yield x, y


def best_linear_layout(
    scope: Any,
    reference: Polygon,
    profile: PlantingProfile,
    profiles: dict[str, PlantingProfile],
    occupied: list[tuple[str, float, float]],
    max_count: int,
    trace: dict[str, Any] | None = None,
) -> list[tuple[float, float]] | None:
    """Prefer long, aligned runs over a few extra staggered trees."""
    frame = linear_reference(reference, profile.spacing_m)
    if frame is None:
        return None
    angle, min_u, max_u, min_v, max_v = frame
    cos_angle, sin_angle = math.cos(angle), math.sin(angle)
    variants: list[tuple[list[tuple[float, float]], int, int, float, float, float]] = []
    empty_phases: list[tuple[float, float]] = []
    for phase_u in (0.0, 0.25, 0.5, 0.75):
        for phase_v in (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875):
            packed = pack_candidates(
                linear_grid_candidates(scope, frame, profile.spacing_m, phase_u, phase_v),
                profile,
                profiles,
                occupied,
                max_count,
            )
            if not packed:
                empty_phases.append((phase_u, phase_v))
                continue
            rows: dict[int, list[float]] = defaultdict(list)
            for x, y in packed:
                u, v = x * cos_angle + y * sin_angle, -x * sin_angle + y * cos_angle
                rows[round(v / profile.spacing_m - phase_v)].append(u)
            adjacent_pairs = 0
            for values in rows.values():
                stations = sorted(values)
                adjacent_pairs += sum(
                    abs(right - left - profile.spacing_m) < 1e-5
                    for left, right in zip(stations, stations[1:])
                )
            isolated_rows = sum(len(values) == 1 for values in rows.values())
            u_values = [x * cos_angle + y * sin_angle for x, y in packed]
            v_values = [-x * sin_angle + y * cos_angle for x, y in packed]
            margin_imbalance = abs((min(u_values) - min_u) - (max_u - max(u_values)))
            margin_imbalance += abs((min(v_values) - min_v) - (max_v - max(v_values)))
            variants.append((packed, adjacent_pairs, isolated_rows, margin_imbalance,
                             phase_u, phase_v))
    if not variants:
        if trace is not None:
            trace.update({"method": "linear", "variants": [
                            {"phase_u": u, "phase_v": v, "accepted_count": 0,
                             "eligible": False} for u, v in empty_phases], "winner": None,
                          "winning_grid_rejections": []})
        return []
    best_count = max(len(item[0]) for item in variants)
    eligible = (item for item in variants if len(item[0]) >= math.ceil(best_count * LINEAR_COUNT_RETENTION))
    winner = max(
        eligible,
        key=lambda item: (item[1], -item[2], -item[3], len(item[0])),
    )
    if trace is not None:
        rejected = []
        replay = pack_candidates(
            linear_grid_candidates(scope, frame, profile.spacing_m, winner[4], winner[5]),
            profile, profiles, occupied, max_count, rejected,
        )
        if replay != winner[0]:
            raise ValueError("Layout audit replay differs from selected linear layout")
        trace.update({
            "method": "linear", "angle_deg": math.degrees(angle),
            "objective": "retain near-maximum count, then maximize adjacent row pairs, "
                         "minimize isolated rows and margin imbalance",
            "count_retention_ratio": LINEAR_COUNT_RETENTION,
            "variants": [
                {"phase_u": item[4], "phase_v": item[5], "accepted_count": len(item[0]),
                 "adjacent_pairs": item[1], "isolated_rows": item[2],
                 "margin_imbalance": item[3],
                 "eligible": len(item[0]) >= math.ceil(best_count * LINEAR_COUNT_RETENTION)}
                for item in variants
            ] + [{"phase_u": u, "phase_v": v, "accepted_count": 0,
                  "eligible": False} for u, v in empty_phases],
            "winner": {"phase_u": winner[4], "phase_v": winner[5],
                       "accepted_count": len(winner[0]), "adjacent_pairs": winner[1],
                       "isolated_rows": winner[2], "margin_imbalance": winner[3]},
            "winning_grid_rejections": rejected,
            "audit_scope": "grid points inside the winning safe-scope band",
        })
    return winner[0]


def safe_scope(
    zone: Any,
    profile: PlantingProfile,
    existing_trees: Any | None,
    existing_tree_belts: Any | None,
    existing_tree_clearance_m: float,
    units_per_meter: float,
) -> Any:
    footprint = profile.footprint_radius_m * units_per_meter
    scope = make_valid(zone).buffer(-footprint) if footprint > 0 else make_valid(zone)
    if scope.is_empty:
        return scope
    if existing_trees is not None and not existing_trees.is_empty:
        clearance = existing_tree_clearance_m * units_per_meter
        # GEOS buffers approximate circles with straight chords.  A tiny
        # expansion plus a fine approximation prevents points just below the
        # declared clearance from slipping between chord midpoints.
        scope = scope.difference(existing_trees.buffer(clearance * 1.001, quad_segs=32))
    if existing_tree_belts is not None and not existing_tree_belts.is_empty:
        scope = scope.difference(existing_tree_belts.buffer(footprint, quad_segs=4))
    return make_valid(scope)


def configured_existing_tree_clearance_m(
    config: dict[str, Any], profile: PlantingProfile
) -> float:
    """Return the total centre-to-centre clearance from an existing tree.

    ``existingTreeClearanceM`` is the current explicit project parameter.  The
    legacy canopy-radius setting remains supported for older config files.
    """
    explicit = config.get("existingTreeClearanceM")
    clearance = (
        float(explicit)
        if explicit is not None
        else float(config.get("existingTreeCanopyRadiusM", 2.5))
        + profile.footprint_radius_m
    )
    if not math.isfinite(clearance) or clearance < 0:
        raise ValueError("existingTreeClearanceM must be finite and non-negative")
    return clearance


def rule_geometry(
    target_type: str,
    constraints: dict[str, Any],
    normalized: dict[str, Any],
    utilities: dict[str, Any],
) -> Any | None:
    def first_available(*items: Any | None) -> Any | None:
        """Return the first non-empty geometry without relying on Shapely truthiness."""
        for item in items:
            if item is not None and not item.is_empty:
                return item
        return None

    if target_type in utilities:
        return utilities[target_type]
    if target_type == "building":
        parts = [normalized.get("building"), normalized.get("building_linework")]
        parts = [item for item in parts if item is not None and not item.is_empty]
        return unary_union(parts) if parts else constraints.get("buildings_in_work_area")
    if target_type == "sidewalk":
        return first_available(normalized.get("sidewalk"), constraints.get("sidewalk_area"))
    if target_type == "road_edge":
        return first_available(normalized.get("road_edge"), constraints.get("road_area"))
    return first_available(normalized.get(target_type), constraints.get(target_type))


def prepare_sidewalk_for_checks(
    normalized: dict[str, Any],
    constraints: dict[str, Any],
    zone_report: dict[str, Any],
    units_per_meter: float,
) -> None:
    """Use the same local sidewalk geometry for point checks and allow zones."""
    setback = max(
        (
            float(rule["min_distance_m"]) * units_per_meter
            for plant in zone_report.get("plant_types", {}).values()
            for rule in plant.get("rules", [])
            if rule.get("target_object_type") == "sidewalk"
            and rule.get("min_distance_m") is not None
        ),
        default=0.0,
    )
    sidewalk = relevant_sidewalk_geometry(
        normalized.get("sidewalk"),
        constraints.get("sidewalk_area"),
        normalized.get("work_boundary", constraints.get("base_allowed_area")),
        setback,
    )
    if sidewalk is None:
        normalized.pop("sidewalk", None)
    else:
        normalized["sidewalk"] = sidewalk


def build_checks(
    point: Point,
    profile: PlantingProfile,
    plant_report: dict[str, Any],
    constraints: dict[str, Any],
    normalized: dict[str, Any],
    utilities: dict[str, Any],
    units_per_meter: float,
    geometry_cache: dict[str, Any | None] | None = None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = [
        {
            "code": "ALLOWED_ZONE",
            "target": "plant allow zone",
            "status": "passed",
            "actual_distance_m": None,
            "required_distance_m": None,
            "norm_reference": "Geometric intersection of all available restrictions",
            "explanation": "Полный габарит посадки находится внутри рассчитанной допустимой зоны.",
        },
        {
            "code": "PLANT_SELECTION",
            "target": profile.species,
            "status": "passed",
            "actual_distance_m": None,
            "required_distance_m": None,
            "norm_reference": profile.catalog_reference,
            "explanation": "; ".join(profile.selection_reasons) or "Вид выбран из проектного каталога растений.",
        },
    ]
    for evaluation in plant_report.get("rules", []):
        target_type = str(evaluation.get("target_object_type", ""))
        if geometry_cache is not None and target_type in geometry_cache:
            geometry = geometry_cache[target_type]
        else:
            geometry = rule_geometry(target_type, constraints, normalized, utilities)
            if geometry_cache is not None:
                geometry_cache[target_type] = geometry
        status = str(evaluation.get("status", "unavailable"))
        required = evaluation.get("min_distance_m")
        actual = None
        if status != "unavailable" and geometry is not None and not geometry.is_empty:
            actual = point.distance(geometry) / units_per_meter
        if status == "applied" and required is not None:
            check_status = "passed" if actual is not None and actual + 1e-7 >= float(required) else "failed"
            explanation = (
                f"Измерено {actual:.3f} м; требуется не менее {float(required):g} м."
                if actual is not None
                else "Исходная геометрия для измерения расстояния недоступна."
            )
        else:
            check_status = "manual_review"
            explanation = str(evaluation.get("reason", "Требуется ручная проверка."))
        checks.append(
            {
                "code": evaluation.get("rule_code"),
                "target": target_type,
                "status": check_status,
                "actual_distance_m": actual,
                "required_distance_m": required,
                "norm_reference": evaluation.get("norm_reference"),
                "explanation": explanation,
                "geometry_source": evaluation.get("geometry_source"),
            }
        )
    return checks


def generate_plan(
    zones_path: Path,
    zone_report_path: Path,
    normalized_path: Path,
    constraint_map_path: Path,
    utilities_path: Path,
    config_path: Path,
    output_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    zones = load_zones(zones_path)
    zone_report = json.loads(zone_report_path.read_text(encoding="utf-8-sig"))
    normalized = load_geojsonl_by_object_type(normalized_path)
    constraints = load_geojsonl_by_object_type(constraint_map_path)
    utilities = load_geojsonl_by_object_type(utilities_path) if utilities_path.exists() else {}
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    profiles = load_profiles(config_path)
    units_per_meter = float(zone_report.get("dxf_units_per_meter", config.get("dxfUnitsPerMeter", 1.0)))
    if not math.isfinite(units_per_meter) or units_per_meter <= 0:
        raise ValueError("dxf_units_per_meter must be finite and positive")
    prepare_sidewalk_for_checks(normalized, constraints, zone_report, units_per_meter)
    accepted: list[tuple[str, float, float]] = []
    features: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    geometry_cache: dict[str, Any | None] = {}
    prefixes = {"tree": "T", "shrub": "S"}
    ordered = sorted(
        (profile for profile in profiles.values() if profile.plant_type in zones),
        key=lambda item: (-item.footprint_radius_m, -item.spacing_m, item.plant_type),
    )
    for profile in ordered:
        zone = zones[profile.plant_type]
        existing_tree_clearance = configured_existing_tree_clearance_m(config, profile)
        scope = safe_scope(
            zone["geometry"],
            profile,
            normalized.get("existing_tree"),
            normalized.get("existing_tree_belt"),
            existing_tree_clearance,
            units_per_meter,
        )
        generated: list[tuple[float, float]] = []
        for component in sorted(polygon_parts(scope), key=lambda item: item.area, reverse=True):
            remaining = profile.max_count - len(generated)
            if remaining <= 0:
                break
            component = component.buffer(-max(0.02 * units_per_meter, 1e-5))
            if component.is_empty:
                continue
            best = best_component_layout(component, profile, profiles, accepted + [
                (profile.plant_type, x, y) for x, y in generated
            ], remaining)
            generated.extend(best)
        plant_report = zone_report.get("plant_types", {}).get(profile.plant_type, {})
        for index, (x, y) in enumerate(generated, start=1):
            point = Point(x, y)
            checks = build_checks(
                point,
                profile,
                plant_report,
                constraints,
                normalized,
                utilities,
                units_per_meter,
                geometry_cache,
            )
            failed = [check for check in checks if check["status"] == "failed"]
            if failed:
                raise ValueError(
                    f"Generated {profile.plant_type} at ({x}, {y}) failed checks: "
                    + ", ".join(str(item["code"]) for item in failed)
                )
            status = "manual_review" if any(
                check["status"] == "manual_review" for check in checks
            ) else "accepted"
            planting_id = f"{prefixes.get(profile.plant_type, profile.plant_type[:1].upper())}-{index:04d}"
            features.append(
                {
                    "type": "Feature",
                    "id": planting_id,
                    "properties": {
                        "object_type": "proposed_planting",
                        "planting_id": planting_id,
                        "plant_type": profile.plant_type,
                        "species": profile.species,
                        "status": status,
                        "spacing_m": profile.spacing_m,
                        "footprint_radius_m": profile.footprint_radius_m,
                        "symbol_radius_m": profile.symbol_radius_m,
                        "coordinate_reference": "local_dxf_coordinates",
                        "dxf_units_per_meter": units_per_meter,
                        "checks": checks,
                    },
                    "geometry": mapping(point),
                }
            )
            accepted.append((profile.plant_type, x, y))
        summary[profile.plant_type] = {
            "count": len(generated),
            "species": profile.species,
            "spacing_m": profile.spacing_m,
            "footprint_radius_m": profile.footprint_radius_m,
            "safe_scope_area_in_dxf_square_units": scope.area,
            "source_zone_status": zone["properties"].get("verification_status"),
        }

    herbaceous_profile = next(
        (item for item in config.get("plantingProfiles", []) if item.get("plantType") == "herbaceous"),
        None,
    )
    base = constraints.get("base_allowed_area")
    if herbaceous_profile is not None and base is not None and not base.is_empty:
        coverage = base
        belts = normalized.get("existing_tree_belt")
        # Existing tree canopies block new woody planting centres, but they do
        # not require circular holes in a lawn or other herbaceous cover.
        if belts is not None and not belts.is_empty:
            coverage = coverage.difference(belts)
        coverage = make_valid(coverage)
        area_count = 0
        total_area = 0.0
        for polygon in polygon_parts(coverage):
            if polygon.area < 0.05 * units_per_meter * units_per_meter:
                continue
            area_count += 1
            total_area += polygon.area
            planting_id = f"H-{area_count:04d}"
            features.append(
                {
                    "type": "Feature",
                    "id": planting_id,
                    "properties": {
                        "object_type": "proposed_planting_area",
                        "planting_id": planting_id,
                        "plant_type": "herbaceous",
                        "species": herbaceous_profile.get("species"),
                        "status": "accepted",
                        "coordinate_reference": "local_dxf_coordinates",
                        "dxf_units_per_meter": units_per_meter,
                        "checks": [
                            {
                                "code": "HERBACEOUS_NPA_CLASSIFICATION",
                                "target": "травяной покров",
                                "status": "passed",
                                "actual_distance_m": None,
                                "required_distance_m": None,
                                "norm_reference": "Постановление Правительства Москвы № 743-ПП, Правила, п. 2.1.13",
                                "explanation": (
                                    "Выбранный тип относится к травяному покрову: пункт 2.1.13 прямо включает "
                                    "в него травянистую растительность искусственного происхождения и все виды газонов."
                                ),
                            },
                            {
                                "code": "BASE_PLANTABLE_SURFACE",
                                "target": "confirmed plantable surface",
                                "status": "passed",
                                "actual_distance_m": None,
                                "required_distance_m": None,
                                "norm_reference": "Расчётные общие физические ограничения",
                                "explanation": "Покрытие находится внутри базовой области озеленения и вне защитных пятен существующей растительности.",
                            }
                        ],
                    },
                    "geometry": mapping(polygon),
                }
            )
        summary["herbaceous"] = {
            "area_count": area_count,
            "area_in_dxf_square_units": total_area,
            "species": herbaceous_profile.get("species"),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(feature, ensure_ascii=False) + "\n" for feature in features),
        encoding="utf-8",
    )
    point_features = [
        feature for feature in features
        if feature["properties"]["object_type"] == "proposed_planting"
    ]
    report = {
        "status": "passed",
        "inputs": {
            "plant_allow_zones": str(zones_path),
            "plant_allow_zones_report": str(zone_report_path),
            "normalized_objects": str(normalized_path),
            "constraint_map": str(constraint_map_path),
            "reconstructed_utilities": str(utilities_path),
            "config": str(config_path),
        },
        "output": str(output_path),
        "coordinate_reference": "local_dxf_coordinates",
        "dxf_units_per_meter": units_per_meter,
        "summary": summary,
        "point_placement_count": len(point_features),
        "manual_review_count": sum(
            feature["properties"]["status"] == "manual_review"
            for feature in point_features
        ),
        "failed_check_count": 0,
        # Backward-compatible total; point-only and all-feature counts are explicit below.
        "unique_id_count": len({feature["id"] for feature in features}),
        "unique_point_id_count": len({feature["id"] for feature in point_features}),
        "unique_feature_id_count": len({feature["id"] for feature in features}),
        "feature_count": len(features),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate concrete planting points and coverage areas.")
    parser.add_argument("plant_allow_zones", type=Path)
    parser.add_argument("plant_allow_zones_report", type=Path)
    parser.add_argument("normalized_objects", type=Path)
    parser.add_argument("constraint_map", type=Path)
    parser.add_argument("--utilities", type=Path, default=Path("reconstructed_utilities.geojsonl"))
    parser.add_argument("--config", type=Path, default=Path("nanocad-plugin/config/greenai.plugin.json"))
    parser.add_argument("--output", type=Path, default=Path("planting_plan.geojsonl"))
    parser.add_argument("--report", type=Path, default=Path("planting_plan_report.json"))
    args = parser.parse_args()
    try:
        report = generate_plan(
            args.plant_allow_zones,
            args.plant_allow_zones_report,
            args.normalized_objects,
            args.constraint_map,
            args.utilities,
            args.config,
            args.output,
            args.report,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Planting plan generation error: {error}") from error
    print(f"Output: {args.output}")
    print(f"Report: {args.report}")
    for plant_type, item in report["summary"].items():
        print(f"  {plant_type}: {item.get('count', item.get('area_count', 0))}")


if __name__ == "__main__":
    main()
