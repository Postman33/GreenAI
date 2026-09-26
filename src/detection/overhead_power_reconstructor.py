"""Reconstruct overhead power-line route hypotheses from DXF arrow symbols.

The geodetic drawings used by the project frequently encode an overhead line as
short directional arrows instead of a continuous polyline.  This module finds
the arrow symbols, groups their common tails into network nodes and connects
nodes when the arrows point towards each other.

Reciprocal arrows provide stronger geometric evidence than one-sided arrows,
but the arrow semantics and voltage still require manual review.  The module
therefore publishes reciprocal routes to the accepted utility geometry while
keeping all weaker hypotheses in a separate review file.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import ezdxf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity
from ezdxf.disassemble import recursive_decompose
from shapely.geometry import LineString, mapping


Point = tuple[float, float]

DEBUG_LAYERS = {
    "DEBUG_LEP_NODES": 2,
    "DEBUG_LEP_RECIPROCAL": 3,
    "DEBUG_LEP_ONE_SIDED": 30,
    "DEBUG_LEP_UNMATCHED": 1,
    "DEBUG_LEP_TEXT_LINK": 6,
}

VOLTAGE_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*К\s*В", re.IGNORECASE)
WIRES_RE = re.compile(r"(?<!\d)(\d+(?:\s*\+\s*\d+)?)\s*ПР\.?")


@dataclass(frozen=True)
class Primitive:
    handle: str
    layer: str
    points: tuple[Point, ...]
    length: float


@dataclass(frozen=True)
class Arrow:
    shaft_handle: str
    tail: Point
    tip: Point

    @property
    def direction(self) -> Point:
        dx = self.tip[0] - self.tail[0]
        dy = self.tip[1] - self.tail[1]
        length = math.hypot(dx, dy)
        return dx / length, dy / length


@dataclass(frozen=True)
class Node:
    id: int
    point: Point
    arrow_indices: tuple[int, ...]


@dataclass(frozen=True)
class Route:
    start_node: int
    end_node: int
    confidence: str
    angle_error_deg: float
    lateral_error_dxf_units: float
    distance_dxf_units: float


@dataclass(frozen=True)
class Label:
    handle: str
    point: Point
    text: str
    voltage_kv: float | None
    wires: str | None


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_length(points: Sequence[Point]) -> float:
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def layer_tail(layer: str) -> str:
    """Return the original layer name after an XREF bind prefix."""
    return layer.rsplit("$0$", 1)[-1].casefold()


def is_lep_layer(layer: str) -> bool:
    return layer_tail(layer) in {"лэп".casefold(), "топо_лэп".casefold()}


def entity_points(entity: DXFEntity) -> tuple[Point, ...] | None:
    if entity.dxftype() == "LINE":
        return (
            (float(entity.dxf.start.x), float(entity.dxf.start.y)),
            (float(entity.dxf.end.x), float(entity.dxf.end.y)),
        )
    if entity.dxftype() == "LWPOLYLINE":
        return tuple((float(x), float(y)) for x, y, *_ in entity.get_points())
    return None


def recursive_entities_quietly(document: Drawing) -> Iterable[DXFEntity]:
    """Expand nested INSERTs without flooding the pipeline with copy warnings."""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            yield from recursive_decompose(document.modelspace())


def read_lep_entities(document: Drawing) -> tuple[list[Primitive], list[Label]]:
    primitives: list[Primitive] = []
    labels: list[Label] = []
    # Bound geobases are usually nested INSERTs. recursive_decompose applies
    # every block transform, so route points end up in modelspace coordinates.
    for entity_index, entity in enumerate(recursive_entities_quietly(document)):
        if not is_lep_layer(entity.dxf.layer):
            continue
        handle = str(entity.dxf.get("handle", "") or f"virtual-{entity_index}")
        points = entity_points(entity)
        if points and len(points) >= 2:
            primitives.append(
                Primitive(
                    handle=handle,
                    layer=entity.dxf.layer,
                    points=points,
                    length=polyline_length(points),
                )
            )
            continue
        if entity.dxftype() not in {"TEXT", "MTEXT"}:
            continue
        text = entity.dxf.text if entity.dxftype() == "TEXT" else entity.plain_text()
        insert = entity.dxf.insert
        voltage_match = VOLTAGE_RE.search(text)
        wires_match = WIRES_RE.search(text.upper())
        labels.append(
            Label(
                handle=handle,
                point=(float(insert.x), float(insert.y)),
                text=text.strip(),
                voltage_kv=(
                    float(voltage_match.group(1).replace(",", "."))
                    if voltage_match
                    else None
                ),
                wires=(wires_match.group(1).replace(" ", "") if wires_match else None),
            )
        )
    return primitives, labels


def read_lep_records(path: Path) -> list[Primitive]:
    """Read already transformed LEP primitives from the fast Go extractor."""
    primitives: list[Primitive] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("object_type") != "overhead_power_line":
                continue
            raw = record.get("geometry") or {}
            kind = raw.get("kind")
            if kind == "line":
                points = (tuple(raw["start"][:2]), tuple(raw["end"][:2]))
            elif kind == "polyline":
                points = tuple(tuple(point[:2]) for point in raw.get("points", ()))
            else:
                continue
            if len(points) < 2:
                continue
            try:
                numeric_points = tuple(
                    (float(point[0]), float(point[1])) for point in points
                )
            except (TypeError, ValueError, IndexError) as error:
                raise ValueError(
                    f"Line {line_number}: invalid overhead-power coordinates"
                ) from error
            primitives.append(
                Primitive(
                    handle=str(record.get("handle") or f"record-{line_number}"),
                    layer=str(record.get("source_layer") or "ЛЭП"),
                    points=numeric_points,
                    length=polyline_length(numeric_points),
                )
            )
    return primitives


def _point_key(point: Point, tolerance: float) -> tuple[int, int]:
    return round(point[0] / tolerance), round(point[1] / tolerance)


def find_arrow_tips(
    primitives: Sequence[Primitive],
    endpoint_tolerance: float = 0.04,
    dxf_units_per_meter: float = 1.0,
) -> list[Point]:
    """Find tips of V-shaped and two-piece arrowheads."""
    tips: dict[tuple[int, int], Point] = {}

    # A compact three-vertex V is the common LWPOLYLINE representation.
    for primitive in primitives:
        if (
            len(primitive.points) == 3
            and 0.75 * dxf_units_per_meter
            <= primitive.length
            <= 1.30 * dxf_units_per_meter
        ):
            point = primitive.points[1]
            tips[_point_key(point, endpoint_tolerance)] = point

    # Some drawings store both sides of the V as separate short primitives.
    short_endpoints: dict[tuple[int, int], list[tuple[Point, Point]]] = {}
    for primitive in primitives:
        if (
            len(primitive.points) != 2
            or not 0.30 * dxf_units_per_meter
            <= primitive.length
            <= 0.75 * dxf_units_per_meter
        ):
            continue
        a, b = primitive.points
        short_endpoints.setdefault(_point_key(a, endpoint_tolerance), []).append((a, b))
        short_endpoints.setdefault(_point_key(b, endpoint_tolerance), []).append((b, a))

    for key, incident in short_endpoints.items():
        if len(incident) < 2:
            continue
        # Arrow sides must diverge. Collinear fragments are not an arrowhead.
        found = False
        for index, (origin, first) in enumerate(incident):
            first_angle = math.atan2(first[1] - origin[1], first[0] - origin[0])
            for _, second in incident[index + 1 :]:
                second_angle = math.atan2(second[1] - origin[1], second[0] - origin[0])
                delta = abs(math.degrees(math.atan2(
                    math.sin(first_angle - second_angle),
                    math.cos(first_angle - second_angle),
                )))
                if 20.0 <= delta <= 100.0:
                    tips[key] = origin
                    found = True
                    break
            if found:
                break
    return list(tips.values())


def detect_arrows(
    primitives: Sequence[Primitive],
    endpoint_tolerance: float = 0.04,
    dxf_units_per_meter: float = 1.0,
) -> list[Arrow]:
    tips = find_arrow_tips(
        primitives,
        endpoint_tolerance=endpoint_tolerance,
        dxf_units_per_meter=dxf_units_per_meter,
    )
    arrows: list[Arrow] = []
    used_shafts: set[str] = set()
    for primitive in primitives:
        if (
            len(primitive.points) != 2
            or not 1.40 * dxf_units_per_meter
            <= primitive.length
            <= 3.20 * dxf_units_per_meter
        ):
            continue
        matches: list[tuple[int, float]] = []
        for endpoint_index, endpoint in enumerate(primitive.points):
            nearest = min((distance(endpoint, tip) for tip in tips), default=math.inf)
            if nearest <= endpoint_tolerance:
                matches.append((endpoint_index, nearest))
        if len(matches) != 1 or primitive.handle in used_shafts:
            continue
        tip_index = matches[0][0]
        tip = primitive.points[tip_index]
        tail = primitive.points[1 - tip_index]
        if distance(tail, tip) < 1e-9:
            continue
        arrows.append(Arrow(primitive.handle, tail, tip))
        used_shafts.add(primitive.handle)
    return arrows


def build_nodes(arrows: Sequence[Arrow], tolerance: float = 0.08) -> list[Node]:
    groups: dict[tuple[int, int], list[int]] = {}
    for index, arrow in enumerate(arrows):
        groups.setdefault(_point_key(arrow.tail, tolerance), []).append(index)

    nodes: list[Node] = []
    for indices in groups.values():
        x = sum(arrows[index].tail[0] for index in indices) / len(indices)
        y = sum(arrows[index].tail[1] for index in indices) / len(indices)
        nodes.append(Node(len(nodes), (x, y), tuple(indices)))
    return nodes


def _ray_error(origin: Point, direction: Point, target: Point) -> tuple[float, float, float]:
    vx = target[0] - origin[0]
    vy = target[1] - origin[1]
    along = vx * direction[0] + vy * direction[1]
    lateral = abs(vx * direction[1] - vy * direction[0])
    if along <= 0:
        return math.inf, lateral, math.hypot(vx, vy)
    angle = math.degrees(math.atan2(lateral, along))
    return angle, lateral, math.hypot(vx, vy)


def _best_targets(
    arrows: Sequence[Arrow],
    nodes: Sequence[Node],
    *,
    min_distance: float,
    max_distance: float,
    max_angle_deg: float,
    max_lateral_m: float,
) -> dict[int, tuple[int, float, float, float]]:
    arrow_to_node = {
        arrow_index: node.id for node in nodes for arrow_index in node.arrow_indices
    }
    best: dict[int, tuple[int, float, float, float]] = {}
    for arrow_index, arrow in enumerate(arrows):
        source_node = arrow_to_node[arrow_index]
        candidates: list[tuple[float, int, float, float, float]] = []
        for node in nodes:
            if node.id == source_node:
                continue
            angle, lateral, route_distance = _ray_error(
                arrow.tail, arrow.direction, node.point
            )
            allowed_lateral = max_lateral_m + route_distance * 0.005
            if (
                min_distance <= route_distance <= max_distance
                and angle <= max_angle_deg
                and lateral <= allowed_lateral
            ):
                # Follow the first plausible node along the ray.  Without this,
                # a perfectly collinear node hundreds of metres away can win
                # over the actual next support with a tiny survey deviation.
                score = route_distance + angle * 2.0 + lateral * 5.0
                candidates.append((score, node.id, angle, lateral, route_distance))
        if candidates:
            _, node_id, angle, lateral, route_distance = min(candidates)
            best[arrow_index] = (node_id, angle, lateral, route_distance)
    return best


def reconstruct_routes(
    arrows: Sequence[Arrow],
    nodes: Sequence[Node],
    *,
    min_distance: float = 6.0,
    max_distance: float = 500.0,
    max_angle_deg: float = 7.0,
    max_lateral_m: float = 1.0,
) -> tuple[list[Route], set[int]]:
    """Connect arrow nodes and distinguish reciprocal from one-sided matches."""
    best = _best_targets(
        arrows,
        nodes,
        min_distance=min_distance,
        max_distance=max_distance,
        max_angle_deg=max_angle_deg,
        max_lateral_m=max_lateral_m,
    )
    arrow_to_node = {
        arrow_index: node.id for node in nodes for arrow_index in node.arrow_indices
    }
    routes: dict[tuple[int, int], Route] = {}
    used_arrows: set[int] = set()

    for arrow_index, (target_node, angle, lateral, route_distance) in best.items():
        source_node = arrow_to_node[arrow_index]
        reciprocal_indices = [
            candidate
            for candidate in nodes[target_node].arrow_indices
            if best.get(candidate, (None,))[0] == source_node
        ]
        confidence = "reciprocal" if reciprocal_indices else "one_sided"
        key = tuple(sorted((source_node, target_node)))
        candidate_route = Route(
            start_node=key[0],
            end_node=key[1],
            confidence=confidence,
            angle_error_deg=round(angle, 4),
            lateral_error_dxf_units=round(lateral, 4),
            distance_dxf_units=round(route_distance, 3),
        )
        current = routes.get(key)
        if current is None or (
            current.confidence == "one_sided" and confidence == "reciprocal"
        ) or (
            current.confidence == confidence
            and candidate_route.angle_error_deg < current.angle_error_deg
        ):
            routes[key] = candidate_route
        used_arrows.add(arrow_index)
        used_arrows.update(reciprocal_indices)

    return sorted(routes.values(), key=lambda item: (item.start_node, item.end_node)), used_arrows


def point_segment_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator == 0:
        return distance(point, start)
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator))
    projection = start[0] + t * dx, start[1] + t * dy
    return distance(point, projection)


def associate_labels(
    labels: Sequence[Label],
    routes: Sequence[Route],
    nodes: Sequence[Node],
    max_distance: float = 8.0,
) -> list[dict[str, object]]:
    associations: list[dict[str, object]] = []
    for label in labels:
        candidates = []
        for route_index, route in enumerate(routes):
            start = nodes[route.start_node].point
            end = nodes[route.end_node].point
            candidates.append((point_segment_distance(label.point, start, end), route_index))
        if not candidates:
            continue
        nearest_distance, route_index = min(candidates)
        if nearest_distance <= max_distance:
            associations.append(
                {
                    "label_handle": label.handle,
                    "route_index": route_index,
                    "distance_dxf_units": round(nearest_distance, 3),
                    "text": label.text,
                    "voltage_kv": label.voltage_kv,
                    "wires": label.wires,
                }
            )
    return associations


def route_feature(
    route_index: int,
    route: Route,
    nodes: Sequence[Node],
    associations: Sequence[dict[str, object]],
    *,
    decision: str,
) -> dict[str, Any]:
    """Build an auditable GeoJSON feature for a reconstructed route."""
    start = nodes[route.start_node].point
    end = nodes[route.end_node].point
    route_labels = [
        item for item in associations if int(item["route_index"]) == route_index
    ]
    voltages = sorted(
        {
            float(item["voltage_kv"])
            for item in route_labels
            if item.get("voltage_kv") is not None
        }
    )
    wires = sorted(
        {str(item["wires"]) for item in route_labels if item.get("wires")}
    )
    reciprocal = route.confidence == "reciprocal"
    return {
        "type": "Feature",
        "properties": {
            "object_type": "overhead_power_line",
            "decision": decision,
            "reason": (
                "reconstructed_from_reciprocal_arrow_evidence"
                if reciprocal
                else "one_sided_arrow_route_hypothesis"
            ),
            "status": (
                "algorithmic_reconstruction"
                if reciprocal
                else "manual_review"
            ),
            "manual_review_required": True,
            "manual_review_reason": (
                "A DXF arrow may denote a turn, power-flow direction, or a line "
                "break; confirm its semantics and voltage from the legend or "
                "explanatory note before calculating a protection zone."
            ),
            "evidence": (
                "reciprocal_arrows" if reciprocal else "one_sided_arrow"
            ),
            "route_index": route_index,
            "start_node": route.start_node,
            "end_node": route.end_node,
            "angle_error_deg": route.angle_error_deg,
            "lateral_error_dxf_units": route.lateral_error_dxf_units,
            "route_length_dxf_units": route.distance_dxf_units,
            "voltage_kv_candidates": voltages,
            "wire_count_labels": wires,
            "coordinate_reference": "local_dxf_coordinates",
            "source_part_count": 2 if reciprocal else 1,
            "inferred_connection_count": 1,
        },
        "geometry": mapping(LineString((start, end))),
    }


def unmatched_arrow_feature(index: int, arrow: Arrow) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "object_type": "overhead_power_line",
            "decision": "manual_review",
            "reason": "unmatched_arrow",
            "status": "manual_review",
            "manual_review_required": True,
            "arrow_index": index,
            "shaft_handle": arrow.shaft_handle,
            "coordinate_reference": "local_dxf_coordinates",
        },
        "geometry": mapping(LineString((arrow.tail, arrow.tip))),
    }


def write_jsonl(path: Path, features: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as destination:
        for feature in features:
            destination.write(json.dumps(feature, ensure_ascii=False) + "\n")


def merge_accepted_utilities(
    base_path: Path | None,
    output_path: Path,
    accepted_routes: Sequence[dict[str, Any]],
) -> None:
    """Replace raw overhead-power graphics with reciprocal route geometry."""
    retained: list[dict[str, Any]] = []
    if base_path is not None:
        with base_path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                feature = json.loads(line)
                if feature.get("type") != "Feature":
                    raise ValueError(
                        f"Line {line_number}: expected GeoJSON Feature in base utilities"
                    )
                if (
                    feature.get("properties", {}).get("object_type")
                    == "overhead_power_line"
                ):
                    continue
                retained.append(feature)
    write_jsonl(output_path, [*retained, *accepted_routes])


def _ensure_layers(document: Drawing) -> None:
    for name, color in DEBUG_LAYERS.items():
        if name not in document.layers:
            document.layers.add(name, color=color, lineweight=35)


def export_debug_dxf(
    document: Drawing,
    output_path: Path,
    arrows: Sequence[Arrow],
    nodes: Sequence[Node],
    routes: Sequence[Route],
    used_arrows: set[int],
    labels: Sequence[Label],
    associations: Sequence[dict[str, object]],
) -> None:
    _ensure_layers(document)
    modelspace = document.modelspace()
    for node in nodes:
        modelspace.add_circle(node.point, radius=0.45, dxfattribs={"layer": "DEBUG_LEP_NODES"})

    for route in routes:
        layer = "DEBUG_LEP_RECIPROCAL" if route.confidence == "reciprocal" else "DEBUG_LEP_ONE_SIDED"
        modelspace.add_line(
            nodes[route.start_node].point,
            nodes[route.end_node].point,
            dxfattribs={"layer": layer},
        )

    for index, arrow in enumerate(arrows):
        if index in used_arrows:
            continue
        dx, dy = arrow.direction
        preview_end = arrow.tail[0] + dx * 12.0, arrow.tail[1] + dy * 12.0
        modelspace.add_line(arrow.tail, preview_end, dxfattribs={"layer": "DEBUG_LEP_UNMATCHED"})

    labels_by_handle = {label.handle: label for label in labels}
    for association in associations:
        label = labels_by_handle[str(association["label_handle"])]
        route = routes[int(association["route_index"])]
        start = nodes[route.start_node].point
        end = nodes[route.end_node].point
        midpoint = (start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0
        modelspace.add_line(label.point, midpoint, dxfattribs={"layer": "DEBUG_LEP_TEXT_LINK"})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.saveas(output_path)


def export_preview(
    output_path: Path,
    arrows: Sequence[Arrow],
    nodes: Sequence[Node],
    routes: Sequence[Route],
    used_arrows: set[int],
    labels: Sequence[Label],
) -> None:
    """Write a compact PNG that shows only the reconstruction experiment."""
    figure, axes = plt.subplots(figsize=(14, 10))
    for index, arrow in enumerate(arrows):
        color = "#9ca3af" if index in used_arrows else "#dc2626"
        axes.annotate(
            "",
            xy=arrow.tip,
            xytext=arrow.tail,
            arrowprops={"arrowstyle": "->", "color": color, "linewidth": 0.8},
        )
    for route in routes:
        start = nodes[route.start_node].point
        end = nodes[route.end_node].point
        color = "#16a34a" if route.confidence == "reciprocal" else "#f59e0b"
        axes.plot([start[0], end[0]], [start[1], end[1]], color=color, linewidth=2.0)
    if nodes:
        axes.scatter(
            [node.point[0] for node in nodes],
            [node.point[1] for node in nodes],
            s=15,
            color="#2563eb",
            zorder=4,
        )
    for label in labels:
        if label.voltage_kv is not None or label.wires is not None:
            axes.text(label.point[0], label.point[1], label.text, fontsize=6, color="#7e22ce")
    axes.set_aspect("equal", adjustable="datalim")
    axes.grid(True, linewidth=0.3, alpha=0.35)
    axes.set_title(
        "LEP reconstruction: green=reciprocal, orange=one-sided, red=unmatched; "
        "manual review required"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.tight_layout()
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def run(
    input_path: Path,
    output_path: Path,
    review_output_path: Path,
    report_path: Path,
    *,
    base_utilities_path: Path | None = None,
    objects_path: Path | None = None,
    debug_dxf_path: Path | None = None,
    dxf_units_per_meter: float = 1.0,
    min_distance: float = 6.0,
    max_distance: float = 500.0,
    max_angle_deg: float = 7.0,
    preview_path: Path | None = None,
) -> dict[str, object]:
    if not math.isfinite(dxf_units_per_meter) or dxf_units_per_meter <= 0:
        raise ValueError("dxf_units_per_meter must be finite and greater than zero")
    if objects_path is None:
        document = ezdxf.readfile(input_path)
        primitives, labels = read_lep_entities(document)
        input_mode = "source_dxf_recursive"
    else:
        document = ezdxf.new("R2018") if debug_dxf_path is not None else None
        primitives = read_lep_records(objects_path)
        labels = []
        input_mode = "go_extractor_records"
    arrows = detect_arrows(
        primitives,
        endpoint_tolerance=0.04 * dxf_units_per_meter,
        dxf_units_per_meter=dxf_units_per_meter,
    )
    nodes = build_nodes(arrows, tolerance=0.08 * dxf_units_per_meter)
    routes, used_arrows = reconstruct_routes(
        arrows,
        nodes,
        min_distance=min_distance * dxf_units_per_meter,
        max_distance=max_distance * dxf_units_per_meter,
        max_angle_deg=max_angle_deg,
        max_lateral_m=1.0 * dxf_units_per_meter,
    )
    associations = associate_labels(
        labels,
        routes,
        nodes,
        max_distance=8.0 * dxf_units_per_meter,
    )
    route_features = [
        route_feature(
            route_index,
            route,
            nodes,
            associations,
            decision=("accepted" if route.confidence == "reciprocal" else "manual_review"),
        )
        for route_index, route in enumerate(routes)
    ]
    accepted_routes = [
        feature
        for feature in route_features
        if feature["properties"]["decision"] == "accepted"
    ]
    review_routes = [
        feature
        for feature in route_features
        if feature["properties"]["decision"] == "manual_review"
    ]
    review_routes.extend(
        unmatched_arrow_feature(index, arrow)
        for index, arrow in enumerate(arrows)
        if index not in used_arrows
    )
    merge_accepted_utilities(base_utilities_path, output_path, accepted_routes)
    write_jsonl(review_output_path, review_routes)
    if debug_dxf_path is not None:
        assert document is not None
        export_debug_dxf(
            document,
            debug_dxf_path,
            arrows,
            nodes,
            routes,
            used_arrows,
            labels,
            associations,
        )
    if preview_path is not None:
        export_preview(preview_path, arrows, nodes, routes, used_arrows, labels)

    report: dict[str, object] = {
        "input": str(input_path),
        "output": str(output_path),
        "base_utilities": str(base_utilities_path) if base_utilities_path else None,
        "objects": str(objects_path) if objects_path else None,
        "input_mode": input_mode,
        "review_output": str(review_output_path),
        "debug_dxf": str(debug_dxf_path) if debug_dxf_path else None,
        "preview": str(preview_path) if preview_path else None,
        "status": "manual_review_required",
        "semantic_caveat": (
            "An arrow may denote a turn, power-flow direction, or a line break. "
            "The reconstructed geometry must be checked against the drawing legend "
            "or explanatory note before a protection zone is calculated."
        ),
        "dxf_units_per_meter": dxf_units_per_meter,
        "source_primitives": len(primitives),
        "source_labels": len(labels),
        "detected_arrows": len(arrows),
        "detected_nodes": len(nodes),
        "reciprocal_routes": sum(route.confidence == "reciprocal" for route in routes),
        "one_sided_routes": sum(route.confidence == "one_sided" for route in routes),
        "accepted_route_features": len(accepted_routes),
        "manual_review_features": len(review_routes),
        "unmatched_arrows": len(arrows) - len(used_arrows),
        "parsed_voltage_labels": sum(label.voltage_kv is not None for label in labels),
        "parsed_wire_labels": sum(label.wires is not None for label in labels),
        "label_associations": associations,
        "nodes": [asdict(node) for node in nodes],
        "routes": [asdict(route) for route in routes],
        "labels": [asdict(label) for label in labels],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconstruct experimental overhead power-line routes from DXF arrows."
    )
    parser.add_argument("input", type=Path, help="Source DXF")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Accepted utility GeoJSONL with raw overhead-power graphics replaced",
    )
    parser.add_argument(
        "--base-utilities",
        type=Path,
        help="Existing reconstructed utility GeoJSONL to preserve and augment",
    )
    parser.add_argument(
        "--objects",
        type=Path,
        help="Raw JSONL from the Go extractor; avoids recursively reparsing a large DXF",
    )
    parser.add_argument(
        "--review-output",
        type=Path,
        required=True,
        help="One-sided and unmatched arrow hypotheses as GeoJSONL",
    )
    parser.add_argument("--report", type=Path, required=True, help="JSON report")
    parser.add_argument("--debug-dxf", type=Path, help="Optional annotated source DXF")
    parser.add_argument("--preview", type=Path, help="Optional PNG preview")
    parser.add_argument("--dxf-units-per-meter", type=float, default=1.0)
    parser.add_argument("--max-distance", type=float, default=500.0)
    parser.add_argument(
        "--min-distance",
        type=float,
        default=6.0,
        help="Ignore shorter links inside a composite arrow symbol",
    )
    parser.add_argument("--max-angle", type=float, default=7.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = run(
        args.input,
        args.output,
        args.review_output,
        args.report,
        base_utilities_path=args.base_utilities,
        objects_path=args.objects,
        debug_dxf_path=args.debug_dxf,
        dxf_units_per_meter=args.dxf_units_per_meter,
        min_distance=args.min_distance,
        max_distance=args.max_distance,
        max_angle_deg=args.max_angle,
        preview_path=args.preview,
    )
    print(f"Read: {args.input}")
    print(f"Output: {args.output}")
    print(f"Review: {args.review_output}")
    print(f"Report: {args.report}")
    if args.debug_dxf:
        print(f"Debug DXF: {args.debug_dxf}")
    if args.preview:
        print(f"Preview: {args.preview}")
    print(f"Arrows: {report['detected_arrows']}")
    print(f"Nodes: {report['detected_nodes']}")
    print(f"Reciprocal routes: {report['reciprocal_routes']}")
    print(f"One-sided routes: {report['one_sided_routes']}")
    print(f"Unmatched arrows: {report['unmatched_arrows']}")


if __name__ == "__main__":
    main()
