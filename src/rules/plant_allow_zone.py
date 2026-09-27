"""Build a planting zone from the active computable placement rules.

Unsupported utility types are listed in the report, but are not placement
rules and do not change the status of every proposed planting.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg
from shapely import buffer as buffer_geometries
from shapely import union_all
from shapely.geometry import mapping, shape
from shapely.ops import clip_by_rect, unary_union
from shapely.validation import make_valid

from ..geometry.constraint_builder import as_polygonal, read_object_geometry
from ..geometry.sidewalk_geometry import relevant_sidewalk_geometry


DEFAULT_DSN = "postgresql://admin:admin@localhost:5432/admin"
TARGET_HARDINESS_ZONE = 4
UTILITY_OBJECT_TYPES = {
    "water_pipe",
    "storm_drain",
    "gas_pipe",
    "heat_pipe",
    "sewer_pipe",
    "power_cable",
    "telecom_cable",
    "overhead_power_line",
}
ACCEPTED_UTILITY_GEOMETRY_SOURCES = {
    "cleaned_high_confidence_geometry",
    "reconstructed_high_confidence_geometry",
}


def load_normalized_objects(path: Path) -> dict[str, Any]:
    """Read normalized GeoJSONL and merge features by semantic object type."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid normalized GeoJSON"
                ) from error
            if feature.get("type") != "Feature":
                raise ValueError(
                    f"Line {line_number}: expected GeoJSON Feature"
                )
            object_type = feature.get("properties", {}).get("object_type")
            if not object_type:
                raise ValueError(
                    f"Line {line_number}: object_type is missing"
                )
            geometry_data = feature.get("geometry")
            if not geometry_data:
                continue
            geometry = make_valid(shape(geometry_data))
            if not geometry.is_empty:
                grouped[object_type].append(geometry)

    return {
        object_type: unary_union(geometries)
        for object_type, geometries in grouped.items()
    }


def load_utility_geometries(
    path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load accepted cleaned/reconstructed utilities and their provenance."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    metadata: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "source_kind": "cleaned",
            "source_feature_count": 0,
            "source_part_count": 0,
            "inferred_connection_count": 0,
            "manual_review_required": False,
            "statuses": set(),
            "reasons": set(),
        }
    )
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            if feature.get("type") != "Feature":
                raise ValueError(
                    f"Line {line_number}: expected cleaned utility GeoJSON Feature"
                )
            properties = feature.get("properties", {})
            if properties.get("decision") != "accepted":
                raise ValueError(
                    f"Line {line_number}: cleaned utility decision must be accepted"
                )
            object_type = properties.get("object_type")
            if not object_type:
                raise ValueError(
                    f"Line {line_number}: cleaned utility object_type is missing"
                )
            geometry_data = feature.get("geometry")
            if not geometry_data:
                continue
            geometry = make_valid(shape(geometry_data))
            if not geometry.is_empty:
                grouped[object_type].append(geometry)
                item = metadata[object_type]
                item["source_feature_count"] += 1
                source_part_count = properties.get("source_part_count", 1)
                inferred_count = properties.get("inferred_connection_count", 0)
                for field, value in (
                    ("source_part_count", source_part_count),
                    ("inferred_connection_count", inferred_count),
                ):
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValueError(
                            f"Line {line_number}: {field} must be a non-negative integer"
                        )
                    item[field] += value
                status = properties.get("status")
                reason = properties.get("reason")
                if status:
                    item["statuses"].add(str(status))
                if reason:
                    item["reasons"].add(str(reason))
                if properties.get("manual_review_required") is True:
                    item["manual_review_required"] = True
                if (
                    status == "algorithmic_reconstruction"
                    or reason == "accepted_with_reconstructed_gaps"
                    or "inferred_connection_count" in properties
                ):
                    item["source_kind"] = "reconstructed"

    geometries = {
        object_type: unary_union(geometries)
        for object_type, geometries in grouped.items()
    }
    serializable_metadata = {
        object_type: {
            **item,
            "statuses": sorted(item["statuses"]),
            "reasons": sorted(item["reasons"]),
        }
        for object_type, item in metadata.items()
    }
    return geometries, serializable_metadata


def load_cleaned_utilities(path: Path) -> dict[str, Any]:
    """Compatibility wrapper returning accepted utility geometry only."""
    geometries, _ = load_utility_geometries(path)
    return geometries


def load_rules(
    dsn: str,
    requested_plant_types: set[str] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Load placement rules with their normative source from PostgreSQL."""
    query = """
        SELECT
            rule.rule_code,
            rule.source_plant_from,
            rule.target_object_to,
            rule.conditions,
            rule.norm_reference,
            document.code,
            document.title,
            document.edition,
            document.source_url
        FROM placement_rules AS rule
        LEFT JOIN norm_documents AS document
          ON document.id = rule.norm_document_id
        ORDER BY rule.source_plant_from, rule.rule_code
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
    except psycopg.Error as error:
        raise RuntimeError(f"could not load placement rules: {error}") from error

    for row in rows:
        plant_type = row[1]
        if requested_plant_types is not None and plant_type not in requested_plant_types:
            continue
        conditions = row[3]
        if isinstance(conditions, str):
            conditions = json.loads(conditions)
        conditions = conditions or {}
        # Previously seeded manual_review rows may still exist in a database
        # that has not been reseeded. They are no longer active rules.
        if conditions.get("check") == "manual_review":
            continue
        grouped[plant_type].append(
            {
                "rule_code": row[0],
                "plant_type": plant_type,
                "target_object_type": row[2],
                "conditions": conditions,
                "norm_reference": row[4],
                "norm_document": {
                    "code": row[5],
                    "title": row[6],
                    "edition": row[7],
                    "source_url": row[8],
                },
            }
        )

    if requested_plant_types:
        missing = requested_plant_types - set(grouped)
        if missing:
            raise ValueError(
                "No placement rules found for plant types: "
                + ", ".join(sorted(missing))
            )
    if not grouped:
        raise ValueError("No placement rules found in the database")
    return dict(grouped)


def load_plants(
    dsn: str,
    requested_plant_types: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load catalog plants, including flags needed by the selector."""
    query = """
        SELECT
            id,
            name,
            plant_type,
            min_spacing_m,
            recommended_spacing_m,
            mature_crown_radius_m,
            dimension_source,
            selection_priority,
            hardiness_zone_min,
            hardiness_zone_max,
            climate_suitability,
            hardiness_source,
            is_invasive,
            is_toxic,
            is_thorny
        FROM plant_catalog
        ORDER BY
            plant_type,
            CASE climate_suitability
                WHEN 'recommended' THEN 0
                WHEN 'conditional' THEN 1
                ELSE 2
            END,
            selection_priority,
            hardiness_zone_min NULLS LAST,
            name
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
    except psycopg.Error as error:
        raise RuntimeError(f"could not load plant catalog: {error}") from error

    for row in rows:
        plant_type = row[2]
        if requested_plant_types is not None and plant_type not in requested_plant_types:
            continue
        grouped[plant_type].append(
            {
                "id": row[0],
                "name": row[1],
                "plant_type": plant_type,
                "min_spacing_m": float(row[3]) if row[3] is not None else None,
                "recommended_spacing_m": float(row[4]) if row[4] is not None else None,
                "mature_crown_radius_m": float(row[5]) if row[5] is not None else None,
                "dimension_source": row[6],
                "selection_priority": row[7],
                "hardiness_zone_min": row[8],
                "hardiness_zone_max": row[9],
                "climate_suitability": row[10],
                "hardiness_source": row[11],
                "target_hardiness_zone": TARGET_HARDINESS_ZONE,
                "is_invasive": row[12],
                "is_toxic": row[13],
                "is_thorny": row[14],
            }
        )
    return dict(grouped)


def validate_distance_rule(rule: dict[str, Any]) -> float:
    value = rule["conditions"].get("min_distance_m")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"Rule {rule['rule_code']}: min_distance_m must be a number"
        )
    distance = float(value)
    if not math.isfinite(distance) or distance < 0:
        raise ValueError(
            f"Rule {rule['rule_code']}: min_distance_m must be finite and non-negative"
        )
    return distance

def apply_rules(
    plant_type: str,
    base_allowed_area: Any,
    normalized_objects: dict[str, Any],
    rules: list[dict[str, Any]],
    dxf_units_per_meter: float,
    exclusion_cache: dict[tuple[str, float], Any],
    geometry_sources: dict[str, str],
    geometry_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Apply computable rules and return zone, rule results and warnings."""
    allowed_area = base_allowed_area
    evaluations: list[dict[str, Any]] = []
    warnings: list[str] = []

    for rule in rules:
        conditions = rule["conditions"] or {}
        check = conditions.get("check")
        target_type = rule["target_object_type"]
        target_geometry = normalized_objects.get(target_type)
        evaluation = {
            "rule_code": rule["rule_code"],
            "target_object_type": target_type,
            "check": check,
            "norm_reference": rule["norm_reference"],
            "norm_document": rule["norm_document"],
            "geometry_source": geometry_sources.get(
                target_type, "normalized_raw_geometry"
            ),
        }
        # Preserve the normative threshold even when the source geometry is
        # missing or unsuitable for an automatic distance measurement.
        if check == "min_distance":
            evaluation["min_distance_m"] = validate_distance_rule(rule)
        source_metadata = (geometry_metadata or {}).get(target_type)
        if source_metadata:
            evaluation["source_geometry_metadata"] = source_metadata

        if target_geometry is None or target_geometry.is_empty:
            evaluation.update(
                status="unavailable",
                reason=f"No {target_type} geometry was found in normalized input",
            )
            warnings.append(
                f"{plant_type}/{rule['rule_code']}: missing {target_type} geometry"
            )
            evaluations.append(evaluation)
            continue

        if check == "manual_review":
            evaluation.update(
                status="manual_review",
                reason=conditions.get("reason", "Manual review is required"),
                source_geometry_type=target_geometry.geom_type,
            )
            warnings.append(
                f"{plant_type}/{rule['rule_code']}: manual review required"
            )
            evaluations.append(evaluation)
            continue

        if (
            check == "min_distance"
            and target_type in UTILITY_OBJECT_TYPES
            and evaluation["geometry_source"]
            not in ACCEPTED_UTILITY_GEOMETRY_SOURCES
        ):
            evaluation.update(
                status="manual_review",
                reason=(
                    "Automatic utility setback requires accepted cleaned or "
                    "reconstructed utility geometry"
                ),
                source_geometry_type=target_geometry.geom_type,
            )
            warnings.append(
                f"{plant_type}/{rule['rule_code']}: accepted cleaned or reconstructed "
                f"{target_type} geometry is required"
            )
            evaluations.append(evaluation)
            continue

        if check != "min_distance":
            evaluation.update(
                status="unsupported",
                reason=f"Unsupported rule check: {check!r}",
            )
            warnings.append(
                f"{plant_type}/{rule['rule_code']}: unsupported check {check!r}"
            )
            evaluations.append(evaluation)
            continue

        distance_m = validate_distance_rule(rule)
        buffer_distance = distance_m * dxf_units_per_meter
        print(
            f"  applying {plant_type}/{rule['rule_code']} "
            f"({distance_m:g} m from {target_type})...",
            flush=True,
        )
        area_before = allowed_area.area
        cache_key = (target_type, buffer_distance)
        exclusion = exclusion_cache.get(cache_key)
        if exclusion is None:
            min_x, min_y, max_x, max_y = base_allowed_area.bounds
            relevant_geometry = clip_by_rect(
                target_geometry,
                min_x - buffer_distance,
                min_y - buffer_distance,
                max_x + buffer_distance,
                max_y + buffer_distance,
            )
            component_count = (
                len(relevant_geometry.geoms)
                if hasattr(relevant_geometry, "geoms")
                else 1
            )
            print(
                f"    relevant source: {relevant_geometry.geom_type}, "
                f"{component_count} component(s)",
                flush=True,
            )
            components = (
                list(relevant_geometry.geoms)
                if hasattr(relevant_geometry, "geoms")
                else [relevant_geometry]
            )
            buffered_components = buffer_geometries(
                components,
                buffer_distance,
                quad_segs=8,
            )
            exclusion = union_all(buffered_components)
            exclusion_cache[cache_key] = exclusion
        excluded_from_current_area = allowed_area.intersection(exclusion).area
        allowed_area = as_polygonal(allowed_area.difference(exclusion))
        evaluation.update(
            status="applied",
            min_distance_m=distance_m,
            buffer_distance_in_dxf_units=buffer_distance,
            source_geometry_type=target_geometry.geom_type,
            area_before_in_dxf_square_units=area_before,
            excluded_area_in_dxf_square_units=excluded_from_current_area,
            area_after_in_dxf_square_units=allowed_area.area,
        )
        evaluations.append(evaluation)
        print(
            f"    area: {area_before:.3f} -> {allowed_area.area:.3f}",
            flush=True,
        )
    return allowed_area, evaluations, warnings


def build_plant_allow_zones(
    constraint_map_path: Path,
    normalized_path: Path,
    output_path: Path,
    report_path: Path,
    dsn: str,
    plant_types: set[str] | None,
    dxf_units_per_meter: float,
    cleaned_utilities_path: Path | None = None,
    unit_metadata_path: Path | None = None,
) -> None:
    """Build and write one provisional allow-zone feature per plant class."""
    base_allowed_area = as_polygonal(
        read_object_geometry(constraint_map_path, "base_allowed_area")
    )
    if base_allowed_area.is_empty:
        raise ValueError("base_allowed_area is empty")

    normalized_objects = load_normalized_objects(normalized_path)
    rules_by_plant_type = load_rules(dsn, plant_types)
    geometry_sources = {
        object_type: "normalized_raw_geometry"
        for object_type in normalized_objects
    }
    try:
        reconstructed_sidewalk = read_object_geometry(
            constraint_map_path, "sidewalk_area"
        )
    except ValueError:
        reconstructed_sidewalk = None
    sidewalk_setback = max(
        (
            validate_distance_rule(rule) * dxf_units_per_meter
            for rules in rules_by_plant_type.values()
            for rule in rules
            if rule["target_object_type"] == "sidewalk"
            and rule["conditions"].get("check") == "min_distance"
        ),
        default=0.0,
    )
    resolved_sidewalk = relevant_sidewalk_geometry(
        normalized_objects.get("sidewalk"),
        reconstructed_sidewalk,
        normalized_objects.get("work_boundary", base_allowed_area),
        sidewalk_setback,
    )
    if resolved_sidewalk is not None:
        normalized_objects["sidewalk"] = resolved_sidewalk
        if reconstructed_sidewalk is not None and not reconstructed_sidewalk.is_empty:
            geometry_sources["sidewalk"] = "reconstructed_sidewalk_area"
    else:
        normalized_objects.pop("sidewalk", None)
        geometry_sources.pop("sidewalk", None)
    building_linework = normalized_objects.get("building_linework")
    if building_linework is not None and not building_linework.is_empty:
        building_footprints = normalized_objects.get("building")
        building_sources = [building_linework]
        if building_footprints is not None and not building_footprints.is_empty:
            building_sources.append(building_footprints)
        normalized_objects["building"] = unary_union(building_sources)
        geometry_sources["building"] = (
            "verified_footprints_plus_source_building_edges"
        )
    utility_geometries: dict[str, Any] = {}
    utility_geometry_metadata: dict[str, dict[str, Any]] = {}
    ignored_cleaned_utility_types: list[str] = []
    if cleaned_utilities_path is not None:
        loaded_utility_geometries, loaded_utility_metadata = load_utility_geometries(
            cleaned_utilities_path
        )
        referenced_utility_targets = {
            rule["target_object_type"]
            for rules in rules_by_plant_type.values()
            for rule in rules
            if rule["target_object_type"] in UTILITY_OBJECT_TYPES
        }
        utility_geometries = {
            object_type: geometry
            for object_type, geometry in loaded_utility_geometries.items()
            if object_type in referenced_utility_targets
        }
        utility_geometry_metadata = {
            object_type: loaded_utility_metadata[object_type]
            for object_type in utility_geometries
        }
        ignored_cleaned_utility_types = sorted(
            set(loaded_utility_geometries) - set(utility_geometries)
        )
        normalized_objects.update(utility_geometries)
        geometry_sources.update({
            object_type: (
                "reconstructed_review_geometry"
                if utility_geometry_metadata[object_type]["manual_review_required"]
                else (
                    "reconstructed_high_confidence_geometry"
                    if utility_geometry_metadata[object_type]["source_kind"]
                    == "reconstructed"
                    else "cleaned_high_confidence_geometry"
                )
            )
            for object_type in utility_geometries
        })
    # Keep the complete catalog in the report so the downstream planting
    # service can offer species for area types (for example herbaceous cover)
    # even when that type has no distance rule of its own.
    plants_by_plant_type = load_plants(dsn, None)
    unit_metadata: dict[str, Any] = {}
    if unit_metadata_path is not None:
        unit_metadata = json.loads(unit_metadata_path.read_text(encoding="utf-8-sig"))
        if not unit_metadata.get("unit_scale_confirmed", False):
            raise ValueError("DXF unit metadata does not confirm the drawing scale")
        metadata_scale = float(unit_metadata["dxf_units_per_meter"])
        if not math.isclose(
            metadata_scale, dxf_units_per_meter, rel_tol=1e-9, abs_tol=1e-12
        ):
            raise ValueError(
                "--dxf-units-per-meter does not match --unit-metadata: "
                f"{dxf_units_per_meter:g} versus {metadata_scale:g}"
            )
    report: dict[str, Any] = {
        "constraint_map_input": str(constraint_map_path),
        "normalized_input": str(normalized_path),
        "cleaned_utilities_input": (
            str(cleaned_utilities_path) if cleaned_utilities_path else None
        ),
        "unit_metadata_input": (
            str(unit_metadata_path) if unit_metadata_path else None
        ),
        "utility_geometry_input": (
            str(cleaned_utilities_path) if cleaned_utilities_path else None
        ),
        "cleaned_utility_object_types": sorted(utility_geometries),
        "utility_geometry_object_types": sorted(utility_geometries),
        "reconstructed_utility_object_types": sorted(
            object_type
            for object_type, item in utility_geometry_metadata.items()
            if item["source_kind"] == "reconstructed"
        ),
        "utility_geometry_metadata": utility_geometry_metadata,
        "inferred_connection_count_by_type": {
            object_type: item["inferred_connection_count"]
            for object_type, item in utility_geometry_metadata.items()
        },
        "ignored_cleaned_utility_object_types": ignored_cleaned_utility_types,
        "output": str(output_path),
        "coordinate_reference": "local_dxf_coordinates",
        "dxf_units_per_meter": dxf_units_per_meter,
        "unit_scale_confirmed": bool(unit_metadata.get("unit_scale_confirmed", False)),
        "unit_scale_source": unit_metadata.get("unit_scale_source"),
        "insert_units_code": unit_metadata.get("insert_units_code"),
        "insert_units_name": unit_metadata.get("insert_units_name"),
        "unit_assumption_requires_confirmation": not bool(
            unit_metadata.get("unit_scale_confirmed", False)
        ),
        "base_allowed_area_in_dxf_square_units": base_allowed_area.area,
        "plant_types": {},
        "plant_catalog": {
            plant_type: [
                plant for plant in plants
                if not plant["is_invasive"]
                and plant.get("climate_suitability", "recommended") != "seasonal_only"
            ]
            for plant_type, plants in sorted(plants_by_plant_type.items())
        },
    }

    features = []
    exclusion_cache: dict[tuple[str, float], Any] = {}
    for plant_type in sorted(rules_by_plant_type):
        catalog_plants = plants_by_plant_type.get(plant_type, [])
        selectable_plants = [
            plant for plant in catalog_plants
            if not plant["is_invasive"]
            and plant.get("climate_suitability", "recommended") != "seasonal_only"
        ]
        invasive_plants = [
            plant for plant in catalog_plants if plant["is_invasive"]
        ]
        climate_excluded_plants = [
            plant for plant in catalog_plants
            if plant.get("climate_suitability") == "seasonal_only"
        ]
        catalog_warnings = []
        if not selectable_plants:
            catalog_warnings.append(
                f"{plant_type}: no non-invasive catalog plants are available"
            )
        missing_spacing = sum(
            plant["min_spacing_m"] is None for plant in selectable_plants
        )
        missing_recommended_spacing = sum(
            plant.get("recommended_spacing_m") is None for plant in selectable_plants
        )
        missing_crown_radius = sum(
            plant.get("mature_crown_radius_m") is None for plant in selectable_plants
        )
        missing_toxicity = sum(
            plant["is_toxic"] is None for plant in selectable_plants
        )
        missing_thorniness = sum(
            plant["is_thorny"] is None for plant in selectable_plants
        )
        if missing_spacing:
            catalog_warnings.append(
                f"{plant_type}: {missing_spacing} selectable plant(s) have "
                "unverified min_spacing_m"
            )
        if missing_recommended_spacing:
            catalog_warnings.append(
                f"{plant_type}: {missing_recommended_spacing} selectable plant(s) have "
                "unverified recommended_spacing_m"
            )
        if missing_crown_radius:
            catalog_warnings.append(
                f"{plant_type}: {missing_crown_radius} selectable plant(s) have "
                "unverified mature_crown_radius_m"
            )
        if missing_toxicity:
            catalog_warnings.append(
                f"{plant_type}: {missing_toxicity} selectable plant(s) have "
                "unverified toxicity"
            )
        if missing_thorniness:
            catalog_warnings.append(
                f"{plant_type}: {missing_thorniness} selectable plant(s) have "
                "unverified thorniness"
            )

        allowed_area, evaluations, warnings = apply_rules(
            plant_type,
            base_allowed_area,
            normalized_objects,
            rules_by_plant_type[plant_type],
            dxf_units_per_meter,
            exclusion_cache,
            geometry_sources,
            utility_geometry_metadata,
        )
        configured_utility_types = {
            rule["target_object_type"]
            for rule in rules_by_plant_type[plant_type]
            if rule["target_object_type"] in UTILITY_OBJECT_TYPES
        }
        unchecked_utility_types = sorted(
            object_type
            for object_type in UTILITY_OBJECT_TYPES - configured_utility_types
            if object_type in normalized_objects
            and not normalized_objects[object_type].is_empty
        )
        if unchecked_utility_types:
            warnings.append(
                "No active placement rule for utility types: "
                + ", ".join(unchecked_utility_types)
            )
        unresolved = [
            item["rule_code"]
            for item in evaluations
            if item["status"] != "applied"
        ]
        applied = [
            item["rule_code"]
            for item in evaluations
            if item["status"] == "applied"
        ]
        verification_status = (
            "requires_manual_review" if unresolved else "verified_by_available_rules"
        )
        features.append(
            {
                "type": "Feature",
                "id": f"plant_allow_zone_{plant_type}",
                "properties": {
                    "object_type": "plant_allow_zone",
                    "plant_type": plant_type,
                    "verification_status": verification_status,
                    "coordinate_reference": "local_dxf_coordinates",
                    "dxf_units_per_meter": dxf_units_per_meter,
                    "area_in_dxf_square_units": allowed_area.area,
                    "applied_rules": applied,
                    "unresolved_rules": unresolved,
                    "unchecked_utility_object_types": unchecked_utility_types,
                    "selectable_plants": selectable_plants,
                    "excluded_invasive_plants": [
                        plant["name"] for plant in invasive_plants
                    ],
                    "excluded_not_winter_hardy_plants": [
                        plant["name"] for plant in climate_excluded_plants
                    ],
                    "catalog_warnings": catalog_warnings,
                },
                "geometry": mapping(allowed_area),
            }
        )
        report["plant_types"][plant_type] = {
            "verification_status": verification_status,
            "geometry_type": allowed_area.geom_type,
            "base_area_in_dxf_square_units": base_allowed_area.area,
            "allowed_area_in_dxf_square_units": allowed_area.area,
            "excluded_area_in_dxf_square_units": (
                base_allowed_area.area - allowed_area.area
            ),
            "rules": evaluations,
            "unchecked_utility_object_types": unchecked_utility_types,
            "verification_scope": "configured_rules_only",
            "warnings": warnings,
            "plant_catalog": {
                "total": len(catalog_plants),
                "selectable": selectable_plants,
                "excluded_invasive": invasive_plants,
                "excluded_not_winter_hardy": climate_excluded_plants,
                "target_hardiness_zone": TARGET_HARDINESS_ZONE,
                "missing_min_spacing_count": missing_spacing,
                "missing_recommended_spacing_count": missing_recommended_spacing,
                "missing_crown_radius_count": missing_crown_radius,
                "missing_toxicity_count": missing_toxicity,
                "missing_thorniness_count": missing_thorniness,
                "warnings": catalog_warnings,
            },
        }

    output_path.write_text(
        "".join(
            json.dumps(feature, ensure_ascii=False) + "\n"
            for feature in features
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Constraint map: {constraint_map_path}")
    print(f"Normalized objects: {normalized_path}")
    print(f"Output: {output_path}")
    print(f"Report: {report_path}")
    print(f"Base allowed area: {base_allowed_area.area:.3f} square DXF units")
    for plant_type, result in report["plant_types"].items():
        print(
            f"  {plant_type}: {result['allowed_area_in_dxf_square_units']:.3f} "
            f"| {result['verification_status']} "
            f"| {result['plant_catalog']['total']} plant(s), "
            f"{len(result['plant_catalog']['excluded_invasive'])} invasive excluded "
            f"| {len(result['warnings'])} rule warning(s)"
        )
    if report["unit_assumption_requires_confirmation"]:
        print(
            "WARNING: one metre is assumed to equal "
            f"{dxf_units_per_meter:g} DXF unit(s); confirm the drawing scale"
        )
    else:
        print(
            "DXF unit scale confirmed: "
            f"{dxf_units_per_meter:g} unit(s) per metre "
            f"({report['unit_scale_source']})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build per-plant-class allow zones using PostGIS rules."
    )
    parser.add_argument("constraint_map_geojsonl", type=Path)
    parser.add_argument("normalized_objects_geojsonl", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("plant_allow_zones.geojsonl")
    )
    parser.add_argument(
        "--utility-geometries",
        "--cleaned-utilities",
        dest="cleaned_utilities",
        type=Path,
        help=(
            "Accepted utility GeoJSONL produced by network_reconstructor (preferred) "
            "or utility_cleaner; this geometry replaces raw utility objects before "
            "automatic distance rules are applied"
        ),
    )
    parser.add_argument(
        "--report", type=Path, default=Path("plant_allow_zones_report.json")
    )
    parser.add_argument(
        "--plant-types",
        help="Comma-separated plant classes. Defaults to every class with rules.",
    )
    parser.add_argument(
        "--dxf-units-per-meter",
        type=float,
        default=1.0,
        help="Scale for converting normative metres to drawing units (default: 1).",
    )
    parser.add_argument(
        "--unit-metadata",
        type=Path,
        help="JSON report produced by scripts/detect_dxf_units.py",
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL", DEFAULT_DSN),
        help="PostgreSQL DSN; defaults to DATABASE_URL or local Docker Compose.",
    )
    args = parser.parse_args()
    if not math.isfinite(args.dxf_units_per_meter) or args.dxf_units_per_meter <= 0:
        raise SystemExit("--dxf-units-per-meter must be finite and greater than zero")
    plant_types = (
        {item.strip() for item in args.plant_types.split(",") if item.strip()}
        if args.plant_types
        else None
    )
    try:
        build_plant_allow_zones(
            args.constraint_map_geojsonl,
            args.normalized_objects_geojsonl,
            args.output,
            args.report,
            args.dsn,
            plant_types,
            args.dxf_units_per_meter,
            args.cleaned_utilities,
            args.unit_metadata,
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise SystemExit(f"Plant allow-zone error: {error}") from error


if __name__ == "__main__":
    main()
