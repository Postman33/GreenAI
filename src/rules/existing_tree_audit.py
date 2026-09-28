"""Screen mapped existing trees against the active tree placement setbacks.

This is an inventory review, not a decision to remove existing vegetation.
Only rules with accepted geometry and ``status=applied`` can produce a
measured conflict.  The same rule geometry resolver is used by the planting
point checks.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import Point, box, mapping
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from ..planting.placement_generator import (
    load_geojsonl_by_object_type,
    prepare_sidewalk_for_checks,
    rule_geometry,
)


def _points(geometry: Any) -> Iterable[Point]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "Point":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _points(part)


def _parts(geometry: Any) -> Iterable[Any]:
    if geometry is None or geometry.is_empty:
        return
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _parts(part)
    else:
        yield geometry


def audit_existing_trees(
    normalized: dict[str, Any],
    constraints: dict[str, Any],
    utilities: dict[str, Any],
    zone_report: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return one auditable GeoJSON feature per tree inside the work boundary."""
    units = float(zone_report.get("dxf_units_per_meter", 1.0))
    if not math.isfinite(units) or units <= 0:
        raise ValueError("dxf_units_per_meter must be finite and positive")
    work = normalized.get("work_boundary")
    if work is None or work.is_empty:
        raise ValueError("work_boundary is required for existing tree screening")

    prepare_sidewalk_for_checks(normalized, constraints, zone_report, units)
    tree_report = zone_report.get("plant_types", {}).get("tree", {})
    rules = [
        rule for rule in tree_report.get("rules", [])
        if rule.get("check") == "min_distance"
        and rule.get("target_object_type") != "existing_tree"
    ]
    indexes: dict[str, tuple[STRtree, list[Any]]] = {}
    unavailable: list[str] = []
    for rule in rules:
        code = str(rule.get("rule_code", ""))
        if rule.get("status") != "applied":
            unavailable.append(code)
            continue
        target = str(rule.get("target_object_type", ""))
        if target in indexes:
            continue
        geometry = rule_geometry(target, constraints, normalized, utilities)
        parts = list(_parts(geometry))
        if parts:
            indexes[target] = (STRtree(parts), parts)
        else:
            unavailable.append(code)

    all_trees = sorted(
        {(float(point.x), float(point.y)) for point in _points(normalized.get("existing_tree"))},
        key=lambda xy: (xy[0], xy[1]),
    )
    features: list[dict[str, Any]] = []
    by_target: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for x, y in all_trees:
        point = Point(x, y)
        if not work.covers(point):
            continue
        checks: list[dict[str, Any]] = []
        for rule in rules:
            code = str(rule.get("rule_code", ""))
            target = str(rule.get("target_object_type", ""))
            required = float(rule.get("min_distance_m", 0.0))
            if not math.isfinite(required) or required < 0:
                raise ValueError(f"{code}: invalid min_distance_m")
            check = {
                "code": code,
                "target": target,
                "required_distance_m": required,
                "actual_distance_m": None,
                "norm_reference": rule.get("norm_reference"),
                "norm_document": rule.get("norm_document"),
                "rule_kind": rule.get("rule_kind", "minimum_distance"),
                "measurement_limit": rule.get("measurement_limit"),
                "geometry_source": rule.get("geometry_source"),
            }
            if rule.get("status") != "applied" or target not in indexes:
                check["status"] = "unavailable"
                checks.append(check)
                continue
            tree, parts = indexes[target]
            radius = required * units
            candidates = tree.query(box(x - radius, y - radius, x + radius, y + radius))
            if len(candidates):
                nearest = min(
                    (point.distance(parts[int(index)]), int(index))
                    for index in candidates
                )
                actual = nearest[0] / units
                check["actual_distance_m"] = actual
                if actual + 1e-7 < required:
                    nearest_on_target = nearest_points(point, parts[nearest[1]])[1]
                    check["status"] = "conflict"
                    check["nearest_target_point"] = [nearest_on_target.x, nearest_on_target.y]
                    by_rule[code] += 1
                else:
                    check["status"] = "passed"
            else:
                # The spatial query proves the minimum distance without
                # calculating an unrelated, potentially remote nearest point.
                check["status"] = "passed"
            checks.append(check)
        by_target.update({
            str(check["target"])
            for check in checks if check["status"] == "conflict"
        })
        status = (
            "conflict" if any(item["status"] == "conflict" for item in checks)
            else "incomplete" if not checks or any(item["status"] == "unavailable" for item in checks)
            else "clear"
        )
        statuses[status] += 1
        tree_id = f"ET-{len(features) + 1:04d}"
        features.append({
            "type": "Feature",
            "id": tree_id,
            "geometry": mapping(point),
            "properties": {
                "object_type": "existing_tree_rule_screening",
                "existing_tree_id": tree_id,
                "status": status,
                "conflict_count": sum(item["status"] == "conflict" for item in checks),
                "checks": checks,
                "coordinate_reference": "local_dxf_coordinates",
                "assessment_scope": "new_planting_setback_screening_requires_field_review",
            },
        })
    report = {
        "status": "completed",
        "source_tree_count": len(all_trees),
        "screened_tree_count": len(features),
        "outside_work_boundary_count": len(all_trees) - len(features),
        "conflict_tree_count": statuses["conflict"],
        "clear_tree_count": statuses["clear"],
        "incomplete_tree_count": statuses["incomplete"],
        "conflicts_by_target": dict(sorted(by_target.items())),
        "conflicts_by_rule": dict(sorted(by_rule.items())),
        "unavailable_rule_codes": sorted(set(unavailable)),
        "unchecked_utility_object_types": sorted(
            str(item) for item in tree_report.get("unchecked_utility_object_types", [])
        ),
        "dxf_units_per_meter": units,
        "assessment_scope": "Potential conflicts with active new-tree setbacks; not a removal decision.",
    }
    return features, report


def build_audit(
    normalized_path: Path,
    constraints_path: Path,
    utilities_path: Path,
    zone_report_path: Path,
    output_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    normalized = load_geojsonl_by_object_type(normalized_path)
    constraints = load_geojsonl_by_object_type(constraints_path)
    utilities = load_geojsonl_by_object_type(utilities_path)
    zone_report = json.loads(zone_report_path.read_text(encoding="utf-8-sig"))
    features, report = audit_existing_trees(normalized, constraints, utilities, zone_report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for feature in features:
            output.write(json.dumps(feature, ensure_ascii=False) + "\n")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Screen existing trees against active tree setbacks")
    parser.add_argument("normalized", type=Path)
    parser.add_argument("constraints", type=Path)
    parser.add_argument("utilities", type=Path)
    parser.add_argument("zone_report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = build_audit(
        args.normalized, args.constraints, args.utilities, args.zone_report,
        args.output, args.report,
    )
    print(f"Existing trees screened: {report['screened_tree_count']}")
    print(f"Potential conflicts: {report['conflict_tree_count']}")
    print(f"Audit: {args.output}")
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()
