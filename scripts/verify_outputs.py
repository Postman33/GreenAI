"""Verify spatial invariants of generated constraint and planting-zone files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shapely.geometry import GeometryCollection, shape
from shapely.ops import unary_union


AREA_TOLERANCE = 1e-4


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
    parser.add_argument("--output", type=Path, default=Path("verification_report.json"))
    args = parser.parse_args()

    constraints = read_by_object_type(args.constraint_map)
    zones = read_by_object_type(args.plant_zones)
    _, base = one(constraints, "base_allowed_area")
    _, road = one(constraints, "road_area")
    _, hard_surfaces = one(constraints, "hard_surface_area")
    _, sidewalks = one(constraints, "sidewalk_area")
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
        if outside_confirmed_plantable > AREA_TOLERANCE:
            failures.append(
                f"{plant_type}: zone extends outside confirmed plantable surfaces"
            )

    if not checks:
        failures.append("No plant_allow_zone features found")

    report = {
        "constraint_map": str(args.constraint_map),
        "plant_zones": str(args.plant_zones),
        "area_tolerance": AREA_TOLERANCE,
        "status": "passed" if not failures else "failed",
        "checks": checks,
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
            "outside_confirmed_plantable="
            f"{item['outside_confirmed_plantable_area']:.6f}"
        )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
