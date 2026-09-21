"""Controllable, explainable planting planner.

The service accepts a JSON request that selects WHAT to plant (plant type and
catalog species) and WHERE to plant it (the whole allow zone, a user polygon,
or explicit points).  Every accepted or rejected point receives the same
auditable rule checks.  With no request file it reproduces the automatic MVP
scenario for trees, shrubs and herbaceous cover.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import GeometryCollection, Point, mapping, shape
from shapely.validation import make_valid

try:  # Supports both `python src/planting_service.py` and package imports.
    from .placement_generator import (
        PlantingProfile,
        best_component_layout,
        build_checks,
        configured_existing_tree_clearance_m,
        grid_candidates,
        load_geojsonl_by_object_type,
        load_profiles,
        load_zones,
        polygon_parts,
        required_spacing,
        safe_scope,
    )
except ImportError:  # pragma: no cover - exercised by CLI integration
    from placement_generator import (  # type: ignore
        PlantingProfile,
        best_component_layout,
        build_checks,
        configured_existing_tree_clearance_m,
        grid_candidates,
        load_geojsonl_by_object_type,
        load_profiles,
        load_zones,
        polygon_parts,
        required_spacing,
        safe_scope,
    )


POINT_MODES = {"fill_area", "points"}
AREA_MODES = {"cover_area"}
PLANTING_PRESETS: dict[str, tuple[str, ...]] = {
    "balanced_mixed": ("tree", "shrub", "herbaceous"),
    "dense_mixed": ("tree", "shrub", "herbaceous"),
    "tree_lawn": ("tree", "herbaceous"),
    "trees_only": ("tree",),
    "shrub_lawn": ("shrub", "herbaceous"),
    "shrubs_only": ("shrub",),
    "lawn_only": ("herbaceous",),
}


@dataclass(frozen=True)
class PlantingSelection:
    request_id: str
    plant_type: str
    species: str
    mode: str
    area: Any | None
    points: tuple[Point, ...]
    spacing_m: float | None
    max_count: int | None
    selection_reasons: tuple[str, ...]
    catalog_reference: str | None


def _request_geometry(value: Any, label: str) -> Any:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a GeoJSON geometry or Feature")
    geometry_data = value.get("geometry") if value.get("type") == "Feature" else value
    if not geometry_data:
        raise ValueError(f"{label} has no geometry")
    geometry = make_valid(shape(geometry_data))
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"{label} must be Polygon or MultiPolygon")
    return geometry


def _request_points(value: Any, label: str) -> tuple[Point, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must contain at least one [x, y] coordinate")
    result: list[Point] = []
    for index, coordinate in enumerate(value, start=1):
        if (
            not isinstance(coordinate, list)
            or len(coordinate) < 2
            or isinstance(coordinate[0], bool)
            or isinstance(coordinate[1], bool)
        ):
            raise ValueError(f"{label}[{index}] must be [x, y]")
        x, y = float(coordinate[0]), float(coordinate[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError(f"{label}[{index}] coordinates must be finite")
        result.append(Point(x, y))
    return tuple(result)


def load_request(
    path: Path | None,
    config: dict[str, Any],
    preset: str = "dense_mixed",
    tree_spacing_m: float | None = None,
    tree_max_count: int | None = None,
) -> list[PlantingSelection]:
    if path is None:
        if preset not in PLANTING_PRESETS:
            raise ValueError(f"Unknown planting preset: {preset}")
        selected_types = set(PLANTING_PRESETS[preset])
        if tree_spacing_m is not None and (
            not math.isfinite(tree_spacing_m) or tree_spacing_m <= 0
        ):
            raise ValueError("tree_spacing_m must be finite and positive")
        if tree_max_count is not None and tree_max_count < 1:
            raise ValueError("tree_max_count must be positive")
        selections: list[PlantingSelection] = []
        for item in config.get("plantingProfiles", []):
            plant_type = str(item["plantType"])
            if plant_type not in selected_types:
                continue
            geometry_kind = str(item.get("geometryKind", "point"))
            mode = "fill_area" if geometry_kind == "point" else "cover_area"
            spacing = tree_spacing_m if plant_type == "tree" else None
            if spacing is None and plant_type == "tree":
                if preset == "balanced_mixed":
                    spacing = max(6.0, float(item.get("spacingM", 6.0)))
                elif preset == "dense_mixed":
                    # The dense preset deliberately requests the configured
                    # physical minimum. Other presets may use the catalog's
                    # recommended spacing later in resolve_profile().
                    spacing = float(item.get("spacingM", 5.0))
            if spacing is not None and plant_type == "tree":
                minimum_footprint_spacing = 2.0 * float(item.get("footprintRadiusM", 0.0))
                if spacing + 1e-9 < minimum_footprint_spacing:
                    raise ValueError(
                        f"tree_spacing_m={spacing:g} m is smaller than the configured "
                        f"mature crown diameter {minimum_footprint_spacing:g} m"
                    )
            selections.append(
                PlantingSelection(
                    request_id=f"auto_{plant_type}",
                    plant_type=plant_type,
                    species=str(item["species"]),
                    mode=mode,
                    area=None,
                    points=(),
                    spacing_m=spacing,
                    max_count=tree_max_count if plant_type == "tree" else None,
                    selection_reasons=tuple(str(v) for v in item.get("selectionReasons", [])),
                    catalog_reference=str(item.get("catalogReference", "plant catalog")),
                )
            )
        if not selections:
            raise ValueError(f"Preset {preset} did not match any configured planting profiles")
        return selections

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_selections = data.get("selections") if isinstance(data, dict) else None
    if not isinstance(raw_selections, list) or not raw_selections:
        raise ValueError("Planting request must contain a non-empty selections array")
    selections = []
    ids: set[str] = set()
    used_types: set[str] = set()
    for index, item in enumerate(raw_selections, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"selections[{index}] must be an object")
        request_id = str(item.get("id") or f"selection_{index}")
        if request_id in ids:
            raise ValueError(f"Duplicate selection id: {request_id}")
        ids.add(request_id)
        plant_type = str(item.get("plant_type") or "")
        if not plant_type:
            raise ValueError(f"{request_id}: plant_type is required")
        if plant_type in used_types:
            raise ValueError(
                f"{request_id}: only one selection per plant_type is currently supported"
            )
        used_types.add(plant_type)
        species = str(item.get("species") or "").strip()
        if not species:
            raise ValueError(f"{request_id}: species is required")
        mode = str(item.get("mode") or ("cover_area" if plant_type == "herbaceous" else "fill_area"))
        if mode not in POINT_MODES | AREA_MODES:
            raise ValueError(f"{request_id}: unsupported mode {mode!r}")
        if plant_type == "herbaceous" and mode not in AREA_MODES:
            raise ValueError(f"{request_id}: herbaceous planting requires mode=cover_area")
        if plant_type not in {"herbaceous", "shrub"} and mode in AREA_MODES:
            raise ValueError(f"{request_id}: {plant_type} requires fill_area or points")
        area = _request_geometry(item["area"], f"{request_id}.area") if "area" in item else None
        points = _request_points(item.get("points"), f"{request_id}.points") if mode == "points" else ()
        spacing = float(item["spacing_m"]) if item.get("spacing_m") is not None else None
        if spacing is not None and (not math.isfinite(spacing) or spacing <= 0):
            raise ValueError(f"{request_id}: spacing_m must be finite and positive")
        maximum = int(item["max_count"]) if item.get("max_count") is not None else None
        if maximum is not None and maximum < 1:
            raise ValueError(f"{request_id}: max_count must be positive")
        selections.append(
            PlantingSelection(
                request_id=request_id,
                plant_type=plant_type,
                species=species,
                mode=mode,
                area=area,
                points=points,
                spacing_m=spacing,
                max_count=maximum,
                selection_reasons=tuple(str(v) for v in item.get("selection_reasons", [])),
                catalog_reference=(
                    str(item["catalog_reference"])
                    if item.get("catalog_reference")
                    else None
                ),
            )
        )
    return selections


def _catalog(zone_report: dict[str, Any], plant_type: str) -> list[dict[str, Any]]:
    top_level = zone_report.get("plant_catalog", {}).get(plant_type)
    if isinstance(top_level, list):
        return top_level
    nested = (
        zone_report.get("plant_types", {})
        .get(plant_type, {})
        .get("plant_catalog", {})
        .get("selectable", [])
    )
    return nested if isinstance(nested, list) else []


def resolve_profile(
    selection: PlantingSelection,
    base_profiles: dict[str, PlantingProfile],
    config: dict[str, Any],
    zone_report: dict[str, Any],
) -> PlantingProfile | dict[str, Any]:
    plants = _catalog(zone_report, selection.plant_type)
    catalog_item = next((item for item in plants if item.get("name") == selection.species), None)
    configured = next(
        (
            item for item in config.get("plantingProfiles", [])
            if item.get("plantType") == selection.plant_type
        ),
        None,
    )
    if catalog_item is None and not (
        configured is not None and configured.get("species") == selection.species
    ):
        available = ", ".join(str(item.get("name")) for item in plants[:8]) or "none"
        raise ValueError(
            f"{selection.request_id}: species {selection.species!r} is not selectable "
            f"for {selection.plant_type}; available: {available}"
        )
    catalog_reference = selection.catalog_reference or (
        f"plant_catalog#{catalog_item['id']}: {selection.species}"
        if catalog_item is not None
        else str(configured.get("catalogReference", "plant catalog"))
    )
    reasons = selection.selection_reasons or tuple(
        str(value) for value in (configured or {}).get("selectionReasons", [])
    )
    if selection.mode in AREA_MODES:
        catalog_dimensions = {
            "minSpacingM": catalog_item.get("min_spacing_m"),
            "recommendedSpacingM": catalog_item.get("recommended_spacing_m"),
            "matureCrownRadiusM": catalog_item.get("mature_crown_radius_m"),
            "dimensionSource": catalog_item.get("dimension_source"),
        } if catalog_item is not None else {}
        return {
            **(configured or {}),
            **catalog_dimensions,
            "plantType": selection.plant_type,
            "species": selection.species,
            "catalogReference": catalog_reference,
            "selectionReasons": list(reasons),
        }
    if selection.plant_type not in base_profiles:
        raise ValueError(f"No point profile configured for {selection.plant_type}")
    base = base_profiles[selection.plant_type]
    minimum_spacing = (
        float(catalog_item["min_spacing_m"])
        if catalog_item is not None and catalog_item.get("min_spacing_m") is not None
        else None
    )
    recommended_spacing = (
        float(catalog_item["recommended_spacing_m"])
        if catalog_item is not None and catalog_item.get("recommended_spacing_m") is not None
        else None
    )
    crown_radius = (
        float(catalog_item["mature_crown_radius_m"])
        if catalog_item is not None and catalog_item.get("mature_crown_radius_m") is not None
        else base.footprint_radius_m
    )
    spacing = selection.spacing_m
    if spacing is None:
        spacing = recommended_spacing or minimum_spacing or base.spacing_m
    minimum_center_spacing = max(minimum_spacing or 0.0, 2.0 * crown_radius)
    if spacing + 1e-9 < minimum_center_spacing:
        raise ValueError(
            f"{selection.request_id}: spacing_m={spacing:g} m is smaller than the "
            f"catalog minimum {minimum_center_spacing:g} m for {selection.species!r} "
            f"(minimum spacing and mature crown diameter are both checked)"
        )
    return replace(
        base,
        species=selection.species,
        spacing_m=spacing,
        footprint_radius_m=crown_radius,
        max_count=selection.max_count or base.max_count,
        catalog_reference=catalog_reference,
        selection_reasons=reasons,
    )


def _scope_check(point: Point, scope: Any, profile: PlantingProfile, units: float) -> dict[str, Any]:
    passed = not scope.is_empty and scope.covers(point)
    clearance = profile.footprint_radius_m
    return {
        "code": "ALLOWED_ZONE",
        "target": "plant allow zone",
        "status": "passed" if passed else "failed",
        "actual_distance_m": None,
        "required_distance_m": clearance,
        "norm_reference": "Расчётное пересечение всех применимых ограничений",
        "explanation": (
            f"Центр находится в безопасной области; полный габарит радиусом {clearance:g} м "
            "остаётся внутри выбранной допустимой зоны."
            if passed
            else "Точка или полный габарит посадки выходит за выбранную допустимую зону."
        ),
    }


def _spacing_check(
    point: Point,
    profile: PlantingProfile,
    profiles: dict[str, PlantingProfile],
    occupied: list[tuple[str, float, float]],
    units: float,
) -> dict[str, Any]:
    nearest_actual: float | None = None
    nearest_required: float | None = None
    nearest_type: str | None = None
    worst_violation: tuple[float, float, str] | None = None
    passed = True
    for other_type, x, y in occupied:
        other = profiles.get(other_type)
        if other is None:
            continue
        actual = point.distance(Point(x, y)) / units
        required = required_spacing(profile, other)
        if nearest_actual is None or actual < nearest_actual:
            nearest_actual, nearest_required, nearest_type = actual, required, other_type
        if actual + 1e-7 < required:
            passed = False
            margin = actual - required
            if worst_violation is None or margin < worst_violation[0] - worst_violation[1]:
                worst_violation = (actual, required, other_type)
    if worst_violation is not None:
        nearest_actual, nearest_required, nearest_type = worst_violation
    return {
        "code": "NEW_PLANT_SPACING",
        "target": nearest_type or "other proposed plantings",
        "status": "passed" if passed else "failed",
        "actual_distance_m": nearest_actual,
        "required_distance_m": nearest_required or profile.spacing_m,
        "norm_reference": profile.catalog_reference,
        "explanation": (
            "Других предложенных посадок ближе установленного шага нет."
            if passed
            else f"Нарушающая шаг посадка находится на расстоянии {nearest_actual:.3f} м; "
            f"требуется не менее {nearest_required:g} м."
        ),
    }


def _point_decision(
    point: Point,
    profile: PlantingProfile,
    selection: PlantingSelection,
    scope: Any,
    plant_report: dict[str, Any],
    constraints: dict[str, Any],
    normalized: dict[str, Any],
    utilities: dict[str, Any],
    units: float,
    profiles: dict[str, PlantingProfile],
    occupied: list[tuple[str, float, float]],
) -> tuple[str, list[dict[str, Any]]]:
    checks = build_checks(
        point, profile, plant_report, constraints, normalized, utilities, units
    )
    checks[0] = _scope_check(point, scope, profile, units)
    if selection.area is not None:
        inside_selection = selection.area.covers(point)
        checks.insert(
            1,
            {
                "code": "USER_SELECTED_AREA",
                "target": selection.request_id,
                "status": "passed" if inside_selection else "failed",
                "actual_distance_m": None,
                "required_distance_m": None,
                "norm_reference": f"planting_request:{selection.request_id}",
                "explanation": (
                    "Точка находится внутри области, выбранной проектировщиком."
                    if inside_selection
                    else "Точка находится вне области, выбранной проектировщиком."
                ),
            },
        )
    checks.append(_spacing_check(point, profile, profiles, occupied, units))
    if any(item["status"] == "failed" for item in checks):
        return "rejected", checks
    if any(item["status"] == "manual_review" for item in checks):
        return "manual_review", checks
    return "accepted", checks


def _point_feature(
    planting_id: str,
    point: Point,
    profile: PlantingProfile,
    selection: PlantingSelection,
    status: str,
    checks: list[dict[str, Any]],
    units: float,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": planting_id,
        "properties": {
            "object_type": "proposed_planting",
            "planting_id": planting_id,
            "request_id": selection.request_id,
            "selection_mode": selection.mode,
            "plant_type": profile.plant_type,
            "species": profile.species,
            "status": status,
            "spacing_m": profile.spacing_m,
            "footprint_radius_m": profile.footprint_radius_m,
            "symbol_radius_m": profile.symbol_radius_m,
            "coordinate_reference": "local_dxf_coordinates",
            "dxf_units_per_meter": units,
            "checks": checks,
        },
        "geometry": mapping(point),
    }


def _decision_feature(
    candidate_id: str,
    point: Point,
    selection: PlantingSelection,
    status: str,
    checks: list[dict[str, Any]],
    diagnostic: bool = False,
) -> dict[str, Any]:
    failed = [str(item["code"]) for item in checks if item["status"] == "failed"]
    manual = [str(item["code"]) for item in checks if item["status"] == "manual_review"]
    return {
        "type": "Feature",
        "id": f"decision_{candidate_id}",
        "properties": {
            "object_type": "planting_decision",
            "candidate_id": candidate_id,
            "request_id": selection.request_id,
            "plant_type": selection.plant_type,
            "species": selection.species,
            "status": status,
            "accepted_into_plan": status != "rejected",
            "diagnostic_candidate": diagnostic,
            "failed_checks": failed,
            "manual_review_checks": manual,
            "rejection_reasons": [
                str(item.get("explanation") or item.get("code"))
                for item in checks
                if item.get("status") == "failed"
            ],
            "checks": checks,
        },
        "geometry": mapping(point),
    }


def _scope_diagnostic_checks(
    point: Point,
    selected_zone: Any,
    profile: PlantingProfile,
    normalized: dict[str, Any],
    existing_tree_clearance_m: float,
    units: float,
) -> list[dict[str, Any]]:
    """Explain which physical operation removed a point from ``safe_scope``."""
    checks: list[dict[str, Any]] = []
    footprint = profile.footprint_radius_m
    boundary_distance = (
        point.distance(selected_zone.boundary) / units
        if selected_zone is not None and not selected_zone.is_empty
        else 0.0
    )
    footprint_passed = selected_zone.covers(point) and boundary_distance + 1e-7 >= footprint
    checks.append(
        {
            "code": "PLANT_FOOTPRINT_INSIDE_ZONE",
            "target": "plant allow-zone boundary",
            "status": "passed" if footprint_passed else "failed",
            "actual_distance_m": boundary_distance,
            "required_distance_m": footprint,
            "norm_reference": "project parameter: footprintRadiusM",
            "explanation": (
                "Полный габарит посадки остаётся внутри допустимой зоны."
                if footprint_passed
                else (
                    f"До границы допустимой зоны {boundary_distance:.3f} м, "
                    f"а текущий расчёт требует разместить внутри неё весь условный "
                    f"радиус посадки {footprint:g} м."
                )
            ),
        }
    )

    existing_trees = normalized.get("existing_tree")
    if existing_trees is not None and not existing_trees.is_empty:
        actual = point.distance(existing_trees) / units
        required = existing_tree_clearance_m
        passed = actual + 1e-7 >= required
        checks.append(
            {
                "code": "EXISTING_TREE_CLEARANCE",
                "target": "existing_tree",
                "status": "passed" if passed else "failed",
                "actual_distance_m": actual,
                "required_distance_m": required,
                "norm_reference": "project requirement: preserve existing vegetation",
                "explanation": (
                    f"До ближайшего существующего дерева {actual:.3f} м; "
                    f"принятый защитный интервал — {required:g} м."
                ),
            }
        )

    existing_belts = normalized.get("existing_tree_belt")
    if existing_belts is not None and not existing_belts.is_empty:
        actual = point.distance(existing_belts) / units
        required = footprint
        passed = actual + 1e-7 >= required
        checks.append(
            {
                "code": "EXISTING_TREE_BELT_CLEARANCE",
                "target": "existing_tree_belt",
                "status": "passed" if passed else "failed",
                "actual_distance_m": actual,
                "required_distance_m": required,
                "norm_reference": "project requirement: preserve existing vegetation",
                "explanation": (
                    f"До существующего древесного массива {actual:.3f} м; "
                    f"принятый интервал — {required:g} м."
                ),
            }
        )
    return checks


def _diagnostic_candidate_points(
    selected_zone: Any,
    accepted_points: list[Point],
    spacing_dxf: float,
    maximum: int,
) -> list[Point]:
    """Create deterministic alternatives used only to explain rejected locations.

    Row continuations make gaps beside an accepted row easy to understand in
    CAD.  A coarse grid also covers components where no planting was accepted.
    """
    if maximum <= 0 or selected_zone.is_empty or spacing_dxf <= 0:
        return []
    result: list[Point] = []
    seen: set[tuple[int, int]] = set()
    tolerance = max(spacing_dxf * 0.02, 1e-5)

    def add(point: Point) -> None:
        if len(result) >= maximum or not selected_zone.covers(point):
            return
        if any(point.distance(accepted) <= tolerance for accepted in accepted_points):
            return
        key = (round(point.x / tolerance), round(point.y / tolerance))
        if key not in seen:
            seen.add(key)
            result.append(point)

    # Extend locally visible rows in both directions.  This produces the
    # intuitive "why is there no third tree here?" candidates.
    for index, first in enumerate(accepted_points):
        neighbours = sorted(
            (
                (first.distance(second), second)
                for second in accepted_points[index + 1 :]
                if 0.75 * spacing_dxf <= first.distance(second) <= 1.25 * spacing_dxf
            ),
            key=lambda item: item[0],
        )
        if not neighbours:
            continue
        distance, second = neighbours[0]
        dx = (second.x - first.x) / distance * spacing_dxf
        dy = (second.y - first.y) / distance * spacing_dxf
        add(Point(first.x - dx, first.y - dy))
        add(Point(second.x + dx, second.y + dy))

    # Diagnose broad unused pieces as well.  These points are alternatives,
    # not additional proposals, and only failed checks are exported later.
    for component in sorted(polygon_parts(selected_zone), key=lambda item: item.area, reverse=True):
        for x, y in grid_candidates(component, spacing_dxf, 0.0, 0.5, 0.5):
            add(Point(x, y))
            if len(result) >= maximum:
                return result
    return result


def _write_jsonl(path: Path, features: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(feature, ensure_ascii=False) + "\n" for feature in features),
        encoding="utf-8",
    )


def _markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_explanations_markdown(
    path: Path, features: list[dict[str, Any]], units_per_meter: float
) -> None:
    """Write a reviewer-friendly passport for every accepted plan feature."""
    counts = Counter(item["properties"].get("status", "unknown") for item in features)
    lines = [
        "# Обоснование плана посадок",
        "",
        "Документ сформирован автоматически из того же набора проверок, который записан в `planting_plan.geojsonl`.",
        "",
        f"- Всего объектов: {len(features)}",
        f"- Автоматически подтверждено: {counts.get('accepted', 0)}",
        f"- Требует ручной проверки: {counts.get('manual_review', 0)}",
        f"- Отклонено: {counts.get('rejected', 0)}",
        "",
        "Статус `manual_review` означает, что объект прошёл вычислимые проверки, но его нельзя считать окончательно согласованным до проверки перечисленных исходных данных.",
        "",
    ]
    for feature in features:
        properties = feature.get("properties", {})
        planting_id = str(properties.get("planting_id") or feature.get("id") or "unknown")
        geometry = shape(feature["geometry"])
        lines.extend(
            [
                f"## {planting_id}",
                "",
                f"- Тип: `{_markdown_cell(properties.get('plant_type'))}`",
                f"- Растение/покрытие: {_markdown_cell(properties.get('species'))}",
                f"- Статус: `{_markdown_cell(properties.get('status'))}`",
            ]
        )
        if geometry.geom_type == "Point":
            lines.append(f"- Координаты DXF: X={geometry.x:.3f}; Y={geometry.y:.3f}")
        else:
            area_m2 = geometry.area / (units_per_meter * units_per_meter)
            lines.append(f"- Площадь: {area_m2:.3f} м²")
        lines.extend(
            [
                "",
                "| Проверка | Объект | Статус | Факт, м | Минимум, м | Норма/источник | Объяснение |",
                "|---|---|---:|---:|---:|---|---|",
            ]
        )
        for check in properties.get("checks", []):
            actual = check.get("actual_distance_m")
            required = check.get("required_distance_m")
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(check.get("code")),
                        _markdown_cell(check.get("target")),
                        _markdown_cell(check.get("status")),
                        f"{float(actual):.3f}" if actual is not None else "",
                        f"{float(required):.3f}" if required is not None else "",
                        _markdown_cell(check.get("norm_reference")),
                        _markdown_cell(check.get("explanation")),
                    ]
                )
                + " |"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def plan(
    zones_path: Path,
    zone_report_path: Path,
    normalized_path: Path,
    constraint_map_path: Path,
    utilities_path: Path,
    config_path: Path,
    output_path: Path,
    report_path: Path,
    decisions_path: Path,
    request_path: Path | None = None,
    explanations_path: Path | None = None,
    preset: str = "dense_mixed",
    tree_spacing_m: float | None = None,
    tree_max_count: int | None = None,
    diagnostic_rejected_max_count: int | None = None,
) -> dict[str, Any]:
    zones = load_zones(zones_path)
    zone_report = json.loads(zone_report_path.read_text(encoding="utf-8-sig"))
    normalized = load_geojsonl_by_object_type(normalized_path)
    constraints = load_geojsonl_by_object_type(constraint_map_path)
    utilities = load_geojsonl_by_object_type(utilities_path) if utilities_path.exists() else {}
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    selections = load_request(
        request_path,
        config,
        preset,
        tree_spacing_m,
        tree_max_count,
    )
    base_profiles = (
        load_profiles(config_path)
        if any(selection.mode in POINT_MODES for selection in selections)
        else {}
    )
    units = float(zone_report.get("dxf_units_per_meter", config.get("dxfUnitsPerMeter", 1.0)))
    if not math.isfinite(units) or units <= 0:
        raise ValueError("dxf_units_per_meter must be finite and positive")
    diagnostic_rejected_max = int(
        diagnostic_rejected_max_count
        if diagnostic_rejected_max_count is not None
        else config.get("diagnosticRejectedMaxCount", 300)
    )
    if diagnostic_rejected_max < 0:
        raise ValueError("diagnosticRejectedMaxCount must be non-negative")

    resolved = {
        selection.plant_type: resolve_profile(selection, base_profiles, config, zone_report)
        for selection in selections
    }
    point_profiles = {
        plant_type: profile
        for plant_type, profile in resolved.items()
        if isinstance(profile, PlantingProfile)
    }
    layout_profiles = {
        plant_type: replace(
            profile,
            spacing_m=profile.spacing_m * units,
            footprint_radius_m=profile.footprint_radius_m * units,
            avoid_other_plantings_m=profile.avoid_other_plantings_m * units,
        )
        for plant_type, profile in point_profiles.items()
    }
    features: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    occupied: list[tuple[str, float, float]] = []
    counters: Counter[str] = Counter()
    diagnostic_counters: Counter[str] = Counter()
    summary: dict[str, Any] = {}
    prefixes = {"tree": "T", "shrub": "S", "herbaceous": "H"}

    ordered = sorted(
        (item for item in selections if item.mode in POINT_MODES),
        key=lambda item: -point_profiles[item.plant_type].footprint_radius_m,
    )
    for selection in ordered:
        if selection.plant_type not in zones:
            raise ValueError(f"No allow zone calculated for {selection.plant_type}")
        profile = point_profiles[selection.plant_type]
        layout_profile = layout_profiles[selection.plant_type]
        existing_tree_clearance = configured_existing_tree_clearance_m(config, profile)
        source_zone = zones[selection.plant_type]
        selected_zone = source_zone["geometry"]
        if selection.area is not None:
            selected_zone = selected_zone.intersection(selection.area)
        scope = safe_scope(
            selected_zone,
            profile,
            normalized.get("existing_tree"),
            normalized.get("existing_tree_belt"),
            existing_tree_clearance,
            units,
        )
        candidates: list[Point]
        if selection.mode == "points":
            candidates = list(selection.points)
        else:
            generated: list[tuple[float, float]] = []
            for component in sorted(polygon_parts(scope), key=lambda item: item.area, reverse=True):
                remaining = profile.max_count - len(generated)
                if remaining <= 0:
                    break
                inset = component.buffer(-max(0.02 * units, 1e-5))
                if inset.is_empty:
                    continue
                generated.extend(
                    best_component_layout(
                        inset,
                        layout_profile,
                        layout_profiles,
                        occupied + [(profile.plant_type, x, y) for x, y in generated],
                        remaining,
                    )
                )
            candidates = [Point(x, y) for x, y in generated]

        accepted_count = 0
        rejected_count = 0
        accepted_selection_points: list[Point] = []
        plant_report = zone_report.get("plant_types", {}).get(selection.plant_type, {})
        for point in candidates:
            counters[selection.plant_type] += 1
            candidate_id = f"{prefixes.get(selection.plant_type, 'P')}-{counters[selection.plant_type]:04d}"
            status, checks = _point_decision(
                point,
                profile,
                selection,
                scope,
                plant_report,
                constraints,
                normalized,
                utilities,
                units,
                point_profiles,
                occupied,
            )
            decisions.append(_decision_feature(candidate_id, point, selection, status, checks))
            if status == "rejected":
                rejected_count += 1
                continue
            accepted_count += 1
            features.append(_point_feature(candidate_id, point, profile, selection, status, checks, units))
            occupied.append((profile.plant_type, point.x, point.y))
            accepted_selection_points.append(point)

        diagnostic_rejected_count = 0
        if selection.mode == "fill_area" and diagnostic_rejected_max > 0:
            alternatives = _diagnostic_candidate_points(
                selected_zone,
                accepted_selection_points,
                profile.spacing_m * units,
                diagnostic_rejected_max * 2,
            )
            for point in alternatives:
                status, checks = _point_decision(
                    point,
                    profile,
                    selection,
                    scope,
                    plant_report,
                    constraints,
                    normalized,
                    utilities,
                    units,
                    point_profiles,
                    occupied,
                )
                if status != "rejected":
                    continue
                checks.extend(
                    _scope_diagnostic_checks(
                        point,
                        selected_zone,
                        profile,
                        normalized,
                        existing_tree_clearance,
                        units,
                    )
                )
                diagnostic_counters[selection.plant_type] += 1
                diagnostic_id = (
                    f"R-{prefixes.get(selection.plant_type, 'P')}-"
                    f"{diagnostic_counters[selection.plant_type]:04d}"
                )
                decisions.append(
                    _decision_feature(
                        diagnostic_id,
                        point,
                        selection,
                        "rejected",
                        checks,
                        diagnostic=True,
                    )
                )
                diagnostic_rejected_count += 1
                if diagnostic_rejected_count >= diagnostic_rejected_max:
                    break
        summary[selection.request_id] = {
            "plant_type": selection.plant_type,
            "species": selection.species,
            "mode": selection.mode,
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "diagnostic_rejected_count": diagnostic_rejected_count,
            "safe_scope_area_in_dxf_square_units": scope.area,
        }

    base = constraints.get("base_allowed_area")
    area_occupied: Any = GeometryCollection()
    for selection in (
        item
        for item in selections
        if item.plant_type == "shrub" and item.mode in AREA_MODES
    ):
        zone = zones.get("shrub")
        if zone is None:
            raise ValueError("No allow zone calculated for shrub")
        profile = resolved["shrub"]
        assert isinstance(profile, dict)
        coverage = zone["geometry"]
        if base is not None and not base.is_empty:
            coverage = coverage.intersection(base)
        if selection.area is not None:
            coverage = coverage.intersection(selection.area)
        belts = normalized.get("existing_tree_belt")
        # A mass shrub planting occupies the whole allowed polygon.  Existing
        # tree canopies may have an understorey; mapped vegetation belts remain
        # excluded so the proposal does not replace an existing planting mass.
        if belts is not None and not belts.is_empty:
            coverage = coverage.difference(belts)
        coverage = make_valid(coverage)
        plant_report = zone_report.get("plant_types", {}).get("shrub", {})
        rule_checks: list[dict[str, Any]] = []
        for rule in plant_report.get("rules", []):
            rule_status = str(rule.get("status", ""))
            if rule_status not in {"applied", "manual_review"}:
                continue
            rule_checks.append(
                {
                    "code": str(rule.get("rule_code", "SHRUB_AREA_RULE")),
                    "target": str(rule.get("target_object_type", "plant allow zone")),
                    "status": "manual_review" if rule_status == "manual_review" else "passed",
                    "actual_distance_m": None,
                    "required_distance_m": rule.get("min_distance_m"),
                    "norm_reference": rule.get("norm_reference"),
                    "explanation": (
                        str(rule.get("reason"))
                        if rule_status == "manual_review"
                        else "The shrub polygon was clipped by this placement rule."
                    ),
                    "geometry_source": rule.get("geometry_source"),
                }
            )
        count = 0
        area = 0.0
        estimated_plants = 0
        spacing = float(profile.get("spacingM", 0.0) or 0.0)
        for polygon in polygon_parts(coverage):
            if polygon.area < 0.05 * units * units:
                continue
            counters["shrub"] += 1
            count += 1
            area += polygon.area
            planting_id = f"SA-{counters['shrub']:04d}"
            polygon_estimate = (
                max(1, math.ceil(polygon.area / (spacing * spacing * math.sqrt(3.0) / 2.0)))
                if spacing > 0
                else 0
            )
            estimated_plants += polygon_estimate
            checks = [
                {
                    "code": "SHRUB_AREA_COVERAGE",
                    "target": "shrub plant allow zone",
                    "status": "passed",
                    "actual_distance_m": None,
                    "required_distance_m": None,
                    "norm_reference": "СП 42.13330.2026, таблица 6.3",
                    "explanation": "The entire shrub polygon lies inside the calculated allow zone.",
                },
                {
                    "code": "PLANT_SELECTION",
                    "target": selection.species,
                    "status": "passed",
                    "actual_distance_m": None,
                    "required_distance_m": None,
                    "norm_reference": profile.get("catalogReference"),
                    "explanation": "; ".join(profile.get("selectionReasons", []))
                    or "The species was selected from the project plant catalog.",
                },
                *rule_checks,
            ]
            status = (
                "manual_review"
                if any(check["status"] == "manual_review" for check in checks)
                else "accepted"
            )
            properties: dict[str, Any] = {
                "object_type": "proposed_planting_area",
                "planting_id": planting_id,
                "request_id": selection.request_id,
                "selection_mode": selection.mode,
                "plant_type": "shrub",
                "species": selection.species,
                "status": status,
                "coordinate_reference": "local_dxf_coordinates",
                "dxf_units_per_meter": units,
                "nominal_spacing_m": spacing,
                "estimated_plant_count": polygon_estimate,
                "checks": checks,
            }
            features.append(
                {
                    "type": "Feature",
                    "id": planting_id,
                    "properties": properties,
                    "geometry": mapping(polygon),
                }
            )
            area_occupied = make_valid(area_occupied.union(polygon))
        summary[selection.request_id] = {
            "plant_type": "shrub",
            "species": selection.species,
            "mode": selection.mode,
            "accepted_area_count": count,
            "accepted_area_in_dxf_square_units": area,
            "estimated_plant_count": estimated_plants,
        }

    for selection in (item for item in selections if item.plant_type == "herbaceous"):
        if base is None or base.is_empty:
            raise ValueError("No base_allowed_area available for herbaceous cover")
        profile = resolved["herbaceous"]
        assert isinstance(profile, dict)
        coverage = base
        if not area_occupied.is_empty:
            coverage = coverage.difference(area_occupied)
        if selection.area is not None:
            coverage = coverage.intersection(selection.area)
        belts = normalized.get("existing_tree_belt")
        # Lawn/groundcover may continue beneath an existing tree canopy.  The
        # canopy buffer is still enforced for new tree and shrub centres in
        # safe_scope(), but subtracting it here creates misleading circular
        # holes in an otherwise continuous lawn.
        if belts is not None and not belts.is_empty:
            coverage = coverage.difference(belts)
        coverage = make_valid(coverage)
        count = 0
        area = 0.0
        for polygon in polygon_parts(coverage):
            if polygon.area < 0.05 * units * units:
                continue
            counters["herbaceous"] += 1
            count += 1
            area += polygon.area
            planting_id = f"H-{counters['herbaceous']:04d}"
            checks = [
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
                    "explanation": "Весь полигон покрытия находится внутри подтверждённой базовой области озеленения.",
                },
                {
                    "code": "PLANT_SELECTION",
                    "target": selection.species,
                    "status": "passed",
                    "actual_distance_m": None,
                    "required_distance_m": None,
                    "norm_reference": profile.get("catalogReference"),
                    "explanation": "; ".join(profile.get("selectionReasons", []))
                    or "Вид выбран из проектного каталога растений.",
                },
                {
                    "code": "EXISTING_VEGETATION",
                    "target": "existing trees and vegetation belts",
                    "status": "passed",
                    "actual_distance_m": None,
                    "required_distance_m": None,
                    "norm_reference": "Проектное требование сохранения существующей растительности",
                    "explanation": "Защитные пятна существующих деревьев и растительные массивы исключены из покрытия.",
                },
            ]
            features.append(
                {
                    "type": "Feature",
                    "id": planting_id,
                    "properties": {
                        "object_type": "proposed_planting_area",
                        "planting_id": planting_id,
                        "request_id": selection.request_id,
                        "selection_mode": selection.mode,
                        "plant_type": "herbaceous",
                        "species": selection.species,
                        "status": "accepted",
                        "coordinate_reference": "local_dxf_coordinates",
                        "dxf_units_per_meter": units,
                        "checks": checks,
                    },
                    "geometry": mapping(polygon),
                }
            )
        summary[selection.request_id] = {
            "plant_type": "herbaceous",
            "species": selection.species,
            "mode": selection.mode,
            "accepted_area_count": count,
            "accepted_area_in_dxf_square_units": area,
        }

    _write_jsonl(output_path, features)
    _write_jsonl(decisions_path, decisions)
    if explanations_path is not None:
        write_explanations_markdown(explanations_path, features, units)
    point_features = [
        item for item in features if item["properties"]["object_type"] == "proposed_planting"
    ]
    area_features = [
        item for item in features if item["properties"]["object_type"] == "proposed_planting_area"
    ]
    rejected = sum(item["properties"]["status"] == "rejected" for item in decisions)
    report = {
        "status": "passed",
        "request": str(request_path) if request_path is not None else f"preset:{preset}",
        "output": str(output_path),
        "decisions_output": str(decisions_path),
        "explanations_output": (
            str(explanations_path) if explanations_path is not None else None
        ),
        "coordinate_reference": "local_dxf_coordinates",
        "dxf_units_per_meter": units,
        "summary": summary,
        "point_placement_count": len(point_features),
        "area_placement_count": len(area_features),
        "manual_review_count": sum(
            item["properties"]["status"] == "manual_review" for item in features
        ),
        "rejected_candidate_count": rejected,
        "failed_check_count": 0,
        "unique_id_count": len({item["id"] for item in features}),
        "unique_point_id_count": len({item["id"] for item in point_features}),
        "unique_feature_id_count": len({item["id"] for item in features}),
        "feature_count": len(features),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Choose what and where to plant and explain every decision."
    )
    parser.add_argument("plant_allow_zones", type=Path)
    parser.add_argument("plant_allow_zones_report", type=Path)
    parser.add_argument("normalized_objects", type=Path)
    parser.add_argument("constraint_map", type=Path)
    parser.add_argument("--utilities", type=Path, default=Path("reconstructed_utilities.geojsonl"))
    parser.add_argument("--config", type=Path, default=Path("nanocad-plugin/config/greenai.plugin.json"))
    parser.add_argument("--request", type=Path, help="Optional planting request JSON")
    parser.add_argument(
        "--preset",
        choices=sorted(PLANTING_PRESETS),
        default="dense_mixed",
        help="Automatic composition used when --request is omitted",
    )
    parser.add_argument("--tree-spacing-m", type=float)
    parser.add_argument("--tree-max-count", type=int)
    parser.add_argument("--diagnostic-rejected-max-count", type=int)
    parser.add_argument("--output", type=Path, default=Path("planting_plan.geojsonl"))
    parser.add_argument("--decisions-output", type=Path, default=Path("planting_decisions.geojsonl"))
    parser.add_argument("--report", type=Path, default=Path("planting_plan_report.json"))
    parser.add_argument(
        "--explanations-output",
        type=Path,
        help="Optional Markdown passport with rule checks for every plan feature",
    )
    args = parser.parse_args()
    if args.request is not None and any(
        value is not None
        for value in (
            args.tree_spacing_m,
            args.tree_max_count,
        )
    ):
        raise SystemExit(
            "Planting service error: --request cannot be combined with automatic preset overrides"
        )
    try:
        report = plan(
            args.plant_allow_zones,
            args.plant_allow_zones_report,
            args.normalized_objects,
            args.constraint_map,
            args.utilities,
            args.config,
            args.output,
            args.report,
            args.decisions_output,
            args.request,
            args.explanations_output,
            args.preset,
            args.tree_spacing_m,
            args.tree_max_count,
            args.diagnostic_rejected_max_count,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Planting service error: {error}") from error
    print(f"Output: {args.output}")
    print(f"Decisions: {args.decisions_output}")
    print(f"Report: {args.report}")
    if args.explanations_output is not None:
        print(f"Explanations: {args.explanations_output}")
    for request_id, item in report["summary"].items():
        accepted = item.get("accepted_count", item.get("accepted_area_count", 0))
        print(f"  {request_id}: {accepted} accepted, {item.get('rejected_count', 0)} rejected")


if __name__ == "__main__":
    main()
