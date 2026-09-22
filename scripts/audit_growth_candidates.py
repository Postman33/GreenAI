"""Summarize data-backed opportunities for improving the planting pipeline."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any


LAYER_FAMILIES = {
    "wells": ("Колодц",),
    "manholes": ("Люк",),
    "poles": ("Опор",),
    "traffic_lights": ("Светофор",),
    "lamps": ("Фонар",),
    "posts": ("Столб",),
    "hydrants": ("Гидран",),
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_inventory(path: Path) -> dict[str, Any]:
    inventory = read_json(path)
    layers = inventory.get("layers", [])
    families: dict[str, Any] = {}
    for family, terms in LAYER_FAMILIES.items():
        matched = [
            item
            for item in layers
            if any(
                term.casefold() in str(item.get("layer", "")).casefold()
                for term in terms
            )
        ]
        dxf_types: Counter[str] = Counter()
        for item in matched:
            dxf_types.update(item.get("types", {}))
        families[family] = {
            "matched_layer_count": len(matched),
            "entity_count": sum(int(item.get("total", 0)) for item in matched),
            "dxf_types": dict(sorted(dxf_types.items())),
            "sample_layers": [item.get("layer") for item in matched[:8]],
        }
    return {
        "inventory_input_count": inventory.get("input"),
        "inventory_entity_count": inventory.get("entity_count"),
        "inventory_layer_count": inventory.get("layer_count"),
        "candidate_layer_families": families,
    }


def arc_sweep(raw: dict[str, Any]) -> float:
    return (float(raw.get("end_angle", 0)) - float(raw.get("start_angle", 0))) % 360


def summarize_extracted(path: Path) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    dxf_types: dict[str, Counter[str]] = defaultdict(Counter)
    layers: dict[str, Counter[str]] = defaultdict(Counter)
    well_radii: list[float] = []
    well_full_circle_arcs = 0
    well_circle_keys: set[tuple[float, float, float]] = set()
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            object_type = str(record.get("object_type"))
            counts[object_type] += 1
            dxf_types[object_type][str(record.get("dxf_type"))] += 1
            layer = record.get("source_layer_tail") or record.get("source_layer")
            layers[object_type][str(layer)] += 1
            if object_type != "utility_well":
                continue
            raw = record.get("geometry") or {}
            if raw.get("kind") not in {"arc", "circle"}:
                continue
            radius = float(raw.get("radius", 0))
            center = raw.get("center")
            if radius <= 0 or not center:
                continue
            well_radii.append(radius)
            is_full = raw.get("kind") == "circle" or arc_sweep(raw) >= 350
            if is_full:
                well_full_circle_arcs += 1
                well_circle_keys.add(
                    (round(float(center[0]), 6), round(float(center[1]), 6), round(radius, 6))
                )

    semantic_types = {
        object_type: {
            "record_count": count,
            "dxf_types": dict(sorted(dxf_types[object_type].items())),
            "top_layers": layers[object_type].most_common(8),
        }
        for object_type, count in sorted(counts.items())
    }
    well_summary = {
        "record_count": counts["utility_well"],
        "full_circle_primitive_count": well_full_circle_arcs,
        "unique_full_circle_count": len(well_circle_keys),
        "radius_min": min(well_radii) if well_radii else None,
        "radius_median": median(well_radii) if well_radii else None,
        "radius_max": max(well_radii) if well_radii else None,
        "stable_half_unit_radius": bool(
            well_radii and max(abs(value - 0.5) for value in well_radii) < 0.001
        ),
    }
    return {"semantic_types": semantic_types, "utility_well_geometry": well_summary}


def summarize_pipeline(
    normalization_report: Path,
    constraint_report: Path,
    model_report: Path,
) -> dict[str, Any]:
    normalization = read_json(normalization_report)
    constraints = read_json(constraint_report)
    models = read_json(model_report)
    surface = constraints.get("surface_detection", {})
    network_metrics = {}
    for network, item in models.get("networks", {}).items():
        accepted = item.get("accepted_validation", {})
        network_metrics[network] = {
            "training_primitive_count": item.get("primitive_count"),
            "training_tile_count": item.get("training_tile_count"),
            "validation_tile_count": item.get("validation_tile_count"),
            "validation_precision": accepted.get("precision"),
            "validation_recall": accepted.get("recall"),
            "validation_f1": accepted.get("f1"),
        }
    areas = constraints.get("areas_in_dxf_square_units", {})
    removed_by_wells = float(
        areas.get("incremental_utility_well_exclusion_inside_planting_candidate", 0)
    )
    return {
        "normalization_object_types": normalization.get("object_types", {}),
        "surface_records_by_class": surface.get("records_by_class", {}),
        "skipped_hard_surface_records_without_polygon": surface.get(
            "skipped_hard_surface_records_without_polygon"
        ),
        "constraint_areas": areas,
        "utility_well_area_removed_from_previous_base_estimate": removed_by_wells,
        "network_model_metrics": network_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=Path("dxf_layer_inventory.json"))
    parser.add_argument(
        "--extracted", type=Path, default=Path("output/extracted_objects.jsonl")
    )
    parser.add_argument(
        "--normalization-report",
        type=Path,
        default=Path("output/normalization_report.json"),
    )
    parser.add_argument(
        "--constraint-report",
        type=Path,
        default=Path("output/constraint_report.json"),
    )
    parser.add_argument(
        "--model-report",
        type=Path,
        default=Path("models/utility_detector/latest/training_report.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("output/growth_data_audit.json")
    )
    args = parser.parse_args()

    report = {
        "inventory": summarize_inventory(args.inventory),
        "current_drawing": summarize_extracted(args.extracted),
        "pipeline": summarize_pipeline(
            args.normalization_report,
            args.constraint_report,
            args.model_report,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Growth data audit: {args.output}")
    print(
        "Well footprints: "
        f"{report['current_drawing']['utility_well_geometry']['unique_full_circle_count']}"
    )
    print(
        "Base area removed by physical well footprints: "
        f"{report['pipeline']['utility_well_area_removed_from_previous_base_estimate']:.3f} "
        "square DXF units"
    )


if __name__ == "__main__":
    main()
