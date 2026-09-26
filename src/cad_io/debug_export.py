"""Test a preliminary planting-area hypothesis and export it to DXF/PNG.

Hypothesis under test::

    base_allowed_area = work_boundary - hard_surface_area - buildings

The project lawn layer is kept out of that calculation and used only as a
reference for measuring and visualising the result.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.plotting import plot_polygon
from shapely.validation import make_valid

from ..geometry.constraint_builder import read_object_geometry
from .dxf_exporter import add_zone_polygon
from .surface_inspector import (
    polygon_parts,
    readable_layer_name,
    record_polygon,
    suggested_class,
)


Polygonal = Polygon | MultiPolygon
PROJECT_LAWN_LAYER = "ДВ_ГП_П_Газон_Рулонный"


def polygons(geometry: Any) -> Iterable[Polygon]:
    """Yield polygon components and ignore non-polygonal collection members."""
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygons(part)


def as_polygonal(geometry: Any) -> Polygonal:
    """Keep only valid polygonal components of a Shapely result."""
    parts = list(polygons(make_valid(geometry)))
    if not parts:
        return Polygon()
    return unary_union(parts)


def read_surface_hypothesis(
    candidates_path: Path,
    work_boundary: Polygonal,
    curve_tolerance: float,
    min_area: float,
) -> tuple[Polygonal, Polygonal, dict[str, Any]]:
    """Build hard surfaces and the held-out project-lawn reference."""
    hard_parts: list[Polygon] = []
    project_lawn_parts: list[Polygon] = []
    classes: Counter[str] = Counter()
    hard_layers: Counter[str] = Counter()
    project_lawn_records = 0

    with candidates_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid candidate JSON"
                ) from error

            raw_layer = record.get("source_layer", "0")
            layer = readable_layer_name(record.get("source_layer_tail", raw_layer))
            surface_class = suggested_class(layer)
            classes[surface_class] += 1
            is_project_lawn = layer.casefold() == PROJECT_LAWN_LAYER.casefold()
            if surface_class != "hard_surface_candidate" and not is_project_lawn:
                continue

            polygonal = record_polygon(record, curve_tolerance)
            if polygonal is None:
                continue
            clipped = as_polygonal(polygonal.intersection(work_boundary))
            valid_parts = [part for part in polygon_parts(clipped) if part.area >= min_area]
            if is_project_lawn:
                project_lawn_parts.extend(valid_parts)
                project_lawn_records += 1
            elif surface_class == "hard_surface_candidate":
                hard_parts.extend(valid_parts)
                hard_layers[layer] += 1

    hard_surfaces = as_polygonal(unary_union(hard_parts)) if hard_parts else Polygon()
    project_lawn = (
        as_polygonal(unary_union(project_lawn_parts))
        if project_lawn_parts
        else Polygon()
    )
    diagnostics = {
        "classified_records": dict(sorted(classes.items())),
        "hard_surface_layers": dict(sorted(hard_layers.items())),
        "project_lawn_layer": PROJECT_LAWN_LAYER,
        "project_lawn_records": project_lawn_records,
        "ambiguous_surfaces_included": False,
    }
    return hard_surfaces, project_lawn, diagnostics


def add_polygon_geometry(modelspace, geometry: Polygonal, layer: str) -> None:
    """Write polygon rings as closed DXF LWPOLYLINE entities."""
    for polygon in polygons(geometry):
        modelspace.add_lwpolyline(
            list(polygon.exterior.coords), close=True, dxfattribs={"layer": layer}
        )
        for interior in polygon.interiors:
            modelspace.add_lwpolyline(
                list(interior.coords), close=True, dxfattribs={"layer": layer}
            )


def export_dxf(
    output_path: Path,
    work_boundary: Polygonal,
    hard_surfaces: Polygonal,
    base_allowed_area: Polygonal,
    project_lawn: Polygonal,
    buildings: Polygonal,
) -> None:
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 0
    layers = {
        "DEBUG_WORK_BOUNDARY": 5,
        "DEBUG_BASE_ALLOWED": 3,
        "DEBUG_HARD_SURFACES": 1,
        "DEBUG_PROJECT_LAWN_REFERENCE": 6,
        "DEBUG_BUILDINGS": 30,
    }
    for name, color in layers.items():
        document.layers.add(name, color=color)

    modelspace = document.modelspace()
    add_polygon_geometry(modelspace, work_boundary, "DEBUG_WORK_BOUNDARY")
    add_polygon_geometry(modelspace, base_allowed_area, "DEBUG_BASE_ALLOWED")
    add_polygon_geometry(modelspace, hard_surfaces, "DEBUG_HARD_SURFACES")
    add_polygon_geometry(modelspace, project_lawn, "DEBUG_PROJECT_LAWN_REFERENCE")
    for building in polygons(buildings):
        add_zone_polygon(modelspace, building, "DEBUG_BUILDINGS", 30, 0.15)
    document.saveas(output_path)


def plot_if_not_empty(geometry: Polygonal, axis, **style: Any) -> None:
    if not geometry.is_empty:
        plot_polygon(geometry, axis, add_points=False, **style)


def export_png(
    output_path: Path,
    work_boundary: Polygonal,
    hard_surfaces: Polygonal,
    base_allowed_area: Polygonal,
    project_lawn: Polygonal,
    buildings: Polygonal,
    dpi: int,
) -> None:
    figure, axis = plt.subplots(figsize=(14, 10))

    plot_if_not_empty(
        base_allowed_area, axis, facecolor="#81C784", edgecolor="#2E7D32",
        linewidth=0.45, alpha=0.55, zorder=1,
    )
    plot_if_not_empty(
        hard_surfaces, axis, facecolor="#EF5350", edgecolor="#B71C1C",
        linewidth=0.55, alpha=0.8, zorder=2,
    )
    plot_if_not_empty(
        buildings, axis, facecolor="#757575", edgecolor="#424242",
        linewidth=0.45, alpha=0.75, zorder=3,
    )
    plot_if_not_empty(
        project_lawn, axis, facecolor="#CE93D8", edgecolor="#6A1B9A",
        linewidth=1.2, alpha=0.55, zorder=4,
    )
    plot_if_not_empty(
        work_boundary, axis, facecolor="none", edgecolor="#1565C0",
        linewidth=2.0, zorder=5,
    )

    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("DXF X coordinate")
    axis.set_ylabel("DXF Y coordinate")
    axis.set_title("Base allowed area: boundary minus hard surfaces and buildings")
    axis.grid(True, linewidth=0.25, alpha=0.35)
    axis.legend(
        handles=[
            Line2D([0], [0], color="#1565C0", linewidth=2, label="Work boundary"),
            Patch(facecolor="#81C784", edgecolor="#2E7D32", alpha=0.55, label="Base allowed area"),
            Patch(facecolor="#EF5350", edgecolor="#B71C1C", alpha=0.8, label="Hard surfaces"),
            Patch(facecolor="#CE93D8", edgecolor="#6A1B9A", alpha=0.55, label="Project lawn reference"),
            Patch(facecolor="#757575", edgecolor="#424242", alpha=0.75, label="Excluded buildings"),
        ],
        loc="best",
    )
    figure.tight_layout()
    figure.savefig(output_path, dpi=dpi, bbox_inches="tight")
    plt.close(figure)


def build_debug_exports(
    normalized_path: Path,
    candidates_path: Path,
    dxf_output: Path,
    png_output: Path,
    report_output: Path,
    curve_tolerance: float,
    min_area: float,
    dpi: int,
) -> None:
    work_boundary = as_polygonal(read_object_geometry(normalized_path, "work_boundary"))
    all_buildings = as_polygonal(read_object_geometry(normalized_path, "building"))
    building_distance_to_work_boundary = all_buildings.distance(work_boundary)
    buildings = as_polygonal(all_buildings.intersection(work_boundary))
    hard_surfaces, project_lawn, diagnostics = read_surface_hypothesis(
        candidates_path, work_boundary, curve_tolerance, min_area
    )
    absolute_exclusions = as_polygonal(unary_union([hard_surfaces, buildings]))
    base_allowed_area = as_polygonal(work_boundary.difference(absolute_exclusions))
    if base_allowed_area.is_empty:
        raise ValueError(
            "work_boundary - hard_surface_area - buildings produced an empty geometry"
        )

    lawn_overlap = as_polygonal(project_lawn.intersection(base_allowed_area))
    hard_overlap = as_polygonal(project_lawn.intersection(hard_surfaces))
    building_overlap = as_polygonal(project_lawn.intersection(buildings))
    lawn_coverage = lawn_overlap.area / project_lawn.area if project_lawn.area else None
    candidate_precision_hint = (
        lawn_overlap.area / base_allowed_area.area if base_allowed_area.area else None
    )

    export_dxf(
        dxf_output, work_boundary, hard_surfaces, base_allowed_area,
        project_lawn, buildings,
    )
    export_png(
        png_output, work_boundary, hard_surfaces, base_allowed_area,
        project_lawn, buildings, dpi,
    )

    report = {
        "hypothesis": (
            "base_allowed_area = work_boundary - hard_surface_area - buildings"
        ),
        "normalized_input": str(normalized_path),
        "surface_candidate_input": str(candidates_path),
        "coordinate_reference": "local_dxf_coordinates",
        "units_confirmed_as_metres": False,
        "curve_tolerance_in_dxf_units": curve_tolerance,
        "minimum_polygon_area_in_dxf_square_units": min_area,
        "areas_in_dxf_square_units": {
            "work_boundary": work_boundary.area,
            "hard_surfaces": hard_surfaces.area,
            "all_normalized_buildings": all_buildings.area,
            "buildings": buildings.area,
            "base_allowed_area": base_allowed_area.area,
            "project_lawn_reference": project_lawn.area,
            "project_lawn_inside_candidate": lawn_overlap.area,
            "project_lawn_overlapping_hard_surfaces": hard_overlap.area,
            "project_lawn_overlapping_buildings": building_overlap.area,
        },
        "metrics": {
            "project_lawn_coverage_by_candidate": lawn_coverage,
            "candidate_area_covered_by_project_lawn": candidate_precision_hint,
            "building_distance_to_work_boundary_in_dxf_units": (
                building_distance_to_work_boundary
            ),
        },
        "surface_selection": diagnostics,
        "interpretation": (
            "The project lawn is a held-out visual reference. Hard surfaces "
            "and buildings are absolute exclusions. Regulatory buffers are "
            "deferred to the per-plant constraint stage."
        ),
        "warnings": (
            [
                "No normalized building polygon intersects the work boundary; "
                "subtracting buildings therefore has no effect in this sample. "
                "A later setback buffer can still reach into the work area."
            ]
            if buildings.is_empty
            else []
        ),
    }
    report_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Normalized input: {normalized_path}")
    print(f"Surface candidates: {candidates_path}")
    print(f"DXF: {dxf_output}")
    print(f"PNG: {png_output}")
    print(f"Report: {report_output}")
    print(f"Work boundary area: {work_boundary.area:.3f} square DXF units")
    print(f"Hard surface area: {hard_surfaces.area:.3f} square DXF units")
    print(f"Buildings area: {buildings.area:.3f} square DXF units")
    print(f"Base allowed area: {base_allowed_area.area:.3f} square DXF units")
    print(f"Project lawn reference: {project_lawn.area:.3f} square DXF units")
    if lawn_coverage is not None:
        print(f"Project lawn covered by candidate: {lawn_coverage:.2%}")
        print(f"Candidate covered by project lawn: {candidate_precision_hint:.2%}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Test work boundary minus hard surfaces and buildings against "
            "the project lawn."
        )
    )
    parser.add_argument("normalized_geojsonl", type=Path)
    parser.add_argument("surface_candidates_jsonl", type=Path)
    parser.add_argument(
        "--dxf-output", type=Path, default=Path("debug_surface_hypothesis.dxf")
    )
    parser.add_argument(
        "--png-output", type=Path, default=Path("debug_surface_hypothesis.png")
    )
    parser.add_argument(
        "--report-output", type=Path, default=Path("debug_surface_hypothesis.json")
    )
    parser.add_argument("--curve-tolerance", type=float, default=0.1)
    parser.add_argument("--min-area", type=float, default=0.01)
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()
    if args.curve_tolerance <= 0 or args.min_area < 0 or args.dpi <= 0:
        raise SystemExit("Tolerance and DPI arguments must be positive")
    try:
        build_debug_exports(
            args.normalized_geojsonl,
            args.surface_candidates_jsonl,
            args.dxf_output,
            args.png_output,
            args.report_output,
            args.curve_tolerance,
            args.min_area,
            args.dpi,
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Debug export error: {error}") from error


if __name__ == "__main__":
    main()
