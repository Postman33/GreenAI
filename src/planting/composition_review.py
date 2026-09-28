"""Conservative design advisories for surveyed existing shrubs.

An advisory never changes source vegetation or the accepted planting plan.
The survey must explicitly distinguish individual shrubs from shrub masses.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from .design import score_tree_composition, tree_grove_layout
from .placement_generator import polygon_parts


def load_shrub_survey(path: Path) -> tuple[list[dict[str, Any]], Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("Existing shrub survey must be a GeoJSON FeatureCollection")
    raw_features = data.get("features")
    if not isinstance(raw_features, list):
        raise ValueError("Existing shrub survey must contain a features array")
    individuals: list[dict[str, Any]] = []
    masses = []
    identifiers: set[str] = set()
    for index, feature in enumerate(raw_features, start=1):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"Survey feature {index} must be a GeoJSON Feature")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"Survey feature {index} must have properties")
        kind = properties.get("object_type")
        if kind not in {"existing_shrub", "existing_shrub_mass"}:
            raise ValueError(f"Survey feature {index} has unsupported object_type {kind!r}")
        geometry = shape(feature.get("geometry"))
        if geometry.is_empty or not geometry.is_valid or not all(math.isfinite(v) for v in geometry.bounds):
            raise ValueError(f"Survey feature {index} has invalid geometry")
        if kind == "existing_shrub":
            if not isinstance(geometry, Point):
                raise ValueError(f"Existing shrub {index} must be a Point")
            identifier = str(feature.get("id") or properties.get("id") or "").strip()
            if not identifier or identifier in identifiers:
                raise ValueError(f"Existing shrub {index} needs a unique id")
            identifiers.add(identifier)
            species = str(properties.get("species") or "").strip()
            individuals.append({"id": identifier, "point": geometry, "species": species})
        else:
            if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError(f"Existing shrub mass {index} must be a polygon")
            masses.append(geometry)
    return individuals, unary_union(masses)


def review_shrub_composition(
    plan_features: list[dict[str, Any]],
    individuals: list[dict[str, Any]],
    masses: Any,
    units_per_meter: float,
) -> list[dict[str, Any]]:
    """Suggest a human review only for an isolated shrub interrupting a uniform design.

    A different species alone is not enough: the shrub must occupy a planned
    planting footprint, lack neighbouring existing shrubs, and not belong to
    a mapped mass. Unknown species warrants identification rather than removal.
    """
    if not math.isfinite(units_per_meter) or units_per_meter <= 0:
        raise ValueError("units_per_meter must be finite and positive")
    designed = []
    for feature in plan_features:
        props = feature.get("properties", {})
        if props.get("plant_type") != "shrub":
            continue
        if props.get("status") not in {"accepted", "manual_review"}:
            continue
        style = props.get("design_style")
        if style not in {"hedge", "shrub_mass"}:
            continue
        if props.get("object_type") != "proposed_planting_area":
            continue
        designed.append((feature, shape(feature["geometry"])))

    advisories = []
    for shrub in individuals:
        point = shrub["point"]
        if not masses.is_empty and masses.buffer(0.5 * units_per_meter).covers(point):
            continue
        # A close neighbour may be an unmapped group; do not call it isolated.
        if any(
            other["id"] != shrub["id"]
            and point.distance(other["point"]) <= 2.0 * units_per_meter
            for other in individuals
        ):
            continue
        for feature, footprint in designed:
            if not footprint.covers(point):
                continue
            props = feature["properties"]
            planned_species = str(props.get("species") or "").strip()
            if shrub["species"] and shrub["species"].casefold() == planned_species.casefold():
                continue
            known_other_species = bool(shrub["species"] and planned_species)
            advisories.append({
                "existing_shrub_id": shrub["id"],
                "plant_type": "shrub",
                "coordinates": [point.x, point.y],
                "planting_id": str(props.get("planting_id") or feature.get("id") or ""),
                "design_style": props["design_style"],
                "existing_species": shrub["species"] or None,
                "planned_species": planned_species or None,
                "recommendation": (
                    "propose_removal_from_composition"
                    if known_other_species else "identify_existing_shrub_before_design_decision"
                ),
                "reason": (
                    "Подтверждённый одиночный куст другого вида находится внутри "
                    "проектируемого однородного массива. Проверьте композицию на месте; "
                    "предложено убрать куст из этой композиции; способ — пересадка "
                    "или удаление — определяет проектировщик после проверки на месте."
                    if known_other_species else
                    "Подтверждённый одиночный куст находится внутри проектируемого "
                    "однородного массива, но его вид неизвестен. Определите вид до решения "
                    "об изменении посадки."
                ),
                "review_required": True,
            })
            break
    return advisories


def review_tree_composition(
    alternative_scope: Any,
    retained_scope: Any,
    existing_trees: Any,
    work_boundary: Any,
    profile: Any,
    profiles: dict[str, Any],
    accepted_tree_points: list[Point],
    clearance: float,
) -> list[dict[str, Any]]:
    """Find single existing trees blocking an otherwise complete new grove.

    This evaluates a design alternative only. The accepted planting plan still
    retains every existing tree and no source entity is erased.
    """
    if (alternative_scope is None or alternative_scope.is_empty
            or existing_trees is None or existing_trees.is_empty):
        return []
    trees = list(existing_trees.geoms) if hasattr(existing_trees, "geoms") else [existing_trees]
    trees = [tree for tree in trees if isinstance(tree, Point)]
    if not trees:
        return []
    index = STRtree(trees)
    proposals: dict[int, dict[str, Any]] = {}
    for part in polygon_parts(alternative_scope):
        trace: dict[str, Any] = {}
        proposed = tree_grove_layout(
            part, profile, profiles, [], profile.max_count, trace=trace,
        )
        winner = trace.get("winner") or {}
        if trace.get("method") != "tree_grove":
            continue
        group_size = int(winner.get("group_size", 0))
        if group_size < 3:
            continue
        for offset in range(0, len(proposed), group_size):
            group = proposed[offset:offset + group_size]
            if len(group) != group_size:
                continue
            if any(
                1e-6 < planned.distance(Point(x, y)) < profile.spacing_m - 1e-7
                for planned in accepted_tree_points for x, y in group
            ):
                continue
            blockers: set[int] = set()
            blocked_stations = 0
            for x, y in group:
                point = Point(x, y)
                if not retained_scope.covers(point):
                    blocked_stations += 1
                for raw_index in index.query(point.buffer(clearance).envelope):
                    tree_index = int(raw_index)
                    if point.distance(trees[tree_index]) + 1e-7 < clearance:
                        blockers.add(tree_index)
            if len(blockers) != 1 or blocked_stations < 2:
                continue
            tree_index = next(iter(blockers))
            tree = trees[tree_index]
            if work_boundary is not None and not work_boundary.is_empty and not work_boundary.covers(tree):
                continue
            center = Point(sum(x for x, _ in group) / group_size,
                           sum(y for _, y in group) / group_size)
            nearby_planned = sum(
                point.distance(center) <= 1.5 * profile.spacing_m
                for point in accepted_tree_points
            )
            if nearby_planned >= group_size - 1:
                continue
            window = center.buffer(1.75 * profile.spacing_m)
            retained_points = [
                (point.x, point.y) for point in accepted_tree_points
                if window.covers(point)
            ]
            # A mature existing tree has an explicit preservation advantage.
            # These are transparent design weights, not an arborist assessment.
            keep_score = (float(score_tree_composition(
                retained_points, window, profile, "tree_grove", tree,
            )["score"]) + 4.0)
            replacement_score = (float(score_tree_composition(
                group, window, profile, "tree_grove",
            )["score"]) - 2.0)
            quality_gain = round(replacement_score - keep_score, 4)
            if quality_gain < 4.0:
                continue
            gain = group_size - nearby_planned
            previous = proposals.get(tree_index)
            if previous is not None and previous["design_quality_gain"] >= quality_gain:
                continue
            proposals[tree_index] = {
                "existing_tree_id": f"tree@{tree.x:.3f},{tree.y:.3f}",
                "coordinates": [tree.x, tree.y],
                "plant_type": "tree",
                "recommendation": "propose_removal_from_composition",
                "group_size": group_size,
                "blocked_group_stations": blocked_stations,
                "potential_group_gain": gain,
                "keep_tree_design_score": round(keep_score, 4),
                "remove_tree_design_score": round(replacement_score, 4),
                "design_quality_gain": quality_gain,
                "minimum_quality_gain": 4.0,
                "existing_tree_preservation_bonus": 4.0,
                "existing_tree_removal_penalty": 2.0,
                "score_scope": "local planting composition; excludes tree health and legal status",
                "alternative_group_coordinates": [[x, y] for x, y in group],
                "reason": (
                    f"Дерево блокирует {blocked_stations} из {group_size} мест группы. "
                    f"Оценка: сохранить {keep_score:.1f}, заменить {replacement_score:.1f} "
                    "(проектные баллы). Перед решением нужны инвентаризация дерева, "
                    "проверка на месте и необходимые согласования."
                ),
                "review_required": True,
                "source": "normalized_existing_tree_center",
            }
    return sorted(proposals.values(), key=lambda item: (-item["potential_group_gain"],
                                                        item["existing_tree_id"]))
