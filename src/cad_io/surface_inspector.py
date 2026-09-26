"""Inspect all closed HATCH/LWPOLYLINE surfaces inside the work boundary."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import matplotlib
from shapely.errors import GEOSException

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping
from shapely.ops import polygonize, unary_union
from shapely.plotting import plot_polygon
from shapely.validation import make_valid

from ..geometry.parts import polygon_parts

from ..geometry.constraint_builder import read_object_geometry
from ..geometry.normalizer import primitive_to_geometry


Polygonal = Polygon | MultiPolygon

DXF_COLORS = [
    1, 2, 3, 4, 5, 6, 30, 40, 50, 90, 110, 130, 140, 160, 180, 200, 210, 220,
]

PLANTABLE_WORDS = (
    "газон", "грунт", "растител", "озелен", "цветник", "клумб", "почв",
)
HARD_SURFACE_WORDS = (
    "асфальт", "покрыт", "тротуар", "трот", "плитк", "проезж", "дорог", "проезд",
    "пч", "щебень", "лестниц", "пандус",
)
REFERENCE_WORDS = (
    "граница работ", "красные линии", "борт", "бордюр", "оград",
)


def readable_layer_name(value: str) -> str:
    """Repair a common UTF-8-as-CP1251 mojibake without changing valid text."""
    if "Р" not in value and "С" not in value:
        return value
    try:
        repaired = value.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired if repaired else value


def record_polygon(record: dict[str, Any], curve_tolerance: float) -> Polygonal | None:
    raw = record.get("geometry") or {}
    kind = raw.get("kind")
    if kind == "polyline" and not raw.get("closed"):
        return None

    geometry = primitive_to_geometry(record, curve_tolerance)
    if geometry is None or geometry.is_empty:
        return None

    if isinstance(geometry, Polygon | MultiPolygon):
        polygonal = geometry
    else:
        faces = list(polygonize(geometry))
        if not faces:
            return None
        # Surface extraction can contain self-intersecting CAD contours.  A
        # single malformed hatch must not abort inspection of the whole file.
        repaired_faces = [make_valid(face) for face in faces if not face.is_empty]
        try:
            polygonal = unary_union(repaired_faces)
        except GEOSException:
            # ``buffer(0)`` is a conservative last-resort repair for the
            # individual polygonized faces and keeps the source record local.
            fallback = [face.buffer(0) for face in repaired_faces if not face.is_empty]
            if not fallback:
                return None
            try:
                polygonal = unary_union(fallback)
            except GEOSException:
                return None

    polygonal = make_valid(polygonal)
    parts = list(polygon_parts(polygonal))
    return unary_union(parts) if parts else None


def suggested_class(layer_name: str) -> str:
    normalized = layer_name.casefold()
    if "граница покрыт" in normalized:
        return "reference_geometry"
    if any(word in normalized for word in REFERENCE_WORDS):
        return "reference_geometry"
    plantable = any(word in normalized for word in PLANTABLE_WORDS)
    hard_surface = any(word in normalized for word in HARD_SURFACE_WORDS)
    if plantable and hard_surface:
        return "ambiguous"
    if plantable:
        return "plantable_candidate"
    if hard_surface:
        return "hard_surface_candidate"
    return "unknown"


def add_to_dxf(modelspace, geometry: Polygonal, layer: str) -> None:
    for polygon in polygon_parts(geometry):
        modelspace.add_lwpolyline(
            list(polygon.exterior.coords), close=True, dxfattribs={"layer": layer}
        )
        for interior in polygon.interiors:
            modelspace.add_lwpolyline(
                list(interior.coords), close=True, dxfattribs={"layer": layer}
            )


def write_dxf(path: Path, work_boundary: Polygonal, surfaces: list[dict[str, Any]]) -> None:
    document = ezdxf.new("R2018")
    document.header["$INSUNITS"] = 0
    document.layers.add("INSPECT_WORK_BOUNDARY", color=5)
    modelspace = document.modelspace()
    add_to_dxf(modelspace, work_boundary, "INSPECT_WORK_BOUNDARY")

    for index, surface in enumerate(surfaces, start=1):
        layer = f"SURFACE_{index:03d}"
        document.layers.add(layer, color=DXF_COLORS[(index - 1) % len(DXF_COLORS)])
        add_to_dxf(modelspace, surface["geometry"], layer)
        surface["debug_dxf_layer"] = layer
    document.saveas(path)


def write_png(path: Path, work_boundary: Polygonal, surfaces: list[dict[str, Any]], dpi: int) -> None:
    figure, axis = plt.subplots(figsize=(14, 10))
    color_map = plt.get_cmap("tab20")
    for index, surface in enumerate(surfaces):
        plot_polygon(
            surface["geometry"],
            axis,
            add_points=False,
            facecolor=color_map(index % 20),
            edgecolor=color_map(index % 20),
            linewidth=0.35,
            alpha=0.45,
            zorder=1,
        )
    plot_polygon(
        work_boundary,
        axis,
        add_points=False,
        facecolor="none",
        edgecolor="#0D47A1",
        linewidth=2,
        zorder=2,
    )
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("DXF X coordinate")
    axis.set_ylabel("DXF Y coordinate")
    axis.set_title("Closed surface candidates inside the work boundary")
    axis.grid(True, linewidth=0.25, alpha=0.35)
    figure.tight_layout()
    figure.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(figure)


def inspect_surfaces(
    candidates_path: Path,
    normalized_path: Path,
    dxf_output: Path,
    png_output: Path,
    report_output: Path,
    curve_tolerance: float,
    min_area: float,
    dpi: int,
) -> None:
    work_boundary = read_object_geometry(normalized_path, "work_boundary")
    grouped: dict[str, list[Polygon]] = defaultdict(list)
    source_layers: dict[str, set[str]] = defaultdict(set)
    dxf_types: dict[str, Counter[str]] = defaultdict(Counter)
    raw_counts: Counter[str] = Counter()
    skipped_counts: Counter[str] = Counter()

    with candidates_path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            raw_layer = record.get("source_layer", "0")
            layer = readable_layer_name(record.get("source_layer_tail", raw_layer))
            raw_counts[layer] += 1
            polygonal = record_polygon(record, curve_tolerance)
            if polygonal is None:
                skipped_counts[layer] += 1
                continue
            clipped = make_valid(polygonal.intersection(work_boundary))
            for polygon in polygon_parts(clipped):
                if polygon.area >= min_area:
                    grouped[layer].append(polygon)
            source_layers[layer].add(readable_layer_name(raw_layer))
            dxf_types[layer][record.get("dxf_type", "UNKNOWN")] += 1

    surfaces = []
    for layer, polygons_for_layer in grouped.items():
        geometry = make_valid(unary_union(polygons_for_layer))
        parts = list(polygon_parts(geometry))
        if not parts:
            continue
        geometry = unary_union(parts)
        surfaces.append({
            "source_layer_tail": layer,
            "source_layers": sorted(source_layers[layer]),
            "suggested_class": suggested_class(layer),
            "raw_records": raw_counts[layer],
            "skipped_records": skipped_counts[layer],
            "dxf_types": dict(sorted(dxf_types[layer].items())),
            "polygon_count": len(parts),
            "area_in_dxf_square_units": geometry.area,
            "geometry": geometry,
        })
    surfaces.sort(key=lambda item: item["area_in_dxf_square_units"], reverse=True)

    write_dxf(dxf_output, work_boundary, surfaces)
    write_png(png_output, work_boundary, surfaces, dpi)

    report_surfaces = []
    for surface in surfaces:
        report_surfaces.append({
            key: value
            for key, value in surface.items()
            if key != "geometry"
        })
    report = {
        "candidate_input": str(candidates_path),
        "normalized_input": str(normalized_path),
        "coordinate_reference": "local_dxf_coordinates",
        "work_boundary_area_in_dxf_square_units": work_boundary.area,
        "curve_tolerance_in_dxf_units": curve_tolerance,
        "minimum_polygon_area_in_dxf_square_units": min_area,
        "surface_layer_count": len(surfaces),
        "surfaces": report_surfaces,
        "warning": "suggested_class is a name-based heuristic and requires visual confirmation",
    }
    report_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Candidates: {candidates_path}")
    print(f"Work boundary: {normalized_path}")
    print(f"DXF: {dxf_output}")
    print(f"PNG: {png_output}")
    print(f"Report: {report_output}")
    print(f"Surface layers with polygonal area: {len(surfaces)}")
    for surface in surfaces[:15]:
        print(
            f"  {surface['debug_dxf_layer']}: "
            f"{surface['source_layer_tail']} | "
            f"{surface['area_in_dxf_square_units']:.3f} | "
            f"{surface['suggested_class']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect closed DXF surfaces grouped by source layer."
    )
    parser.add_argument("candidate_jsonl", type=Path)
    parser.add_argument("normalized_geojsonl", type=Path)
    parser.add_argument("--dxf-output", type=Path, default=Path("surface_candidates.dxf"))
    parser.add_argument("--png-output", type=Path, default=Path("surface_candidates.png"))
    parser.add_argument("--report", type=Path, default=Path("surface_candidates_report.json"))
    parser.add_argument("--curve-tolerance", type=float, default=0.1)
    parser.add_argument("--min-area", type=float, default=0.01)
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()
    if args.curve_tolerance <= 0 or args.min_area < 0 or args.dpi <= 0:
        raise SystemExit("Tolerance and DPI arguments must be positive")
    try:
        inspect_surfaces(
            args.candidate_jsonl,
            args.normalized_geojsonl,
            args.dxf_output,
            args.png_output,
            args.report,
            args.curve_tolerance,
            args.min_area,
            args.dpi,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Surface inspector error: {error}") from error


if __name__ == "__main__":
    main()
