"""Conservatively separate utility routes from CAD annotation geometry.

The input is raw JSONL produced by ``dxf_extract_go.exe`` or ``src/loader.py``.
The cleaner intentionally stays outside the planting pipeline until its output
is visually confirmed. It writes accepted and rejected linework, a JSON report,
and optional diagnostic DXF/PNG files.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import matplotlib
import yaml
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
    shape,
)
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.plotting import plot_line, plot_polygon


WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from src.normalizer import primitive_to_geometry  # noqa: E402


@dataclass(frozen=True)
class Primitive:
    geometry: LineString
    source_layer: str
    dxf_type: str


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def line_parts(geometry: Any) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        if not geometry.is_empty and len(geometry.coords) >= 2:
            yield geometry
    elif isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            yield from line_parts(part)
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from line_parts(part)


def polygon_parts(geometry: Any) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def merge_lines(lines: Iterable[LineString]) -> LineString | MultiLineString:
    materialized = [line for line in lines if not line.is_empty]
    if not materialized:
        return MultiLineString([])
    combined = unary_union(materialized)
    try:
        merged = linemerge(combined)
    except ValueError:
        merged = combined
    parts = list(line_parts(merged))
    if not parts:
        return MultiLineString([])
    return parts[0] if len(parts) == 1 else MultiLineString(parts)


def read_work_boundary(path: Path | None, margin: float):
    if path is None:
        return None
    polygons: list[Polygon] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            if feature.get("properties", {}).get("object_type") != "work_boundary":
                continue
            geometry = make_valid(shape(feature["geometry"]))
            polygons.extend(polygon_parts(geometry))
    if not polygons:
        raise ValueError(f"No work_boundary found in {path}")
    return unary_union(polygons).buffer(margin)


def scaled_rules(config: dict[str, Any], object_type: str, scale: float) -> dict[str, Any]:
    rules = dict(config.get("defaults", {}))
    rules.update(config.get("network_types", {}).get(object_type, {}))
    for key in tuple(rules):
        if key.endswith("_m"):
            rules[key.removesuffix("_m")] = float(rules.pop(key)) * scale
    return rules


def load_primitives(
    input_path: Path,
    supported_types: set[str],
    scope: Any,
    curve_tolerance: float,
) -> tuple[dict[str, list[Primitive]], Counter[str], Counter[str]]:
    grouped: dict[str, list[Primitive]] = defaultdict(list)
    outside_scope: Counter[str] = Counter()
    invalid: Counter[str] = Counter()
    with input_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            object_type = record.get("object_type")
            if object_type not in supported_types:
                continue
            try:
                geometry = primitive_to_geometry(record, curve_tolerance)
            except (KeyError, TypeError, ValueError):
                invalid[object_type] += 1
                continue
            if geometry is None or geometry.is_empty:
                invalid[object_type] += 1
                continue
            for part in line_parts(geometry):
                candidate = part
                if scope is not None:
                    if not candidate.intersects(scope):
                        outside_scope[object_type] += 1
                        continue
                    candidate = candidate.intersection(scope)
                parts = list(line_parts(candidate))
                if not parts:
                    outside_scope[object_type] += 1
                    continue
                for clipped in parts:
                    grouped[object_type].append(
                        Primitive(
                            geometry=clipped,
                            source_layer=str(record.get("source_layer", "")),
                            dxf_type=str(record.get("dxf_type", "")),
                        )
                    )
    return grouped, outside_scope, invalid


def span(geometry: LineString) -> float:
    minx, miny, maxx, maxy = geometry.bounds
    return math.hypot(maxx - minx, maxy - miny)


def is_compact_closed_symbol(geometry: LineString, max_span: float) -> bool:
    if not geometry.is_ring:
        return False
    return span(geometry) <= max_span


def connected_components(lines: list[LineString], tolerance: float) -> list[list[int]]:
    if not lines:
        return []
    tree = STRtree(lines)
    groups = UnionFind(len(lines))
    for index, line in enumerate(lines):
        search = line.buffer(tolerance, cap_style=2)
        for other in tree.query(search, predicate="intersects"):
            other_index = int(other)
            if other_index > index and lines_share_endpoint(
                line, lines[other_index], tolerance
            ):
                groups.union(index, other_index)
    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(lines)):
        components[groups.find(index)].append(index)
    return list(components.values())


def endpoints(geometry: LineString) -> tuple[Point, Point]:
    coordinates = list(geometry.coords)
    return Point(coordinates[0]), Point(coordinates[-1])


def lines_share_endpoint(
    left: LineString, right: LineString, tolerance: float
) -> bool:
    """Connect CAD strokes through their ends, not through a mid-line crossing."""
    left_start, left_end = endpoints(left)
    right_start, right_end = endpoints(right)
    return min(
        left_start.distance(right),
        left_end.distance(right),
        right_start.distance(left),
        right_end.distance(left),
    ) <= tolerance


def straightness(geometry: LineString) -> float:
    if geometry.length <= 0:
        return 0.0
    start, end = endpoints(geometry)
    return start.distance(end) / geometry.length


def clustered_endpoint_nodes(
    lines: list[LineString], tolerance: float
) -> tuple[list[tuple[int, int]], dict[int, set[int]]]:
    """Cluster nearby line endpoints and return graph incidence."""
    points: list[Point] = []
    for line in lines:
        points.extend(endpoints(line))
    if not points:
        return [], {}
    tree = STRtree(points)
    groups = UnionFind(len(points))
    for index, point in enumerate(points):
        for other in tree.query(point.buffer(tolerance), predicate="intersects"):
            other_index = int(other)
            if other_index > index:
                groups.union(index, other_index)
    line_nodes: list[tuple[int, int]] = []
    incident: dict[int, set[int]] = defaultdict(set)
    for line_index in range(len(lines)):
        start_node = groups.find(line_index * 2)
        end_node = groups.find(line_index * 2 + 1)
        line_nodes.append((start_node, end_node))
        incident[start_node].add(line_index)
        incident[end_node].add(line_index)
    return line_nodes, incident


def direction_from_node(
    line: LineString,
    line_nodes: tuple[int, int],
    node: int,
) -> tuple[float, float] | None:
    coordinates = list(line.coords)
    if line_nodes[0] == node:
        origin = coordinates[0]
        candidates = coordinates[1:]
    elif line_nodes[1] == node:
        origin = coordinates[-1]
        candidates = reversed(coordinates[:-1])
    else:
        return None
    for coordinate in candidates:
        dx, dy = coordinate[0] - origin[0], coordinate[1] - origin[1]
        length = math.hypot(dx, dy)
        if length > 1e-9:
            return dx / length, dy / length
    return None


def vector_angle_degrees(
    left: tuple[float, float], right: tuple[float, float]
) -> float:
    dot = max(-1.0, min(1.0, left[0] * right[0] + left[1] * right[1]))
    return math.degrees(math.acos(dot))


def find_arrowhead_lines(
    lines: list[LineString], rules: dict[str, Any]
) -> set[int]:
    """Find compact V-shaped arrowheads among otherwise accepted strokes.

    A route bend has directions pointing broadly away from each other. An
    arrowhead has two similarly sized legs leaving a shared node at an acute
    angle. Requiring a length ratio avoids treating an ordinary short service
    branch beside a long route segment as an arrow.
    """
    if len(lines) < 2:
        return set()
    tolerance = float(rules["connect_tolerance"])
    max_length = float(rules["max_arrow_leg_length"])
    min_ratio = float(rules["min_arrow_leg_length_ratio"])
    min_angle = float(rules["min_arrow_angle_deg"])
    max_angle = float(rules["max_arrow_angle_deg"])
    max_barb_length = float(rules["max_arrow_barb_length"])
    max_shaft_length = float(rules["max_arrow_shaft_length"])
    max_barb_angle = float(rules["max_arrow_barb_angle_deg"])
    line_nodes, incident = clustered_endpoint_nodes(lines, tolerance)
    arrows: set[int] = set()
    v_pairs_by_node: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for node, line_indices in incident.items():
        for left_index, right_index in combinations(sorted(line_indices), 2):
            left, right = lines[left_index], lines[right_index]
            if left.length > max_length or right.length > max_length:
                continue
            length_ratio = min(left.length, right.length) / max(
                left.length, right.length
            )
            if length_ratio < min_ratio:
                continue
            left_direction = direction_from_node(left, line_nodes[left_index], node)
            right_direction = direction_from_node(right, line_nodes[right_index], node)
            if left_direction is None or right_direction is None:
                continue
            angle = vector_angle_degrees(left_direction, right_direction)
            if min_angle <= angle <= max_angle:
                arrows.update((left_index, right_index))
                v_pairs_by_node[node].append((left_index, right_index))

    # A leader arrow often has two short V legs and one longer shaft meeting
    # at the same tip. Once the V is known, reject the compact shaft too.
    for node, pairs in v_pairs_by_node.items():
        for left_index, right_index in pairs:
            left_direction = direction_from_node(
                lines[left_index], line_nodes[left_index], node
            )
            right_direction = direction_from_node(
                lines[right_index], line_nodes[right_index], node
            )
            if left_direction is None or right_direction is None:
                continue
            for shaft_index in incident[node] - {left_index, right_index}:
                shaft = lines[shaft_index]
                if shaft.length > max_shaft_length:
                    continue
                shaft_direction = direction_from_node(
                    shaft, line_nodes[shaft_index], node
                )
                if shaft_direction is None:
                    continue
                if min(
                    vector_angle_degrees(shaft_direction, left_direction),
                    vector_angle_degrees(shaft_direction, right_direction),
                ) <= max_barb_angle:
                    arrows.add(shaft_index)

    # A one-sided leader has a long shaft and a single short diagonal barb.
    # At the arrow tip both vectors point broadly in the same direction; a
    # normal pipe bend points away in substantially different directions.
    for node, line_indices in incident.items():
        for left_index, right_index in combinations(sorted(line_indices), 2):
            left, right = lines[left_index], lines[right_index]
            if left.length <= right.length:
                barb_index, shaft_index = left_index, right_index
            else:
                barb_index, shaft_index = right_index, left_index
            barb, shaft = lines[barb_index], lines[shaft_index]
            if (
                barb.length > max_barb_length
                or shaft.length > max_shaft_length
                or shaft.length <= barb.length
            ):
                continue
            barb_direction = direction_from_node(barb, line_nodes[barb_index], node)
            shaft_direction = direction_from_node(shaft, line_nodes[shaft_index], node)
            if barb_direction is None or shaft_direction is None:
                continue
            if vector_angle_degrees(barb_direction, shaft_direction) <= max_barb_angle:
                arrows.update((barb_index, shaft_index))
    return arrows


def find_high_confidence_lines(
    lines: list[LineString], rules: dict[str, Any]
) -> set[int]:
    """Keep long strokes and end-connected, nearly straight continuations."""
    if not lines:
        return set()
    tolerance = float(rules["connect_tolerance"])
    min_single_length = float(rules["min_high_confidence_single_length"])
    min_continuation_angle = float(rules["min_straight_continuation_angle_deg"])
    line_nodes, incident = clustered_endpoint_nodes(lines, tolerance)
    trusted = (
        {
            index
            for index, line in enumerate(lines)
            if line.length >= min_single_length
        }
        if rules.get("allow_isolated_long_strokes", False)
        else set()
    )
    for node, line_indices in incident.items():
        for left_index, right_index in combinations(sorted(line_indices), 2):
            left_direction = direction_from_node(
                lines[left_index], line_nodes[left_index], node
            )
            right_direction = direction_from_node(
                lines[right_index], line_nodes[right_index], node
            )
            if left_direction is None or right_direction is None:
                continue
            if (
                vector_angle_degrees(left_direction, right_direction)
                >= min_continuation_angle
            ):
                trusted.update((left_index, right_index))
    return trusted


def endpoint_touches(geometry: LineString, target: Any, tolerance: float) -> bool:
    coordinates = list(geometry.coords)
    return (
        Point(coordinates[0]).distance(target) <= tolerance
        and Point(coordinates[-1]).distance(target) <= tolerance
    )


def clean_type(
    primitives: list[Primitive], rules: dict[str, Any]
) -> tuple[dict[str, list[LineString]], dict[str, Any]]:
    buckets: dict[str, list[LineString]] = defaultdict(list)
    min_fragment = float(rules["min_fragment_length"])
    min_seed = float(rules["min_seed_length"])
    min_seed_straightness = float(rules["min_seed_straightness"])
    min_component_length = float(rules["min_component_length"])
    min_component_span = float(rules["min_component_span"])
    connect_tolerance = float(rules["connect_tolerance"])
    max_closed_span = float(rules["max_compact_closed_span"])
    max_connector_length = float(rules["max_connector_length"])

    eligible: list[Primitive] = []
    for primitive in primitives:
        geometry = primitive.geometry
        if geometry.length < min_fragment:
            buckets["too_short"].append(geometry)
        elif is_compact_closed_symbol(geometry, max_closed_span):
            buckets["compact_closed_symbol"].append(geometry)
        else:
            eligible.append(primitive)

    seed_primitives = [
        item
        for item in eligible
        if item.geometry.length >= min_seed
        and straightness(item.geometry) >= min_seed_straightness
    ]
    seed_ids = {id(item) for item in seed_primitives}
    seed_lines = [item.geometry for item in seed_primitives]
    accepted_seed_indices: set[int] = set()
    component_reports: list[dict[str, Any]] = []
    for component_index, indices in enumerate(
        connected_components(seed_lines, connect_tolerance), start=1
    ):
        component_lines = [seed_lines[index] for index in indices]
        component_geometry = merge_lines(component_lines)
        total_length = sum(line.length for line in component_lines)
        component_span = span(LineString([
            (component_geometry.bounds[0], component_geometry.bounds[1]),
            (component_geometry.bounds[2], component_geometry.bounds[3]),
        ]))
        accepted = (
            total_length >= min_component_length
            and component_span >= min_component_span
        )
        if accepted:
            accepted_seed_indices.update(indices)
        else:
            buckets["non_route_component"].extend(component_lines)
        component_reports.append({
            "component_index": component_index,
            "primitive_count": len(indices),
            "total_length_in_dxf_units": total_length,
            "span_in_dxf_units": component_span,
            "accepted": accepted,
        })

    accepted_backbone = [
        seed_lines[index] for index in sorted(accepted_seed_indices)
    ]
    accepted_seed_ids = {
        id(seed_primitives[index]) for index in accepted_seed_indices
    }
    backbone_geometry = merge_lines(accepted_backbone)
    connectors: list[LineString] = []
    for primitive in eligible:
        if id(primitive) in accepted_seed_ids:
            continue
        geometry = primitive.geometry
        if id(primitive) in seed_ids:
            # It was a seed in a rejected, insufficiently long component.
            continue
        if (
            not backbone_geometry.is_empty
            and geometry.length <= max_connector_length
            and endpoint_touches(geometry, backbone_geometry, connect_tolerance)
        ):
            connectors.append(geometry)
        elif geometry.length >= min_seed:
            buckets["curved_or_symbol_seed"].append(geometry)
        else:
            buckets["disconnected_short_fragment"].append(geometry)

    accepted_with_reason = [
        *(('accepted_backbone', line) for line in accepted_backbone),
        *(('accepted_connector', line) for line in connectors),
    ]
    arrow_indices = find_arrowhead_lines(
        [line for _, line in accepted_with_reason], rules
    )
    survivor_indices = [
        index for index in range(len(accepted_with_reason)) if index not in arrow_indices
    ]
    high_confidence_local = find_high_confidence_lines(
        [accepted_with_reason[index][1] for index in survivor_indices], rules
    )
    high_confidence_indices = {
        survivor_indices[index] for index in high_confidence_local
    }
    accepted: list[LineString] = []
    for index, (reason, line) in enumerate(accepted_with_reason):
        if index in arrow_indices:
            buckets["arrow_or_leader"].append(line)
        elif index in high_confidence_indices:
            buckets[reason].append(line)
            accepted.append(line)
        else:
            buckets["review_ambiguous_branch"].append(line)
    review = [
        geometry
        for reason, geometries in buckets.items()
        if reason_decision(reason) == "manual_review"
        for geometry in geometries
    ]
    rejected = [
        geometry
        for reason, geometries in buckets.items()
        if reason_decision(reason) == "rejected"
        for geometry in geometries
    ]
    source_length = sum(item.geometry.length for item in primitives)
    accepted_length = sum(item.length for item in accepted)
    report = {
        "status": "heuristic_requires_visual_review",
        "source_primitive_count": len(primitives),
        "accepted_primitive_count": len(accepted),
        "manual_review_primitive_count": len(review),
        "rejected_primitive_count": len(rejected),
        "source_length_in_dxf_units": source_length,
        "accepted_length_in_dxf_units": accepted_length,
        "accepted_length_ratio": accepted_length / source_length if source_length else 0.0,
        "counts_by_reason": {
            reason: len(geometries) for reason, geometries in sorted(buckets.items())
        },
        "component_count": len(component_reports),
        "accepted_component_count": sum(item["accepted"] for item in component_reports),
        "components": component_reports,
    }
    return buckets, report


def feature(object_type: str, decision: str, reason: str, geometry: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "object_type": object_type,
            "decision": decision,
            "reason": reason,
            "status": "heuristic_requires_visual_review",
            "coordinate_reference": "local_dxf_coordinates",
        },
        "geometry": mapping(geometry),
    }


def reason_decision(reason: str) -> str:
    if reason.startswith("accepted_"):
        return "accepted"
    if reason.startswith("review_"):
        return "manual_review"
    return "rejected"


def write_geojsonl(
    path: Path,
    results: dict[str, dict[str, list[LineString]]],
    requested_decision: str,
) -> None:
    with path.open("w", encoding="utf-8") as destination:
        for object_type in sorted(results):
            for reason, geometries in sorted(results[object_type].items()):
                decision = reason_decision(reason)
                if decision != requested_decision or not geometries:
                    continue
                geometry = merge_lines(geometries)
                destination.write(json.dumps(feature(
                    object_type,
                    decision,
                    reason,
                    geometry,
                ), ensure_ascii=False) + "\n")


def safe_layer_name(prefix: str, object_type: str) -> str:
    return f"{prefix}_{object_type}".upper()[:255]


def write_debug_dxf(
    path: Path,
    results: dict[str, dict[str, list[LineString]]],
    boundary: Any,
) -> Path:
    document = ezdxf.new("R2018")
    modelspace = document.modelspace()
    if boundary is not None:
        document.layers.add("CLEAN_WORK_SCOPE", color=5)
        for polygon in polygon_parts(boundary):
            modelspace.add_lwpolyline(
                list(polygon.exterior.coords),
                close=True,
                dxfattribs={"layer": "CLEAN_WORK_SCOPE"},
            )
    for object_type, buckets in sorted(results.items()):
        clean_layer = safe_layer_name("CLEAN", object_type)
        review_layer = safe_layer_name("REVIEW", object_type)
        reject_layer = safe_layer_name("REJECT", object_type)
        document.layers.add(clean_layer, color=3)
        document.layers.add(review_layer, color=2)
        document.layers.add(reject_layer, color=1)
        for reason, geometries in buckets.items():
            decision = reason_decision(reason)
            layer = {
                "accepted": clean_layer,
                "manual_review": review_layer,
                "rejected": reject_layer,
            }[decision]
            for line in geometries:
                coordinates = list(line.coords)
                if len(coordinates) >= 2:
                    modelspace.add_lwpolyline(coordinates, dxfattribs={"layer": layer})
    candidates = [path, *(
        path.with_name(f"{path.stem}_v{version}{path.suffix}")
        for version in range(2, 100)
    )]
    last_error: PermissionError | None = None
    for candidate in candidates:
        try:
            document.saveas(candidate)
            if candidate != path:
                print(f"Warning: {path} is locked; debug DXF written to {candidate}")
            return candidate
        except PermissionError as error:
            last_error = error
    assert last_error is not None
    raise last_error


def write_debug_png(
    path: Path,
    results: dict[str, dict[str, list[LineString]]],
    boundary: Any,
) -> None:
    columns = 2
    rows = math.ceil(len(results) / columns)
    figure, axes = plt.subplots(rows, columns, figsize=(12, 6 * rows), dpi=150)
    axes_list = list(getattr(axes, "flat", [axes]))
    for axis, (object_type, buckets) in zip(axes_list, sorted(results.items())):
        if boundary is not None:
            plot_polygon(
                boundary,
                axis,
                add_points=False,
                facecolor="white",
                edgecolor="#455A64",
                linewidth=0.6,
                alpha=0.12,
            )
        rejected = merge_lines(
            geometry
            for reason, geometries in buckets.items()
            if reason_decision(reason) == "rejected"
            for geometry in geometries
        )
        review = merge_lines(
            geometry
            for reason, geometries in buckets.items()
            if reason_decision(reason) == "manual_review"
            for geometry in geometries
        )
        accepted = merge_lines(
            geometry
            for reason, geometries in buckets.items()
            if reason.startswith("accepted_")
            for geometry in geometries
        )
        if not rejected.is_empty:
            plot_line(rejected, axis, add_points=False, color="#EF5350", linewidth=0.25, alpha=0.25)
        if not review.is_empty:
            plot_line(review, axis, add_points=False, color="#F9A825", linewidth=0.45, alpha=0.65)
        if not accepted.is_empty:
            plot_line(accepted, axis, add_points=False, color="#00A152", linewidth=0.8, alpha=0.9)
        axis.set_aspect("equal", adjustable="box")
        axis.grid(True, linewidth=0.25, alpha=0.35)
        axis.set_title(object_type)
    for axis in axes_list[len(results):]:
        axis.set_visible(False)
    figure.suptitle(
        "Utility cleaner: green=accepted, yellow=review, red=rejected",
        y=0.995,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.975))
    figure.savefig(path, bbox_inches="tight")
    plt.close(figure)


def run(args: argparse.Namespace) -> None:
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    network_config = config.get("network_types", {})
    if not network_config:
        raise ValueError("network_types is empty in cleaner config")
    scope = read_work_boundary(
        args.work_boundary,
        args.scope_margin_m * args.dxf_units_per_meter,
    )
    grouped, outside_scope, invalid = load_primitives(
        args.input,
        set(network_config),
        scope,
        args.curve_tolerance,
    )
    results: dict[str, dict[str, list[LineString]]] = {}
    report: dict[str, Any] = {
        "input": str(args.input),
        "work_boundary": str(args.work_boundary) if args.work_boundary else None,
        "coordinate_reference": "local_dxf_coordinates",
        "dxf_units_per_meter": args.dxf_units_per_meter,
        "scope_margin_m": args.scope_margin_m,
        "status": "heuristic_requires_visual_review",
        "network_types": {},
        "warnings": [],
    }
    for object_type in sorted(network_config):
        rules = scaled_rules(config, object_type, args.dxf_units_per_meter)
        primitives = grouped.get(object_type, [])
        if not rules.get("enabled", True):
            reason = str(rules.get("reason", "disabled in config"))
            results[object_type] = {
                "review_unsupported": [item.geometry for item in primitives]
            }
            report["network_types"][object_type] = {
                "status": "unsupported_manual_review",
                "reason": reason,
                "source_primitive_count_in_scope": len(primitives),
                "outside_scope_count": outside_scope[object_type],
                "invalid_geometry_count": invalid[object_type],
            }
            report["warnings"].append(f"{object_type}: {reason}")
            continue
        buckets, type_report = clean_type(primitives, rules)
        type_report.update({
            "outside_scope_count": outside_scope[object_type],
            "invalid_geometry_count": invalid[object_type],
            "effective_rules_in_dxf_units": {
                key: value
                for key, value in rules.items()
                if key not in {"enabled", "reason"}
            },
        })
        results[object_type] = buckets
        report["network_types"][object_type] = type_report

    for path in (
        args.output,
        args.review_output,
        args.rejected_output,
        args.report,
        args.debug_dxf,
        args.debug_png,
    ):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
    write_geojsonl(args.output, results, requested_decision="accepted")
    write_geojsonl(args.review_output, results, requested_decision="manual_review")
    write_geojsonl(args.rejected_output, results, requested_decision="rejected")
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    actual_debug_dxf = None
    if args.debug_dxf:
        actual_debug_dxf = write_debug_dxf(args.debug_dxf, results, scope)
    if args.debug_png:
        write_debug_png(args.debug_png, results, scope)

    print(f"Input: {args.input}")
    print(f"Cleaned: {args.output}")
    print(f"Manual review: {args.review_output}")
    print(f"Rejected: {args.rejected_output}")
    print(f"Report: {args.report}")
    if actual_debug_dxf:
        print(f"Debug DXF: {actual_debug_dxf}")
    for object_type, item in report["network_types"].items():
        if item["status"] == "unsupported_manual_review":
            print(f"  {object_type}: unsupported, manual review")
        else:
            print(
                f"  {object_type}: accepted {item['accepted_primitive_count']} / "
                f"{item['source_primitive_count']} primitives, "
                f"length ratio {item['accepted_length_ratio']:.1%}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="raw extracted_objects.jsonl")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
    )
    parser.add_argument(
        "--work-boundary",
        type=Path,
        help="normalized GeoJSONL containing work_boundary; enables spatial clipping",
    )
    parser.add_argument("--output", type=Path, default=Path("cleaned_utilities.geojsonl"))
    parser.add_argument(
        "--review-output",
        type=Path,
        default=Path("review_utility_graphics.geojsonl"),
    )
    parser.add_argument(
        "--rejected-output",
        type=Path,
        default=Path("rejected_utility_graphics.geojsonl"),
    )
    parser.add_argument("--report", type=Path, default=Path("utility_cleaning_report.json"))
    parser.add_argument("--debug-dxf", type=Path)
    parser.add_argument("--debug-png", type=Path)
    parser.add_argument("--dxf-units-per-meter", type=float, default=1.0)
    parser.add_argument("--scope-margin-m", type=float, default=20.0)
    parser.add_argument("--curve-tolerance", type=float, default=0.2)
    args = parser.parse_args()
    if args.dxf_units_per_meter <= 0:
        parser.error("--dxf-units-per-meter must be greater than zero")
    if args.scope_margin_m < 0 or args.curve_tolerance <= 0:
        parser.error("scope margin must be non-negative and curve tolerance positive")
    return args


if __name__ == "__main__":
    run(parse_args())
