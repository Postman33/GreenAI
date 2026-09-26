"""Verify spatial invariants of generated constraint and planting-zone files."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from shapely.geometry import GeometryCollection, shape
from shapely.ops import unary_union


AREA_TOLERANCE = 1e-4
NPA_REFERENCE_MARKERS = (
    "сп ",
    "-пп",
    "мгсн",
    "санпин",
    "гост",
    "постановлен",
    "федеральн",
)


def polygon_part_count(geometry: Any) -> int:
    if geometry.geom_type == "Polygon":
        return 1
    if geometry.geom_type == "MultiPolygon":
        return len(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        return sum(polygon_part_count(item) for item in geometry.geoms)
    return 0


def resolve_report_reference(report_path: Path, value: Any) -> Path | None:
    if value is None or not str(value).strip():
        return None
    candidate = Path(str(value))
    if candidate.is_absolute():
        return candidate
    cwd_candidate = Path.cwd() / candidate
    if cwd_candidate.exists():
        return cwd_candidate.resolve()
    return (report_path.parent / candidate).resolve()


def create_dxf_manifest(input_dxf: Path, output_json: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("dxf_manifest.py")),
        str(input_dxf),
        str(output_json),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"DXF manifest failed for {input_dxf}: {details}")
    return json.loads(output_json.read_text(encoding="utf-8"))


def has_npa_reference(checks: list[dict[str, Any]]) -> bool:
    """Return True when at least one check names a legal/normative source."""
    return any(
        any(marker in str(item.get("norm_reference", "")).casefold() for marker in NPA_REFERENCE_MARKERS)
        for item in checks
    )


def read_by_object_type(path: Path) -> dict[str, list[tuple[dict[str, Any], Any]]]:
    result: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            feature = json.loads(line)
            if feature.get("type") != "Feature":
                raise ValueError(f"{path}:{line_number}: expected GeoJSON Feature")
            properties = feature.get("properties", {})
            object_type = properties.get("object_type")
            if not object_type:
                raise ValueError(f"{path}:{line_number}: object_type is missing")
            result.setdefault(object_type, []).append(
                (properties, shape(feature["geometry"]))
            )
    return result


def one(
    records: dict[str, list[tuple[dict[str, Any], Any]]], object_type: str
) -> tuple[dict[str, Any], Any]:
    matches = records.get(object_type, [])
    if len(matches) != 1:
        raise ValueError(f"Expected one {object_type!r} feature, got {len(matches)}")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("constraint_map", type=Path)
    parser.add_argument("plant_zones", type=Path)
    parser.add_argument("--planting-plan", type=Path)
    parser.add_argument("--zone-report", type=Path)
    parser.add_argument("--plan-report", type=Path)
    parser.add_argument("--input-dxf", type=Path)
    parser.add_argument("--output-dxf", type=Path)
    parser.add_argument("--output", type=Path, default=Path("verification_report.json"))
    args = parser.parse_args()

    constraints = read_by_object_type(args.constraint_map)
    zones = read_by_object_type(args.plant_zones)
    _, base = one(constraints, "base_allowed_area")
    road_features = constraints.get("road_area", [])
    if len(road_features) > 1:
        raise ValueError(f"Expected at most one 'road_area' feature, got {len(road_features)}")
    road = road_features[0][1] if road_features else GeometryCollection()
    _, hard_surfaces = one(constraints, "hard_surface_area")
    sidewalk_features = constraints.get("sidewalk_area", [])
    if len(sidewalk_features) > 1:
        raise ValueError(f"Expected at most one 'sidewalk_area' feature, got {len(sidewalk_features)}")
    sidewalks = sidewalk_features[0][1] if sidewalk_features else GeometryCollection()
    _, confirmed_plantable = one(
        constraints, "confirmed_plantable_surface"
    )
    building_parts = [
        geometry
        for _properties, geometry in constraints.get("buildings_in_work_area", [])
    ]
    buildings = unary_union(building_parts) if building_parts else GeometryCollection()
    utility_well_parts = [
        geometry
        for _properties, geometry in constraints.get("utility_well_footprints", [])
    ]
    utility_wells = (
        unary_union(utility_well_parts) if utility_well_parts else GeometryCollection()
    )
    heat_chamber_parts = [
        geometry
        for _properties, geometry in constraints.get("heat_chamber_footprints", [])
    ]
    heat_chambers = (
        unary_union(heat_chamber_parts)
        if heat_chamber_parts else GeometryCollection()
    )

    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    for properties, geometry in zones.get("plant_allow_zone", []):
        plant_type = properties.get("plant_type", "unknown")
        outside_base = geometry.difference(base).area
        road_overlap = geometry.intersection(road).area
        hard_surface_overlap = geometry.intersection(hard_surfaces).area
        sidewalk_overlap = geometry.intersection(sidewalks).area
        building_overlap = geometry.intersection(buildings).area
        utility_well_overlap = geometry.intersection(utility_wells).area
        heat_chamber_overlap = geometry.intersection(heat_chambers).area
        outside_confirmed_plantable = geometry.difference(
            confirmed_plantable
        ).area
        declared_area = float(properties.get("area_in_dxf_square_units", geometry.area))
        area_error = abs(declared_area - geometry.area)
        record = {
            "plant_type": plant_type,
            "geometry_type": geometry.geom_type,
            "is_valid": geometry.is_valid,
            "is_empty": geometry.is_empty,
            "area_in_dxf_square_units": geometry.area,
            "declared_area_error": area_error,
            "outside_base_area": outside_base,
            "road_overlap_area": road_overlap,
            "hard_surface_overlap_area": hard_surface_overlap,
            "sidewalk_overlap_area": sidewalk_overlap,
            "building_overlap_area": building_overlap,
            "utility_well_overlap_area": utility_well_overlap,
            "heat_chamber_overlap_area": heat_chamber_overlap,
            "outside_confirmed_plantable_area": outside_confirmed_plantable,
        }
        checks.append(record)
        if not geometry.is_valid or geometry.is_empty:
            failures.append(f"{plant_type}: invalid or empty geometry")
        if area_error > AREA_TOLERANCE:
            failures.append(f"{plant_type}: declared area differs from geometry")
        if outside_base > AREA_TOLERANCE:
            failures.append(f"{plant_type}: zone extends outside base area")
        if road_overlap > AREA_TOLERANCE:
            failures.append(f"{plant_type}: zone overlaps reconstructed road")
        if hard_surface_overlap > AREA_TOLERANCE:
            failures.append(f"{plant_type}: zone overlaps hard surfaces")
        if sidewalk_overlap > AREA_TOLERANCE:
            failures.append(f"{plant_type}: zone overlaps sidewalks")
        if building_overlap > AREA_TOLERANCE:
            failures.append(f"{plant_type}: zone overlaps buildings")
        if utility_well_overlap > AREA_TOLERANCE:
            failures.append(f"{plant_type}: zone overlaps utility wells")
        if heat_chamber_overlap > AREA_TOLERANCE:
            failures.append(f"{plant_type}: zone overlaps heat chambers")
        if outside_confirmed_plantable > AREA_TOLERANCE:
            failures.append(
                f"{plant_type}: zone extends outside confirmed plantable surfaces"
            )

    if not checks:
        failures.append("No plant_allow_zone features found")

    zone_by_type = {
        str(properties.get("plant_type")): geometry
        for properties, geometry in zones.get("plant_allow_zone", [])
    }
    provenance_checks: dict[str, Any] | None = None
    if args.zone_report is not None:
        zone_report = json.loads(args.zone_report.read_text(encoding="utf-8"))
        reference_fields = (
            "constraint_map_input",
            "normalized_input",
            "utility_geometry_input",
            "unit_metadata_input",
            "output",
        )
        resolved_references = {
            field: resolve_report_reference(args.zone_report, zone_report.get(field))
            for field in reference_fields
        }
        missing_references = sorted(
            field
            for field, path in resolved_references.items()
            if path is not None and not path.exists()
        )
        path_mismatches: list[str] = []
        expected_paths = {
            "constraint_map_input": args.constraint_map.resolve(),
            "output": args.plant_zones.resolve(),
        }
        for field, expected in expected_paths.items():
            actual = resolved_references.get(field)
            if actual is not None and actual != expected:
                path_mismatches.append(field)
        area_mismatches: list[str] = []
        report_plant_types = zone_report.get("plant_types", {})
        for plant_type, geometry in zone_by_type.items():
            report_item = report_plant_types.get(plant_type)
            if report_item is None:
                area_mismatches.append(f"{plant_type}:missing")
                continue
            reported_area = report_item.get("allowed_area_in_dxf_square_units")
            if reported_area is None or abs(float(reported_area) - geometry.area) > AREA_TOLERANCE:
                area_mismatches.append(plant_type)
        duplicate_rule_codes: dict[str, list[str]] = {}
        for plant_type, report_item in report_plant_types.items():
            codes = [str(rule.get("rule_code", "")) for rule in report_item.get("rules", [])]
            duplicates = sorted(code for code, count in Counter(codes).items() if not code or count > 1)
            if duplicates:
                duplicate_rule_codes[plant_type] = duplicates
        provenance_checks = {
            "zone_report": str(args.zone_report),
            "resolved_references": {
                field: str(path) if path is not None else None
                for field, path in resolved_references.items()
            },
            "missing_reference_fields": missing_references,
            "path_mismatch_fields": path_mismatches,
            "zone_area_mismatches": area_mismatches,
            "duplicate_or_empty_rule_codes": duplicate_rule_codes,
            "unit_assumption_requires_confirmation": bool(
                zone_report.get("unit_assumption_requires_confirmation", False)
            ),
        }
        unit_metadata_path = resolved_references.get("unit_metadata_input")
        unit_metadata_consistent = False
        if unit_metadata_path is not None and unit_metadata_path.exists():
            unit_metadata = json.loads(
                unit_metadata_path.read_text(encoding="utf-8-sig")
            )
            unit_metadata_consistent = bool(
                unit_metadata.get("unit_scale_confirmed", False)
            ) and math.isclose(
                float(unit_metadata.get("dxf_units_per_meter", math.nan)),
                float(zone_report.get("dxf_units_per_meter", math.nan)),
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        provenance_checks["unit_metadata_consistent"] = unit_metadata_consistent
        if missing_references:
            failures.append(
                "Zone report references missing artifacts: "
                + ", ".join(missing_references)
            )
        if path_mismatches:
            failures.append(
                "Zone report was produced from different artifacts: "
                + ", ".join(path_mismatches)
            )
        if area_mismatches:
            failures.append(
                "Zone report areas do not match plant-zone geometry: "
                + ", ".join(area_mismatches)
            )
        if duplicate_rule_codes:
            failures.append("Zone report has duplicate or empty rule codes")
        if provenance_checks["unit_assumption_requires_confirmation"]:
            failures.append("DXF unit scale is not confirmed")
        if unit_metadata_path is not None and not unit_metadata_consistent:
            failures.append("DXF unit metadata does not match the zone report")

    plan_checks: dict[str, Any] | None = None
    if args.planting_plan is not None:
        plan = read_by_object_type(args.planting_plan)
        point_records = plan.get("proposed_planting", [])
        area_records = plan.get("proposed_planting_area", [])
        ids: list[str] = []
        point_ids: list[str] = []
        point_failures = 0
        area_failures = 0
        missing_norm_references = 0
        missing_npa_references = 0
        missing_explanations = 0
        outside_zone_area = 0.0
        spacing_failures = 0
        measured_distance_failures = 0
        status_mismatches = 0
        manual_review_count = 0
        area_outside_plant_zone = 0.0
        same_type_area_overlap = 0.0
        cross_type_area_overlap = 0.0
        points_by_type: dict[str, list[tuple[dict[str, Any], Any]]] = {}
        areas_by_type: dict[str, list[Any]] = {}
        for properties, geometry in point_records:
            planting_id = str(properties.get("planting_id", ""))
            ids.append(planting_id)
            point_ids.append(planting_id)
            plant_type = str(properties.get("plant_type", ""))
            zone = zone_by_type.get(plant_type)
            footprint_radius = float(properties.get("footprint_radius_m", 0.0)) * float(
                properties.get("dxf_units_per_meter", 1.0)
            )
            if geometry.geom_type != "Point" or zone is None:
                point_failures += 1
                continue
            footprint = geometry.buffer(footprint_radius, quad_segs=8)
            outside_zone_area += footprint.difference(zone).area
            point_checks = properties.get("checks") or []
            if not point_checks or any(item.get("status") == "failed" for item in point_checks):
                point_failures += 1
            expected_status = (
                "manual_review"
                if any(item.get("status") == "manual_review" for item in point_checks)
                else "accepted"
            )
            if properties.get("status") != expected_status:
                status_mismatches += 1
            if expected_status == "manual_review":
                manual_review_count += 1
            for item in point_checks:
                if item.get("code") not in {"ALLOWED_ZONE"} and not item.get("norm_reference"):
                    missing_norm_references += 1
                if not item.get("explanation"):
                    missing_explanations += 1
                actual = item.get("actual_distance_m")
                required = item.get("required_distance_m")
                if (
                    item.get("status") == "passed"
                    and actual is not None
                    and required is not None
                    and float(actual) + 1e-7 < float(required)
                ):
                    measured_distance_failures += 1
            if not has_npa_reference(point_checks):
                missing_npa_references += 1
            points_by_type.setdefault(plant_type, []).append((properties, geometry))
        for properties, geometry in area_records:
            planting_id = str(properties.get("planting_id", ""))
            ids.append(planting_id)
            plant_type = str(properties.get("plant_type", ""))
            area_checks = properties.get("checks") or []
            if (
                geometry.geom_type not in {"Polygon", "MultiPolygon"}
                or geometry.is_empty
                or not geometry.is_valid
                or not area_checks
                or any(item.get("status") == "failed" for item in area_checks)
            ):
                area_failures += 1
            expected_status = (
                "manual_review"
                if any(item.get("status") == "manual_review" for item in area_checks)
                else "accepted"
            )
            if properties.get("status") != expected_status:
                status_mismatches += 1
            if expected_status == "manual_review":
                manual_review_count += 1
            for item in area_checks:
                if not item.get("norm_reference"):
                    missing_norm_references += 1
                if not item.get("explanation"):
                    missing_explanations += 1
                actual = item.get("actual_distance_m")
                required = item.get("required_distance_m")
                if (
                    item.get("status") == "passed"
                    and actual is not None
                    and required is not None
                    and float(actual) + 1e-7 < float(required)
                ):
                    measured_distance_failures += 1
            if not has_npa_reference(area_checks):
                missing_npa_references += 1
            plant_zone = zone_by_type.get(plant_type)
            if plant_zone is not None:
                area_outside_plant_zone += geometry.difference(plant_zone).area
            elif plant_type != "herbaceous":
                area_failures += 1
            areas_by_type.setdefault(plant_type, []).append(geometry)
        for plant_type, items in points_by_type.items():
            for index, (properties, point) in enumerate(items):
                required = float(properties.get("spacing_m", 0.0)) * float(
                    properties.get("dxf_units_per_meter", 1.0)
                )
                for other_properties, other in items[index + 1:]:
                    other_units = float(other_properties.get("dxf_units_per_meter", 1.0))
                    pair_required = max(
                        required,
                        float(other_properties.get("spacing_m", 0.0)) * other_units,
                        float(properties.get("footprint_radius_m", 0.0)) * float(properties.get("dxf_units_per_meter", 1.0))
                        + float(other_properties.get("footprint_radius_m", 0.0)) * other_units,
                    )
                    if point.distance(other) + 1e-7 < pair_required:
                        spacing_failures += 1
        area_unions: dict[str, Any] = {}
        for plant_type, geometries in areas_by_type.items():
            merged = unary_union(geometries)
            area_unions[plant_type] = merged
            same_type_area_overlap += max(
                0.0, sum(item.area for item in geometries) - merged.area
            )
        cross_type_overlap_pairs: list[dict[str, Any]] = []
        for left_type, right_type in combinations(sorted(area_unions), 2):
            overlap = area_unions[left_type].intersection(area_unions[right_type]).area
            cross_type_area_overlap += overlap
            if overlap > AREA_TOLERANCE:
                cross_type_overlap_pairs.append(
                    {
                        "plant_types": [left_type, right_type],
                        "overlap_area": overlap,
                    }
                )
        area_outside_base = sum(
            geometry.difference(base).area for _properties, geometry in area_records
        )
        unique_ids = len(set(ids))
        plan_checks = {
            "point_placement_count": len(point_records),
            "area_placement_count": len(area_records),
            "unique_point_id_count": len(set(point_ids)),
            "unique_placement_id_count": unique_ids,
            "point_failures": point_failures,
            "area_failures": area_failures,
            "missing_norm_reference_count": missing_norm_references,
            "missing_npa_reference_count": missing_npa_references,
            "missing_explanation_count": missing_explanations,
            "footprint_outside_allow_zone_area": outside_zone_area,
            "same_type_spacing_failure_count": spacing_failures,
            "measured_distance_failure_count": measured_distance_failures,
            "status_mismatch_count": status_mismatches,
            "manual_review_count": manual_review_count,
            "coverage_outside_base_area": area_outside_base,
            "coverage_outside_plant_zone_area": area_outside_plant_zone,
            "same_type_area_overlap": same_type_area_overlap,
            "cross_type_area_overlap": cross_type_area_overlap,
            "cross_type_overlap_pairs": cross_type_overlap_pairs,
        }
        if not point_records and not area_records:
            failures.append("Planting plan has no placements")
        if unique_ids != len(ids) or any(not value for value in ids):
            failures.append("Planting IDs are empty or not unique")
        if point_failures:
            failures.append(f"Planting plan has {point_failures} invalid point records")
        if area_failures:
            failures.append(f"Planting plan has {area_failures} invalid area records")
        if missing_norm_references:
            failures.append(
                f"Planting plan has {missing_norm_references} checks without a normative/catalog reference"
            )
        if missing_npa_references:
            failures.append(
                f"Planting plan has {missing_npa_references} placements without an NPA reference"
            )
        if missing_explanations:
            failures.append(
                f"Planting plan has {missing_explanations} checks without an explanation"
            )
        if outside_zone_area > AREA_TOLERANCE:
            failures.append("One or more complete planting footprints leave their allow zone")
        if spacing_failures:
            failures.append(f"Planting plan has {spacing_failures} same-type spacing violations")
        if measured_distance_failures:
            failures.append(
                f"Planting plan has {measured_distance_failures} passed checks with an insufficient measured distance"
            )
        if status_mismatches:
            failures.append(
                f"Planting plan has {status_mismatches} placement statuses inconsistent with their checks"
            )
        if area_outside_base > AREA_TOLERANCE:
            failures.append("One or more area plantings extend outside base area")
        if area_outside_plant_zone > AREA_TOLERANCE:
            failures.append("One or more area plantings extend outside their plant allow zone")
        if same_type_area_overlap > AREA_TOLERANCE:
            failures.append("Area plantings of the same type overlap each other")
        if cross_type_area_overlap > AREA_TOLERANCE:
            failures.append("Area plantings of different types overlap each other")

        if args.plan_report is not None:
            plan_report = json.loads(args.plan_report.read_text(encoding="utf-8"))
            plan_output = resolve_report_reference(
                args.plan_report, plan_report.get("output")
            )
            decisions_output = resolve_report_reference(
                args.plan_report, plan_report.get("decisions_output")
            )
            explanations_output = resolve_report_reference(
                args.plan_report, plan_report.get("explanations_output")
            )
            explanation_ids: set[str] = set()
            if explanations_output is not None and explanations_output.exists():
                for line in explanations_output.read_text(encoding="utf-8-sig").splitlines():
                    if line.startswith("## "):
                        explanation_ids.add(line[3:].strip())
            expected_explanation_ids = set(ids)
            count_mismatches: list[str] = []
            expected_counts = {
                "point_placement_count": len(point_records),
                "area_placement_count": len(area_records),
                "feature_count": len(point_records) + len(area_records),
                "unique_id_count": unique_ids,
                "unique_feature_id_count": unique_ids,
                "unique_point_id_count": len(set(point_ids)),
                "manual_review_count": manual_review_count,
            }
            for field, expected in expected_counts.items():
                if plan_report.get(field) != expected:
                    count_mismatches.append(field)
            plan_report_check = {
                "plan_report": str(args.plan_report),
                "reported_status": plan_report.get("status"),
                "resolved_output": str(plan_output) if plan_output else None,
                "resolved_decisions_output": (
                    str(decisions_output) if decisions_output else None
                ),
                "resolved_explanations_output": (
                    str(explanations_output) if explanations_output else None
                ),
                "output_matches_argument": plan_output == args.planting_plan.resolve(),
                "decisions_output_exists": bool(
                    decisions_output is not None and decisions_output.exists()
                ),
                "explanations_output_exists": bool(
                    explanations_output is not None and explanations_output.exists()
                ),
                "missing_explanation_ids": sorted(
                    expected_explanation_ids - explanation_ids
                ),
                "unexpected_explanation_ids": sorted(
                    explanation_ids - expected_explanation_ids
                ),
                "count_mismatch_fields": count_mismatches,
            }
            plan_checks["plan_report"] = plan_report_check
            if plan_report.get("status") != "passed":
                failures.append("Planting plan report status is not passed")
            if plan_output != args.planting_plan.resolve():
                failures.append("Planting plan report references a different plan output")
            if decisions_output is None or not decisions_output.exists():
                failures.append("Planting plan report references a missing decisions output")
            if explanations_output is None or not explanations_output.exists():
                failures.append("Planting plan report references a missing explanations output")
            elif explanation_ids != expected_explanation_ids:
                failures.append(
                    "Planting explanations do not contain exactly the plan IDs"
                )
            if count_mismatches:
                failures.append(
                    "Planting plan report counts do not match the plan: "
                    + ", ".join(count_mismatches)
                )

    dxf_checks: dict[str, Any] | None = None
    if (args.input_dxf is None) != (args.output_dxf is None):
        failures.append("Both --input-dxf and --output-dxf are required for DXF verification")
    elif args.input_dxf is not None and args.output_dxf is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".verify-dxf-", dir=args.output.parent.resolve()
        ) as manifest_directory:
            manifest_root = Path(manifest_directory)
            input_manifest = create_dxf_manifest(
                args.input_dxf, manifest_root / "input.json"
            )
            output_manifest = create_dxf_manifest(
                args.output_dxf, manifest_root / "output.json"
            )
        input_records = input_manifest["entities"]
        output_records = output_manifest["entities"]
        input_entities = Counter(
            (record["handle"], record["type"], record["layer"])
            for record in input_records
        )
        output_by_handle = {
            record["handle"]: record for record in output_records
        }
        output_entities = Counter(
            (record["handle"], record["type"], record["layer"])
            for record in output_records
            if not record["layer"].startswith("GREEN_AI_")
        )
        missing_source_entities = sum((input_entities - output_entities).values())
        changed_source_entities = sum((output_entities - input_entities).values())
        changed_source_content_handles: list[str] = []
        changed_source_content_types: Counter[str] = Counter()
        missing_source_handles: list[str] = []
        for input_record in input_records:
            handle = input_record["handle"]
            output_record = output_by_handle.get(handle)
            if output_record is None:
                missing_source_handles.append(handle)
                continue
            if input_record["fingerprint"] != output_record["fingerprint"]:
                changed_source_content_handles.append(handle)
                changed_source_content_types[output_record["type"]] += 1
        result_layer_counts = Counter(
            record["layer"]
            for record in output_records
            if record["layer"].startswith("GREEN_AI_")
        )
        result_layer_type_counts = Counter(
            (record["layer"], record["type"])
            for record in output_records
            if record["layer"].startswith("GREEN_AI_")
        )
        dxf_checks = {
            "input_modelspace_entity_count": input_manifest[
                "modelspace_entity_count"
            ],
            "preserved_source_entity_count": sum(
                record["type"] not in {"ATTRIB", "SEQEND", "VERTEX"}
                and record["owner"] == output_manifest["modelspace_owner_handle"]
                and not record["layer"].startswith("GREEN_AI_")
                for record in output_records
            ),
            "input_raw_entity_record_count": len(input_records),
            "preserved_non_result_raw_entity_record_count": sum(
                output_entities.values()
            ),
            "missing_source_entity_count": missing_source_entities,
            "changed_or_added_non_result_entity_count": changed_source_entities,
            "missing_source_handle_count": len(missing_source_handles),
            "changed_source_content_count": len(changed_source_content_handles),
            "changed_source_content_types": dict(
                sorted(changed_source_content_types.items())
            ),
            "sample_missing_source_handles": missing_source_handles[:20],
            "sample_changed_source_content_handles": changed_source_content_handles[:20],
            "result_layer_entity_counts": dict(sorted(result_layer_counts.items())),
            "green_ai_layer_states": output_manifest["green_ai_layer_states"],
        }
        if missing_source_entities or changed_source_entities:
            failures.append(
                "Output DXF does not preserve the input modelspace entity handles, types and layers"
            )
        if missing_source_handles or changed_source_content_handles:
            failures.append("Output DXF changes the tag content of source entities")

        required_result_layers: set[str] = set()
        if args.planting_plan is not None:
            layer_by_plant_type = {
                "tree": "GREEN_AI_PLANT_TREE",
                "shrub": "GREEN_AI_PLANT_SHRUB",
                "herbaceous": "GREEN_AI_HERBACEOUS",
            }
            expected_point_counts = Counter(
                str(properties.get("plant_type"))
                for properties, _geometry in point_records
            )
            expected_polygon_counts = Counter()
            expected_point_ids: dict[str, set[str]] = {}
            expected_area_ids: dict[str, set[str]] = {}
            for properties, geometry in area_records:
                plant_type = str(properties.get("plant_type"))
                expected_polygon_counts[plant_type] += (
                    polygon_part_count(geometry)
                )
                expected_area_ids.setdefault(plant_type, set()).add(
                    str(properties.get("planting_id", ""))
                )
            for properties, _geometry in point_records:
                expected_point_ids.setdefault(
                    str(properties.get("plant_type")), set()
                ).add(str(properties.get("planting_id", "")))

            result_count_mismatches: list[dict[str, Any]] = []
            result_id_mismatches: list[dict[str, Any]] = []
            for plant_type in sorted(
                set(expected_point_counts) | set(expected_polygon_counts)
            ):
                layer_name = layer_by_plant_type.get(
                    plant_type, f"GREEN_AI_PLANT_{plant_type.upper()}"
                )
                required_result_layers.add(layer_name)
                layer_entities = [
                    record
                    for record in output_records
                    if record["layer"] == layer_name
                ]
                actual_points = result_layer_type_counts[(layer_name, "CIRCLE")]
                actual_polygons = result_layer_type_counts[(layer_name, "HATCH")]
                expected_points = expected_point_counts[plant_type]
                expected_polygons = expected_polygon_counts[plant_type]
                if (
                    actual_points != expected_points
                    or actual_polygons != expected_polygons
                ):
                    result_count_mismatches.append(
                        {
                            "plant_type": plant_type,
                            "layer": layer_name,
                            "expected_points": expected_points,
                            "actual_points": actual_points,
                            "expected_polygons": expected_polygons,
                            "actual_polygons": actual_polygons,
                        }
                    )
                actual_point_ids: set[str] = set()
                actual_area_ids: set[str] = set()
                point_entities_without_id = 0
                area_entities_without_id = 0
                for record in layer_entities:
                    if record["type"] not in {"CIRCLE", "HATCH"}:
                        continue
                    planting_id = record.get("green_ai_id")
                    if record["type"] == "CIRCLE":
                        if planting_id:
                            actual_point_ids.add(str(planting_id))
                        else:
                            point_entities_without_id += 1
                    else:
                        if planting_id:
                            actual_area_ids.add(str(planting_id))
                        else:
                            area_entities_without_id += 1
                expected_points_ids = expected_point_ids.get(plant_type, set())
                expected_areas_ids = expected_area_ids.get(plant_type, set())
                if (
                    actual_point_ids != expected_points_ids
                    or actual_area_ids != expected_areas_ids
                    or point_entities_without_id
                    or area_entities_without_id
                ):
                    result_id_mismatches.append(
                        {
                            "plant_type": plant_type,
                            "missing_point_ids": sorted(
                                expected_points_ids - actual_point_ids
                            )[:20],
                            "unexpected_point_ids": sorted(
                                actual_point_ids - expected_points_ids
                            )[:20],
                            "missing_area_ids": sorted(
                                expected_areas_ids - actual_area_ids
                            )[:20],
                            "unexpected_area_ids": sorted(
                                actual_area_ids - expected_areas_ids
                            )[:20],
                            "point_entities_without_id": point_entities_without_id,
                            "area_entities_without_id": area_entities_without_id,
                        }
                    )
            dxf_checks["planting_result_count_mismatches"] = result_count_mismatches
            dxf_checks["planting_result_id_mismatches"] = result_id_mismatches
            if result_count_mismatches:
                failures.append("Output DXF planting entity counts do not match the plan")
            if result_id_mismatches:
                failures.append("Output DXF planting IDs do not match the plan")

        missing_layers = sorted(required_result_layers - set(result_layer_counts))
        if missing_layers:
            failures.append(f"Output DXF is missing result layers: {', '.join(missing_layers)}")
        hidden_result_layers = sorted(
            layer_name
            for layer_name in required_result_layers
            if output_manifest["green_ai_layer_states"].get(layer_name, {}).get(
                "is_off", True
            )
            or output_manifest["green_ai_layer_states"].get(layer_name, {}).get(
                "is_frozen", True
            )
        )
        dxf_checks["hidden_or_frozen_result_layers"] = hidden_result_layers
        if hidden_result_layers:
            failures.append(
                "Output DXF result layers are hidden or frozen: "
                + ", ".join(hidden_result_layers)
            )

    report = {
        "constraint_map": str(args.constraint_map),
        "plant_zones": str(args.plant_zones),
        "area_tolerance": AREA_TOLERANCE,
        "status": "passed" if not failures else "failed",
        "checks": checks,
        "provenance_checks": provenance_checks,
        "planting_plan_checks": plan_checks,
        "dxf_checks": dxf_checks,
        "failures": failures,
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Status: {report['status']}")
    print(f"Report: {args.output}")
    for item in checks:
        print(
            f"  {item['plant_type']}: area={item['area_in_dxf_square_units']:.3f}, "
            f"outside_base={item['outside_base_area']:.6f}, "
            f"road_overlap={item['road_overlap_area']:.6f}, "
            f"hard_surface_overlap={item['hard_surface_overlap_area']:.6f}, "
            f"sidewalk_overlap={item['sidewalk_overlap_area']:.6f}, "
            f"building_overlap={item['building_overlap_area']:.6f}, "
            f"utility_well_overlap={item['utility_well_overlap_area']:.6f}, "
            f"heat_chamber_overlap={item['heat_chamber_overlap_area']:.6f}, "
            "outside_confirmed_plantable="
            f"{item['outside_confirmed_plantable_area']:.6f}"
        )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
