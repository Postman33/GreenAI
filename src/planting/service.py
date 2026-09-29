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
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from shapely import STRtree
from shapely.geometry import GeometryCollection, Point, mapping, shape
from shapely.validation import make_valid

from ..domain.models import PlantingProfile, PlantingSelection
from .composition_review import load_shrub_survey, review_shrub_composition, review_tree_composition
from .design import STYLE_CONTRACTS, alley_layout, choose_shrub_composition, choose_tree_composition, free_group_layout, hedge_coverage, flowerbed_patches
from .layout_optimizer import optimize_layouts
from .spatial import PlantingPointIndex
from .placement_generator import (
    best_component_layout,
    best_linear_layout,
    build_checks,
    configured_existing_tree_clearance_m,
    grid_candidates,
    load_geojsonl_by_object_type,
    load_profiles,
    load_zones,
    polygon_parts,
    prepare_sidewalk_for_checks,
    required_spacing,
    mixed_canopy_pair,
    physical_planting_area,
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
    "alley": ("tree", "herbaceous"),
    "hedge": ("shrub", "herbaceous"),
    "shrub_mass": ("shrub", "herbaceous"),
    "free_group": ("tree", "herbaceous"),
    "mixed_flowerbed": ("herbaceous",),
}


def _design_options(item: dict[str, Any], plant_type: str, mode: str, label: str) -> dict[str, Any]:
    style = str(item.get("design_style", "auto"))
    if style != "auto" and STYLE_CONTRACTS.get(style) != (plant_type, mode):
        raise ValueError(f"{label}: incompatible design_style {style!r} for {plant_type}/{mode}")
    guide = None
    if "guide" in item:
        if style not in {"alley", "hedge"}:
            raise ValueError(f"{label}: guide is supported only for alley and hedge")
        data = item["guide"]
        if not isinstance(data, dict) or data.get("type") != "LineString":
            raise ValueError(f"{label}: guide must be a LineString")
        coords = _request_points(data.get("coordinates"), f"{label}.guide")
        if len(coords) < 2:
            raise ValueError(f"{label}: guide requires at least two coordinates")
        guide = shape({"type": "LineString", "coordinates": [(p.x, p.y) for p in coords]})
        if guide.length <= 0 or not guide.is_simple or guide.is_ring:
            raise ValueError(f"{label}: guide must be an open, non-self-intersecting line")
    width = float(item.get("band_width_m", 1.5))
    if not math.isfinite(width) or width <= 0:
        raise ValueError(f"{label}: band_width_m must be finite and positive")
    rows = item.get("row_count", 1)
    if type(rows) is not int or rows not in {1, 2}:
        raise ValueError(f"{label}: row_count must be 1 or 2")
    seed = item.get("seed", 0)
    if type(seed) is not int:
        raise ValueError(f"{label}: seed must be an integer")
    composition = item.get("composition", [])
    if not isinstance(composition, list):
        raise ValueError(f"{label}: composition must be an array")
    if style != "mixed_flowerbed" and composition:
        raise ValueError(f"{label}: composition requires mixed_flowerbed")
    if style == "mixed_flowerbed":
        if not isinstance(composition, list) or len(composition) < 2:
            raise ValueError(f"{label}: mixed_flowerbed requires at least two composition entries")
        names = set()
        parts = []
        for part in composition:
            if not isinstance(part, dict) or not str(part.get("species", "")).strip():
                raise ValueError(f"{label}: each composition entry requires species")
            name = str(part["species"]).strip()
            if name in names:
                raise ValueError(f"{label}: duplicate composition species {name!r}")
            names.add(name)
            share = float(part.get("share", 0))
            if not math.isfinite(share) or not 0 < share < 1:
                raise ValueError(f"{label}: composition share must be between 0 and 1")
            density = part.get("plants_per_m2")
            if density is not None:
                density = float(density)
                if not math.isfinite(density) or density <= 0:
                    raise ValueError(f"{label}: plants_per_m2 must be finite and positive")
            parts.append({"species": name, "share": share, "plants_per_m2": density})
        if not math.isclose(sum(part["share"] for part in parts), 1.0, abs_tol=1e-8):
            raise ValueError(f"{label}: composition shares must sum to 1")
        composition = parts
    return dict(design_style=style, guide=guide, band_width_m=width, row_count=rows, seed=seed, composition=tuple(composition))


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
            style = preset if preset in STYLE_CONTRACTS and STYLE_CONTRACTS[preset][0] == plant_type else "auto"
            if style != "auto":
                mode = STYLE_CONTRACTS[style][1]
            design_data = {**config.get("plantingDesignDefaults", {}).get(style, {}), "design_style": style}
            design_options = _design_options(design_data, plant_type, mode, f"auto_{plant_type}")
            species = (
                design_options["composition"][0]["species"]
                if style == "mixed_flowerbed" else str(item["species"])
            )
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
                    species=species,
                    mode=mode,
                    area=None,
                    points=(),
                    spacing_m=spacing,
                    max_count=tree_max_count if plant_type == "tree" else None,
                    selection_reasons=("Состав цветника задан в настройках; условия выращивания проверяются проектировщиком.",)
                    if style == "mixed_flowerbed" else tuple(str(v) for v in item.get("selectionReasons", [])),
                    catalog_reference=None if style == "mixed_flowerbed" else str(item.get("catalogReference", "plant catalog")),
                    **design_options,
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
        style = str(item.get("design_style", "auto"))
        default_mode = STYLE_CONTRACTS.get(style, (None, None))[1]
        species = str(item.get("species") or "").strip()
        if not species:
            raise ValueError(f"{request_id}: species is required")
        mode = str(item.get("mode") or default_mode or ("cover_area" if plant_type == "herbaceous" else "fill_area"))
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
        design_options = _design_options(item, plant_type, mode, request_id)
        if design_options["composition"] and species not in {part["species"] for part in design_options["composition"]}:
            raise ValueError(f"{request_id}: species must be one of the composition species")
        if mode in AREA_MODES and maximum is not None:
            raise ValueError(f"{request_id}: max_count applies only to point plantings")
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
                **design_options,
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
    if selection.design_style == "mixed_flowerbed":
        reasons = selection.selection_reasons or ("Вид входит в заданную композицию цветника.",)
    if selection.mode in AREA_MODES:
        spacing = selection.spacing_m
        if spacing is None:
            spacing = (catalog_item or {}).get("recommended_spacing_m") or (catalog_item or {}).get("min_spacing_m") or (configured or {}).get("spacingM", 0.0)
        minimum = float((catalog_item or {}).get("min_spacing_m") or 0.0)
        if not math.isfinite(float(spacing)) or spacing < minimum or spacing < 0:
            raise ValueError(f"{selection.request_id}: area spacing must be finite and at least {minimum:g} m")
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
            "spacingM": spacing,
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
        # Do not transfer species-specific permission to a different catalog plant.
        understory_trunk_clearance_m=(base.understory_trunk_clearance_m
                                     if selection.species == base.species else None),
        allow_under_tree_canopy=(base.allow_under_tree_canopy
                                if selection.species == base.species else False),
        existing_tree_clearance_m=(base.existing_tree_clearance_m
                                   if selection.species == base.species else None),
    )


def _scope_check(point: Point, scope: Any, profile: PlantingProfile, units: float) -> dict[str, Any]:
    passed = not scope.is_empty and scope.covers(point)
    physical_boundary = profile.footprint_boundary == "physical_area"
    clearance = 0.0 if physical_boundary else profile.footprint_radius_m
    return {
        "code": "ALLOWED_ZONE",
        "target": "plant allow zone",
        "status": "passed" if passed else "failed",
        "actual_distance_m": None,
        "required_distance_m": clearance,
        "norm_reference": "Расчётное пересечение всех применимых ограничений",
        "explanation": (
            "Центр проходит отступы от объектов; крона помещается на территории озеленения, "
            "сохранены интервалы до существующей растительности."
            if passed and physical_boundary else
            f"Центр находится в безопасной области; полный габарит радиусом {clearance:g} м "
            "остаётся внутри выбранной допустимой зоны."
            if passed
            else "Точка вне итоговой допустимой области; конкретные причины указаны в проверках ниже."
        ),
    }


def _spacing_check(
    point: Point,
    profile: PlantingProfile,
    profiles: dict[str, PlantingProfile],
    occupied: list[tuple[str, float, float]],
    units: float,
    occupied_index: PlantingPointIndex | None = None,
) -> dict[str, Any]:
    nearest_actual: float | None = None
    nearest_required: float | None = None
    nearest_type: str | None = None
    worst_violation: tuple[float, float, str] | None = None
    mixed_checks: list[dict[str, Any]] = []
    passed = True
    neighbours = (
        occupied_index.nearest_by_profile(point)
        if occupied_index is not None
        else ((key, point.distance(Point(x, y))) for key, x, y in occupied)
    )
    for other_type, distance in neighbours:
        other = profiles.get(other_type)
        if other is None:
            continue
        actual = distance / units
        required = required_spacing(profile, other)
        if mixed_canopy_pair(profile, other):
            mixed_checks.append({"profile": other_type, "actual_distance_m": actual,
                                 "required_distance_m": required})
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
        "tree_shrub_spacing": mixed_checks,
        "explanation": (
            "Других предложенных посадок ближе установленного шага нет."
            if passed
            else f"Нарушающая шаг посадка находится на расстоянии {nearest_actual:.3f} м; "
            f"требуется не менее {nearest_required:g} м."
        ) + (" Смешанная посадка: проекции крон дерева и кустарника могут пересекаться; "
             "проверяются свободная область у ствола и проектный интервал между центрами. "
             f"Проверено пар: {len(mixed_checks)}; минимальное требование "
             f"{min(item['required_distance_m'] for item in mixed_checks):g} м. "
             "Это параметр композиции из профилей растений."
             if mixed_checks else ""),
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
    geometry_cache: dict[str, Any | None] | None = None,
    occupied_index: PlantingPointIndex | None = None,
    selected_zone: Any | None = None,
    physical_area: Any | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    checks = build_checks(
        point,
        profile,
        plant_report,
        constraints,
        normalized,
        utilities,
        units,
        geometry_cache,
    )
    checks[0] = _scope_check(point, scope, profile, units)
    if selected_zone is not None:
        checks.extend(_scope_diagnostic_checks(
            point, selected_zone, profile, normalized,
            profile.existing_tree_clearance_m, units, physical_area,
        ))
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
    checks.append(_spacing_check(point, profile, profiles, occupied, units, occupied_index))
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
    layout_style: str,
    layout_trace_id: str | None = None,
    species_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": planting_id,
        "properties": {
            "object_type": "proposed_planting",
            "planting_id": planting_id,
            "request_id": selection.request_id,
            "selection_mode": selection.mode,
            "layout_style": layout_style,
            "layout_trace_id": layout_trace_id,
            "species_selection": species_selection,
            "design_style": selection.design_style,
            "plant_type": profile.plant_type,
            "species": profile.species,
            "status": status,
            "spacing_m": profile.spacing_m,
            "avoid_other_plantings_m": profile.avoid_other_plantings_m,
            "footprint_radius_m": profile.footprint_radius_m,
            "symbol_radius_m": profile.symbol_radius_m,
            "understory_trunk_clearance_m": profile.understory_trunk_clearance_m,
            "allow_under_tree_canopy": profile.allow_under_tree_canopy,
            "existing_tree_clearance_m": profile.existing_tree_clearance_m,
            "footprint_boundary": profile.footprint_boundary,
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
    layout_trace_id: str | None = None,
    species_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failed = [str(item["code"]) for item in checks if item["status"] == "failed"]
    manual = [str(item["code"]) for item in checks if item["status"] == "manual_review"]
    reason_checks = [item for item in checks if item.get("status") == "failed"]
    if any(item.get("code") != "ALLOWED_ZONE" for item in reason_checks):
        reason_checks = [item for item in reason_checks if item.get("code") != "ALLOWED_ZONE"]
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
            "candidate_kind": "diagnostic_sample" if diagnostic else "generated_or_requested_point",
            "layout_trace_id": layout_trace_id,
            "species_selection": species_selection,
            "failed_checks": failed,
            "manual_review_checks": manual,
            "rejection_reasons": [
                str(item.get("explanation") or item.get("code"))
                for item in reason_checks
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
    physical_area: Any | None = None,
) -> list[dict[str, Any]]:
    """Explain which physical operation removed a point from ``safe_scope``."""
    checks: list[dict[str, Any]] = []
    footprint = profile.footprint_radius_m
    physical_boundary = profile.footprint_boundary == "physical_area"
    footprint_area = physical_area if physical_boundary else selected_zone
    if footprint_area is None:
        raise ValueError("Physical planting area missing from scope checks")
    if physical_boundary:
        checks.append({
            "code": "PLANT_CENTER_INSIDE_ZONE", "target": "plant allow-zone centre",
            "status": "passed" if selected_zone.covers(point) else "failed",
            "actual_distance_m": None, "required_distance_m": None,
            "norm_reference": "Расчётное пересечение применённых правил отступа",
            "explanation": "Центр находится в допустимой зоне отступов." if selected_zone.covers(point)
                           else "Центр находится вне допустимой зоны отступов.",
        })
    boundary_distance = (
        point.distance(footprint_area.boundary) / units
        if not footprint_area.is_empty
        else 0.0
    )
    footprint_passed = footprint_area.covers(point) and boundary_distance + 1e-7 >= footprint
    boundary_name = "физической территории озеленения" if physical_boundary else "допустимой зоны"
    checks.append(
        {
            "code": "PLANT_FOOTPRINT_INSIDE_SITE" if physical_boundary else "PLANT_FOOTPRINT_INSIDE_ZONE",
            "target": "physical plantable-area boundary" if physical_boundary else "plant allow-zone boundary",
            "status": "passed" if footprint_passed else "failed",
            "actual_distance_m": boundary_distance,
            "required_distance_m": footprint,
            "norm_reference": "project parameter: footprintRadiusM",
            "explanation": (
                f"Полный габарит посадки остаётся внутри {boundary_name}."
                if footprint_passed
                else (
                    f"До границы {boundary_name} {boundary_distance:.3f} м, "
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

    Every disconnected component is sampled before any component receives a
    second grid point. Row continuations then make gaps beside an accepted row
    easy to understand in CAD. This prevents large polygons from exhausting
    the diagnostic limit before small territories are inspected.
    """
    if maximum <= 0 or selected_zone.is_empty or spacing_dxf <= 0:
        return []
    result: list[Point] = []
    seen: set[tuple[int, int]] = set()
    tolerance = max(spacing_dxf * 0.02, 1e-5)
    accepted_tree = STRtree(accepted_points) if accepted_points else None

    def add(point: Point) -> bool:
        if len(result) >= maximum or not selected_zone.covers(point):
            return False
        if accepted_tree is not None and len(
            accepted_tree.query(point, predicate="dwithin", distance=tolerance)
        ):
            return False
        key = (round(point.x / tolerance), round(point.y / tolerance))
        if key not in seen:
            seen.add(key)
            result.append(point)
            return True
        return False

    components = sorted(
        polygon_parts(selected_zone),
        key=lambda item: (item.bounds[0], item.bounds[1], item.bounds[2], item.bounds[3]),
    )

    # Guarantee an initial inspection point for every territory whenever the
    # configured limit permits it. representative_point() is inside even a
    # narrow or concave polygon, where a spacing grid can yield no points.
    for component in components:
        add(component.representative_point())
        if len(result) >= maximum:
            return result

    # Extend locally visible rows in both directions.  This produces the
    # intuitive "why is there no third tree here?" candidates.
    for index, first in enumerate(accepted_points):
        assert accepted_tree is not None
        neighbours = sorted(
            (
                (actual, int(other_index))
                for other_index in accepted_tree.query(
                    first, predicate="dwithin", distance=1.25 * spacing_dxf
                )
                if other_index > index
                for actual in [first.distance(accepted_points[int(other_index)])]
                if 0.75 * spacing_dxf <= actual <= 1.25 * spacing_dxf
            ),
        )
        if not neighbours:
            continue
        distance, second_index = neighbours[0]
        second = accepted_points[second_index]
        dx = (second.x - first.x) / distance * spacing_dxf
        dy = (second.y - first.y) / distance * spacing_dxf
        add(Point(first.x - dx, first.y - dy))
        add(Point(second.x + dx, second.y + dy))

    # Add the regular diagnostic grid round-robin: one new point per component
    # in each pass. These are alternatives, not additional proposals.
    active = [
        iter(grid_candidates(component, spacing_dxf, 0.0, 0.5, 0.5))
        for component in components
    ]
    while active and len(result) < maximum:
        next_active = []
        for iterator in active:
            added = False
            for x, y in iterator:
                if add(Point(x, y)):
                    added = True
                    break
            if added:
                next_active.append(iterator)
            if len(result) >= maximum:
                return result
        active = next_active
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


def _species_selection(selection: PlantingSelection, profile: PlantingProfile | dict[str, Any],
                       request_path: Path | None, species: str | None = None) -> dict[str, Any]:
    """Record provenance without implying that the planner ranked species."""
    reasons = (profile.selection_reasons if isinstance(profile, PlantingProfile)
               else profile.get("selectionReasons", []))
    reference = (profile.catalog_reference if isinstance(profile, PlantingProfile)
                 else profile.get("catalogReference"))
    return {
        "species": species or selection.species,
        "source": "explicit_request" if request_path is not None else "preset_profile",
        "method": "configured_species_not_ranked",
        "selection_reasons": list(reasons),
        "catalog_reference": reference,
    }


def write_explanations_markdown(
    path: Path, features: list[dict[str, Any]], units_per_meter: float,
    composition_advisories: list[dict[str, Any]] | None = None,
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
        species_choice = properties.get("species_selection") or {}
        if species_choice:
            lines.append(
                f"- Источник вида: `{_markdown_cell(species_choice.get('source'))}`; "
                "вид задан запросом или профилем, автоматического ранжирования видов нет."
            )
            if species_choice.get("selection_reasons"):
                lines.append("- Основание выбора вида: " + _markdown_cell(
                    "; ".join(species_choice["selection_reasons"])))
        if properties.get("layout_trace_id"):
            lines.append(f"- Схема размещения: `{_markdown_cell(properties['layout_trace_id'])}` "
                         "(варианты и отсеянные точки — в planting_layout_trace.jsonl).")
        elif properties.get("layout_style"):
            lines.append(f"- Способ размещения: `{_markdown_cell(properties['layout_style'])}`.")
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
    if composition_advisories:
        lines.extend(["## Композиция и существующие растения", "",
                      "Это рекомендации для проверки проектировщиком. Существующие растения "
                      "не удаляются из чертежа автоматически.", ""])
        for advisory in composition_advisories:
            x, y = advisory["coordinates"]
            if advisory.get("plant_type") == "tree":
                lines.append(
                    f"- `{_markdown_cell(advisory['existing_tree_id'])}` (X={x:.3f}; Y={y:.3f}): "
                    f"{_markdown_cell(advisory['reason'])}"
                )
            else:
                lines.append(
                    f"- `{_markdown_cell(advisory['existing_shrub_id'])}` (X={x:.3f}; Y={y:.3f}), "
                    f"посадка `{_markdown_cell(advisory['planting_id'])}`: "
                    + ("предложено убрать одиночный куст другого вида из композиции; "
                       "проверить на месте и выбрать пересадку либо удаление."
                       if advisory["recommendation"] == "propose_removal_from_composition"
                       else "определить вид куста до решения о пересадке.")
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
    layout_trace_path: Path | None = None,
    existing_shrub_survey_path: Path | None = None,
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
    surveyed_shrubs, surveyed_masses = (
        load_shrub_survey(existing_shrub_survey_path)
        if existing_shrub_survey_path is not None else ([], GeometryCollection())
    )
    prepare_sidewalk_for_checks(normalized, constraints, zone_report, units)
    diagnostic_rejected_max = int(
        diagnostic_rejected_max_count
        if diagnostic_rejected_max_count is not None
        else config.get("diagnosticRejectedMaxCount", 1000)
    )
    if diagnostic_rejected_max < 0:
        raise ValueError("diagnosticRejectedMaxCount must be non-negative")
    tree_layout_mode = str(config.get("treeLayoutMode", "cp_sat"))
    if tree_layout_mode not in {"cp_sat", "composition", "linear_preferred", "area_fill"}:
        raise ValueError("treeLayoutMode must be cp_sat, composition, linear_preferred or area_fill")

    resolved = {
        selection.request_id: resolve_profile(selection, base_profiles, config, zone_report)
        for selection in selections
    }
    flower_profiles = {
        (selection.request_id, part["species"]): resolve_profile(
            replace(selection, species=part["species"], catalog_reference=None), base_profiles, config, zone_report
        )
        for selection in selections for part in selection.composition
    }
    point_profiles = {
        plant_type: replace(profile, existing_tree_clearance_m=configured_existing_tree_clearance_m(config, profile))
        for plant_type, profile in resolved.items()
        if isinstance(profile, PlantingProfile)
    }
    physical_ground = (physical_planting_area(constraints)
                       if any(p.footprint_boundary == "physical_area" for p in point_profiles.values()) else None)
    layout_profiles = {
        plant_type: replace(
            profile,
            spacing_m=profile.spacing_m * units,
            footprint_radius_m=profile.footprint_radius_m * units,
            avoid_other_plantings_m=profile.avoid_other_plantings_m * units,
            understory_trunk_clearance_m=(profile.understory_trunk_clearance_m * units
                                         if profile.understory_trunk_clearance_m is not None else None),
        )
        for plant_type, profile in point_profiles.items()
    }
    features: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    layout_traces: list[dict[str, Any]] = []
    optimizer_runs: dict[str, dict[str, Any]] = {}
    if layout_trace_path is None:
        layout_trace_path = output_path.with_name("planting_layout_trace.jsonl")
    occupied: list[tuple[str, float, float]] = []
    occupied_index = PlantingPointIndex()
    tree_composition_advisories: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    diagnostic_counters: Counter[str] = Counter()
    summary: dict[str, Any] = {}
    rule_geometry_cache: dict[str, Any | None] = {}
    prefixes = {"tree": "T", "shrub": "S", "herbaceous": "H"}

    ordered = sorted(
        (item for item in selections if item.mode in POINT_MODES),
        key=lambda item: -point_profiles[item.request_id].footprint_radius_m,
    )
    for selection in ordered:
        if selection.plant_type not in zones:
            raise ValueError(f"No allow zone calculated for {selection.plant_type}")
        profile = point_profiles[selection.request_id]
        species_choice = _species_selection(selection, profile, request_path)
        layout_profile = layout_profiles[selection.request_id]
        # Occupied points retain the selection identity (species/spacing).
        current_layout_profiles = layout_profiles
        existing_tree_clearance = configured_existing_tree_clearance_m(config, profile)
        source_zone = zones[selection.plant_type]
        selected_zone = source_zone["geometry"]
        selected_physical = physical_ground
        if selection.area is not None:
            selected_zone = selected_zone.intersection(selection.area)
            if selected_physical is not None:
                selected_physical = selected_physical.intersection(selection.area)
        scope = safe_scope(
            selected_zone,
            profile,
            normalized.get("existing_tree"),
            normalized.get("existing_tree_belt"),
            existing_tree_clearance,
            units,
            selected_physical,
        )
        if not surveyed_masses.is_empty:
            # Retain mapped existing shrub masses instead of proposing new
            # plants or full crowns on top of them.
            scope = scope.difference(
                surveyed_masses.buffer(profile.footprint_radius_m * units)
            )
        candidates: list[tuple[Point, str, str | None]]
        if selection.mode == "points":
            candidates = [(point, "manual", None) for point in selection.points]
        elif selection.design_style in {"alley", "free_group"}:
            if selection.design_style == "alley":
                reference = selection.area if selection.area is not None else selected_zone
                designed = alley_layout(
                    scope, reference, selection.guide, layout_profile,
                    current_layout_profiles, occupied, profile.max_count, selection.row_count,
                    profile_key=selection.request_id,
                )
            else:
                designed = free_group_layout(
                    scope, layout_profile, current_layout_profiles, occupied,
                    profile.max_count, selection.seed,
                    profile_key=selection.request_id,
                )
            candidates = [(Point(x, y), selection.design_style, None) for x, y in designed]
        else:
            generated: list[tuple[float, float, str, str]] = []
            use_linear = selection.plant_type == "tree" and tree_layout_mode in {
                "cp_sat", "composition", "linear_preferred",
            }
            use_shrub_beds = selection.plant_type == "shrub" and selection.design_style == "auto"
            use_composition = selection.plant_type == "tree" and tree_layout_mode in {"cp_sat", "composition"}
            use_optimizer = (selection.plant_type == "tree" and tree_layout_mode == "cp_sat") or use_shrub_beds
            layout_options: list[dict[str, Any]] = []
            layout_option_traces: dict[str, dict[str, Any]] = {}
            parents = (
                sorted(polygon_parts(selected_zone), key=lambda item: item.area, reverse=True)
                if use_linear or use_shrub_beds else [scope]
            )
            for parent in parents:
                remaining = profile.max_count - len(generated)
                if remaining <= 0:
                    break
                parent_scope = scope.intersection(parent) if use_linear or use_shrub_beds else scope
                if parent_scope.is_empty:
                    continue
                inset = parent_scope.buffer(-max(0.02 * units, 1e-5))
                if inset.is_empty:
                    continue
                occupied_now = occupied + [
                    (selection.request_id, x, y) for x, y, _style, _trace_id in generated
                ]
                if use_composition:
                    design_trace: dict[str, Any] = {}
                    designed, style = choose_tree_composition(
                        inset, parent, layout_profile, current_layout_profiles,
                        occupied_now, remaining, normalized.get("existing_tree"),
                        profile_key=selection.request_id,
                        shade_target=constraints.get("sidewalk_area"), trace=design_trace,
                        include_alternatives=use_optimizer,
                    )
                    trace_id = f"{selection.request_id}:layout_{len(layout_traces) + 1:04d}"
                    design_trace.update({"trace_id": trace_id, "request_id": selection.request_id,
                                         "plant_type": selection.plant_type,
                                         "spacing_m": profile.spacing_m,
                                         "max_count_for_component": remaining})
                    layout_traces.append(design_trace)
                    if use_optimizer:
                        layout_option_traces[trace_id] = design_trace
                        for variant in design_trace.get("variants", []):
                            layout_options.append({
                                "bed": len(layout_option_traces) - 1,
                                "points": [tuple(pair) for pair in variant["coordinates"]],
                                "score": variant["score"], "style": variant["style"],
                                "trace_id": trace_id, "variant": variant,
                            })
                    else:
                        generated.extend((x, y, style, trace_id) for x, y in designed)
                    continue
                if use_shrub_beds:
                    bed_trace: dict[str, Any] = {}
                    bed_points, bed_style = choose_shrub_composition(
                        inset, parent, layout_profile, current_layout_profiles,
                        occupied_now, remaining, trace=bed_trace,
                    )
                    trace_id = f"{selection.request_id}:layout_{len(layout_traces) + 1:04d}"
                    bed_trace.update({"trace_id": trace_id, "request_id": selection.request_id,
                                      "plant_type": selection.plant_type,
                                      "spacing_m": profile.spacing_m,
                                      "max_count_for_component": remaining})
                    layout_traces.append(bed_trace)
                    if use_optimizer:
                        layout_option_traces[trace_id] = bed_trace
                        for variant in bed_trace.get("variants", []):
                            layout_options.append({
                                "bed": len(layout_option_traces) - 1,
                                "points": [tuple(pair) for pair in variant["coordinates"]],
                                "score": variant["score"], "style": variant["style"],
                                "trace_id": trace_id, "variant": variant,
                            })
                    else:
                        generated.extend((x, y, bed_style, trace_id) for x, y in bed_points)
                    continue
                linear_trace: dict[str, Any] = {}
                linear = (
                    best_linear_layout(
                        inset, parent, layout_profile, current_layout_profiles,
                        occupied_now, remaining, trace=linear_trace,
                    ) if use_linear else None
                )
                if linear:
                    trace_id = f"{selection.request_id}:layout_{len(layout_traces) + 1:04d}"
                    linear_trace.update({"trace_id": trace_id, "request_id": selection.request_id,
                                         "plant_type": selection.plant_type,
                                         "spacing_m": profile.spacing_m,
                                         "max_count_for_component": remaining})
                    layout_traces.append(linear_trace)
                    generated.extend((x, y, "linear", trace_id) for x, y in linear)
                    continue
                for component in sorted(polygon_parts(inset), key=lambda item: item.area, reverse=True):
                    remaining = profile.max_count - len(generated)
                    if remaining <= 0:
                        break
                    area_trace: dict[str, Any] = {}
                    area_points = best_component_layout(
                        component, layout_profile, current_layout_profiles,
                        occupied + [(selection.request_id, x, y) for x, y, _style, _trace_id in generated],
                        remaining, trace=area_trace,
                    )
                    trace_id = f"{selection.request_id}:layout_{len(layout_traces) + 1:04d}"
                    area_trace.update({"trace_id": trace_id, "request_id": selection.request_id,
                                       "plant_type": selection.plant_type,
                                       "spacing_m": profile.spacing_m,
                                       "max_count_for_component": remaining})
                    layout_traces.append(area_trace)
                    generated.extend((x, y, area_trace["method"], trace_id) for x, y in area_points)
            if use_optimizer:
                chosen, optimization = optimize_layouts(
                    layout_options, layout_profile, profile.max_count,
                    time_limit_s=10.0 if selection.plant_type == "tree" else 20.0,
                )
                optimizer_runs[selection.request_id] = {
                    key: value for key, value in optimization.items()
                    if key != "selected_option_indices"
                }
                optimizer_runs[selection.request_id]["no_candidate_bed_count"] = (
                    len(layout_option_traces) - optimization["bed_count"]
                )
                optimizer_runs[selection.request_id]["total_bed_count"] = len(layout_option_traces)
                chosen_by_trace = {layout_options[i]["trace_id"]: layout_options[i] for i in chosen}
                for trace_id, design_trace in layout_option_traces.items():
                    option = chosen_by_trace.get(trace_id)
                    design_trace["optimizer"] = optimization
                    design_trace["optimizer_disposition"] = (
                        "selected" if option is not None else
                        "not_selected" if design_trace.get("variants") else "no_candidate"
                    )
                    design_trace["candidate_generator_grid_rejections"] = (
                        design_trace.pop("winning_grid_rejections", [])
                    )
                    design_trace["winning_grid_rejections"] = []
                    design_trace["audit_scope"] = (
                        "enumerated whole-bed schemes; generator grid rejections refer to "
                        "candidate generation, not the CP-SAT-selected scheme"
                    )
                    design_trace["winner"] = (
                        {key: value for key, value in option["variant"].items() if key != "coordinates"}
                        if option is not None else None
                    )
                for i in chosen:
                    option = layout_options[i]
                    generated.extend((x, y, option["style"], option["trace_id"])
                                     for x, y in option["points"])
            candidates = [(Point(x, y), style, trace_id) for x, y, style, trace_id in generated]

        if selection.mode == "points" or selection.design_style in {"alley", "free_group"}:
            trace_id = f"{selection.request_id}:layout_{len(layout_traces) + 1:04d}"
            layout_traces.append({
                "trace_id": trace_id,
                "request_id": selection.request_id,
                "plant_type": selection.plant_type,
                "method": "explicit_points" if selection.mode == "points" else selection.design_style,
                "source": "explicit_request" if request_path is not None else "preset_profile",
                "generated_count": len(candidates),
                "spacing_m": profile.spacing_m,
                "row_count": selection.row_count if selection.design_style == "alley" else None,
                "seed": selection.seed if selection.design_style == "free_group" else None,
                "audit_scope": "provided points" if selection.mode == "points"
                               else "generated points inside safe planting scope",
                "variants": [],
                "winner": None,
                "winning_grid_rejections": [],
            })
            candidates = [(point, style, trace_id) for point, style, _old_id in candidates]

        accepted_count = 0
        rejected_count = 0
        accepted_selection_points: list[Point] = []
        plant_report = zone_report.get("plant_types", {}).get(selection.plant_type, {})
        layout_style_counts: Counter[str] = Counter()
        for point, layout_style, trace_id in candidates:
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
                rule_geometry_cache,
                occupied_index,
                selected_zone,
                selected_physical,
            )
            decisions.append(_decision_feature(candidate_id, point, selection, status, checks,
                                               layout_trace_id=trace_id,
                                               species_selection=species_choice))
            if status == "rejected":
                rejected_count += 1
                continue
            accepted_count += 1
            features.append(_point_feature(candidate_id, point, profile, selection, status, checks,
                                           units, layout_style, trace_id, species_choice))
            layout_style_counts[layout_style] += 1
            occupied.append((selection.request_id, point.x, point.y))
            occupied_index.add(selection.request_id, point)
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
                    rule_geometry_cache,
                    occupied_index,
                    selected_zone,
                    selected_physical,
                )
                if status != "rejected":
                    continue
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
                        species_selection=species_choice,
                    )
                )
                diagnostic_rejected_count += 1
                if diagnostic_rejected_count >= diagnostic_rejected_max:
                    break
        summary[selection.request_id] = {
            "plant_type": selection.plant_type,
            "species": selection.species,
            "mode": selection.mode,
            "design_style": selection.design_style,
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "diagnostic_rejected_count": diagnostic_rejected_count,
            "layout_style_counts": dict(layout_style_counts),
            "safe_scope_area_in_dxf_square_units": scope.area,
            "existing_tree_clearance_m": existing_tree_clearance,
            "footprint_boundary": profile.footprint_boundary,
        }
        if (selection.plant_type == "tree" and selection.mode == "fill_area"
                and selection.design_style == "auto"
                and tree_layout_mode in {"cp_sat", "composition"}):
            alternative_scope = safe_scope(
                selected_zone, profile, None,
                normalized.get("existing_tree_belt"), existing_tree_clearance,
                units, selected_physical,
            )
            if not surveyed_masses.is_empty:
                alternative_scope = alternative_scope.difference(
                    surveyed_masses.buffer(profile.footprint_radius_m * units)
                )
            tree_composition_advisories.extend(review_tree_composition(
                alternative_scope, scope, normalized.get("existing_tree"),
                normalized.get("work_boundary"), layout_profile,
                current_layout_profiles, accepted_selection_points,
                existing_tree_clearance * units,
            ))

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
        profile = resolved[selection.request_id]
        assert isinstance(profile, dict)
        coverage = zone["geometry"]
        if base is not None and not base.is_empty:
            coverage = coverage.intersection(base)
        if selection.area is not None:
            coverage = coverage.intersection(selection.area)
        if selection.design_style == "hedge":
            reference = selection.area if selection.area is not None else zone["geometry"]
            coverage = hedge_coverage(coverage, reference, selection.guide, selection.band_width_m * units)
        coverage = coverage.difference(area_occupied)
        belts = normalized.get("existing_tree_belt")
        # A mass shrub planting occupies the whole allowed polygon.  Existing
        # tree canopies may have an understorey; mapped vegetation belts remain
        # excluded so the proposal does not replace an existing planting mass.
        if belts is not None and not belts.is_empty:
            coverage = coverage.difference(belts)
        if not surveyed_masses.is_empty:
            coverage = coverage.difference(surveyed_masses)
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
                max(1, math.ceil((polygon.area / units ** 2) / (spacing * spacing * math.sqrt(3.0) / 2.0)))
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
                "design_style": selection.design_style,
                "band_width_m": selection.band_width_m if selection.design_style == "hedge" else None,
                "plant_type": "shrub",
                "species": selection.species,
                "species_selection": _species_selection(selection, profile, request_path),
                "layout_style": selection.design_style if selection.design_style != "auto" else "safe_zone_cover",
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
            "design_style": selection.design_style,
            "accepted_area_count": count,
            "accepted_area_in_dxf_square_units": area,
            "estimated_plant_count": estimated_plants,
        }

    for selection in sorted(
        (item for item in selections if item.plant_type == "herbaceous"),
        key=lambda item: item.design_style != "mixed_flowerbed",
    ):
        if base is None or base.is_empty:
            raise ValueError("No base_allowed_area available for herbaceous cover")
        profile = resolved[selection.request_id]
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
        if not surveyed_masses.is_empty:
            coverage = coverage.difference(surveyed_masses)
        coverage = make_valid(coverage)
        count = 0
        area = 0.0
        is_flowerbed = selection.design_style == "mixed_flowerbed"
        patches = flowerbed_patches(coverage, selection.composition) if is_flowerbed else (
            (polygon, None) for polygon in polygon_parts(coverage)
        )
        species_areas: Counter[str] = Counter()
        for polygon, mixture_part in patches:
            if not is_flowerbed and polygon.area < 0.05 * units * units:
                continue
            species = mixture_part["species"] if mixture_part else selection.species
            species_profile = flower_profiles[(selection.request_id, species)] if mixture_part else profile
            species_areas[species] += polygon.area / units ** 2
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
                    "target": species,
                    "status": "passed",
                    "actual_distance_m": None,
                    "required_distance_m": None,
                    "norm_reference": species_profile.get("catalogReference"),
                    "explanation": "; ".join(species_profile.get("selectionReasons", []))
                    or "Вид выбран из проектного каталога растений.",
                },
                {
                    "code": "EXISTING_VEGETATION",
                    "target": "existing trees and vegetation belts",
                    "status": "passed",
                    "actual_distance_m": None,
                    "required_distance_m": None,
                    "norm_reference": "Проектное требование сохранения существующей растительности",
                    "explanation": "Существующие растительные массивы исключены; травянистый покров может продолжаться под кроной дерева.",
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
                        "design_style": selection.design_style,
                        "plant_type": "herbaceous",
                        "species": species,
                        "species_selection": _species_selection(selection, species_profile,
                                                                  request_path, species),
                        "layout_style": selection.design_style if selection.design_style != "auto" else "safe_zone_cover",
                        "composition_share": mixture_part["share"] if mixture_part else None,
                        "plants_per_m2": mixture_part["plants_per_m2"] if mixture_part else None,
                        "estimated_plant_count": (
                            math.ceil(polygon.area / units ** 2 * mixture_part["plants_per_m2"])
                            if mixture_part and mixture_part["plants_per_m2"] else None
                        ),
                        "status": "accepted",
                        "coordinate_reference": "local_dxf_coordinates",
                        "dxf_units_per_meter": units,
                        "checks": checks,
                    },
                    "geometry": mapping(polygon),
                }
            )
            area_occupied = make_valid(area_occupied.union(polygon))
        summary[selection.request_id] = {
            "plant_type": "herbaceous",
            "species": selection.species,
            "mode": selection.mode,
            "design_style": selection.design_style,
            "species_area_m2": dict(species_areas),
            "composition": list(selection.composition),
            "accepted_area_count": count,
            "accepted_area_in_dxf_square_units": area,
        }

    _write_jsonl(output_path, features)
    _write_jsonl(decisions_path, decisions)
    final_status_by_trace: dict[str, Counter[str]] = {}
    for decision in decisions:
        properties = decision["properties"]
        trace_id = properties.get("layout_trace_id")
        if trace_id:
            final_status_by_trace.setdefault(trace_id, Counter())[properties["status"]] += 1
    for trace in layout_traces:
        rejected_grid = trace.get("winning_grid_rejections", [])
        trace["discarded_count_by_reason"] = dict(Counter(
            item["reason"] for item in rejected_grid))
        trace["final_status_counts"] = dict(final_status_by_trace.get(trace["trace_id"], {}))
        trace["distance_unit"] = "local_dxf_unit"
        trace["dxf_units_per_meter"] = units
    _write_jsonl(layout_trace_path, layout_traces)
    point_features = [
        item for item in features if item["properties"]["object_type"] == "proposed_planting"
    ]
    area_features = [
        item for item in features if item["properties"]["object_type"] == "proposed_planting_area"
    ]
    if existing_shrub_survey_path is not None:
        composition_advisories = review_shrub_composition(
            features, surveyed_shrubs, surveyed_masses, units,
        )
        composition_review_status = "surveyed"
    else:
        composition_advisories = []
        composition_review_status = "not_evaluated_no_confirmed_shrub_survey"
    composition_advisories.extend(tree_composition_advisories)
    if explanations_path is not None:
        write_explanations_markdown(explanations_path, features, units, composition_advisories)
    rejected = sum(item["properties"]["status"] == "rejected" for item in decisions)
    report = {
        "status": "passed",
        "normalized_input": str(normalized_path),
        "request": str(request_path) if request_path is not None else f"preset:{preset}",
        "output": str(output_path),
        "decisions_output": str(decisions_path),
        "layout_trace_output": str(layout_trace_path),
        "layout_trace_count": len(layout_traces),
        "layout_audit_scope": (
            "Only enumerated layout variants and generator grid points are audited. "
            "Diagnostic rejections sample other coordinates; arbitrary coordinates are not exhaustively tested."
        ),
        "layout_optimizer_runs": optimizer_runs,
        "species_selection_method": "configured_species_not_ranked",
        "explanations_output": (
            str(explanations_path) if explanations_path is not None else None
        ),
        "coordinate_reference": "local_dxf_coordinates",
        "dxf_units_per_meter": units,
        "summary": summary,
        "composition_review_status": composition_review_status,
        "tree_composition_review_status": (
            "evaluated" if tree_layout_mode in {"cp_sat", "composition"} else "not_evaluated"
        ),
        "existing_shrub_survey": str(existing_shrub_survey_path) if existing_shrub_survey_path else None,
        "composition_advisories": composition_advisories,
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
    parser.add_argument("--config", type=Path, default=Path("config/planting.json"))
    parser.add_argument("--request", type=Path, help="Optional planting request JSON")
    parser.add_argument("--existing-shrub-survey", type=Path,
                        help="Optional confirmed GeoJSON survey of individual shrubs and shrub masses")
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
    parser.add_argument("--layout-trace-output", type=Path,
                        help="JSONL audit of placement variants and points discarded in the selected layout")
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
            args.layout_trace_output,
            args.existing_shrub_survey,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Planting service error: {error}") from error
    print(f"Output: {args.output}")
    print(f"Decisions: {args.decisions_output}")
    print(f"Report: {args.report}")
    print(f"Layout trace: {report['layout_trace_output']}")
    if args.explanations_output is not None:
        print(f"Explanations: {args.explanations_output}")
    for request_id, item in report["summary"].items():
        accepted = item.get("accepted_count", item.get("accepted_area_count", 0))
        print(f"  {request_id}: {accepted} accepted, {item.get('rejected_count', 0)} rejected")


if __name__ == "__main__":
    main()
