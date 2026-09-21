"""Reconstruct small gaps in cleaned engineering utility linework.

The module consumes accepted GeoJSONL emitted by ``utility_detector`` or
``utility_cleaner``.  It preserves every accepted source line, adds explicit
inferred connectors, and writes a drop-in replacement for
``cleaned_utilities.geojsonl``.

Parallel lines are intentionally preserved.  They can describe the two sides
of a wide pipe or utility corridor; each side is continued independently.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import yaml
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    Point,
    mapping,
    shape,
)
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree


@dataclass(frozen=True)
class Endpoint:
    index: int
    line_index: int
    at_start: bool
    point: Point
    outward: tuple[float, float]


@dataclass(frozen=True)
class Candidate:
    kind: str
    source_endpoint: int
    target_endpoint: int | None
    target_line: int
    target_point: Point
    distance: float
    source_angle_deg: float
    target_angle_deg: float | None
    score: float
    parallel_pipe_support: bool = False
    wide_pipe_fan: bool = False


def line_parts(geometry: Any) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        if not geometry.is_empty and len(geometry.coords) >= 2 and geometry.length > 0:
            yield geometry
    elif isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            yield from line_parts(part)
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from line_parts(part)


def merge_lines(lines: Iterable[LineString]) -> LineString | MultiLineString:
    materialized = [line for line in lines if not line.is_empty and line.length > 0]
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


def unit_vector(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy)
    if length == 0:
        return (0.0, 0.0)
    return (dx / length, dy / length)


def angle_degrees(left: tuple[float, float], right: tuple[float, float]) -> float:
    dot = max(-1.0, min(1.0, left[0] * right[0] + left[1] * right[1]))
    return math.degrees(math.acos(dot))


def build_endpoints(lines: list[LineString]) -> list[Endpoint]:
    result: list[Endpoint] = []
    for line_index, line in enumerate(lines):
        coordinates = list(line.coords)
        start = coordinates[0]
        next_point = coordinates[1]
        end = coordinates[-1]
        previous = coordinates[-2]
        result.append(Endpoint(
            len(result),
            line_index,
            True,
            Point(start),
            unit_vector(start[0] - next_point[0], start[1] - next_point[1]),
        ))
        result.append(Endpoint(
            len(result),
            line_index,
            False,
            Point(end),
            unit_vector(end[0] - previous[0], end[1] - previous[1]),
        ))
    return result


def free_endpoint_indices(
    lines: list[LineString],
    endpoints: list[Endpoint],
    tolerance: float,
) -> set[int]:
    """Return endpoints not already touching another source line."""
    if not lines:
        return set()
    tree = STRtree(lines)
    free: set[int] = set()
    for endpoint in endpoints:
        connected = False
        search = endpoint.point.buffer(tolerance)
        for line_index in tree.query(search):
            other_index = int(line_index)
            if other_index == endpoint.line_index:
                continue
            if lines[other_index].distance(endpoint.point) <= tolerance:
                connected = True
                break
        if not connected:
            free.add(endpoint.index)
    return free


def continuation_candidates(
    endpoints: list[Endpoint],
    free: set[int],
    rules: dict[str, Any],
) -> list[Candidate]:
    max_gap = float(rules["max_gap"])
    snap_tolerance = float(rules["snap_tolerance"])
    max_angle = float(rules["max_angle_deg"])
    points = [endpoint.point for endpoint in endpoints]
    tree = STRtree(points)
    result: list[Candidate] = []
    for left_index in sorted(free):
        left = endpoints[left_index]
        for raw_right_index in tree.query(left.point.buffer(max_gap)):
            right_index = int(raw_right_index)
            if right_index <= left_index or right_index not in free:
                continue
            right = endpoints[right_index]
            if right.line_index == left.line_index:
                continue
            distance = left.point.distance(right.point)
            if distance <= snap_tolerance or distance > max_gap:
                continue
            gap_from_left = unit_vector(
                right.point.x - left.point.x,
                right.point.y - left.point.y,
            )
            gap_from_right = (-gap_from_left[0], -gap_from_left[1])
            left_angle = angle_degrees(left.outward, gap_from_left)
            right_angle = angle_degrees(right.outward, gap_from_right)
            if left_angle > max_angle or right_angle > max_angle:
                continue
            direction_score = 1.0 - (left_angle + right_angle) / (2.0 * max_angle)
            distance_score = 1.0 - distance / max_gap
            score = 0.7 * direction_score + 0.3 * distance_score
            result.append(Candidate(
                "continuation",
                left_index,
                right_index,
                right.line_index,
                right.point,
                distance,
                left_angle,
                right_angle,
                max(0.0, min(1.0, score)),
            ))
    return result


def add_parallel_pipe_support(
    candidates: list[Candidate],
    endpoints: list[Endpoint],
    rules: dict[str, Any],
) -> list[Candidate]:
    """Reward paired, parallel continuations without collapsing them to an axis."""
    if not bool(rules.get("parallel_support_enabled", True)):
        return candidates
    max_width = float(rules["parallel_pipe_max_width"])
    max_gap_delta = float(rules["parallel_gap_difference"])
    max_direction_delta = float(rules["parallel_direction_difference_deg"])
    bonus = float(rules["parallel_support_bonus"])
    supported: set[int] = set()
    for left_index, left in enumerate(candidates):
        if left.kind != "continuation" or left.target_endpoint is None:
            continue
        left_start = endpoints[left.source_endpoint].point
        left_end = endpoints[left.target_endpoint].point
        left_vector = unit_vector(left_end.x - left_start.x, left_end.y - left_start.y)
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            if right.kind != "continuation" or right.target_endpoint is None:
                continue
            if len({
                left.source_endpoint,
                left.target_endpoint,
                right.source_endpoint,
                right.target_endpoint,
            }) < 4:
                continue
            right_start = endpoints[right.source_endpoint].point
            right_end = endpoints[right.target_endpoint].point
            right_vector = unit_vector(right_end.x - right_start.x, right_end.y - right_start.y)
            direction_delta = min(
                angle_degrees(left_vector, right_vector),
                angle_degrees(left_vector, (-right_vector[0], -right_vector[1])),
            )
            if direction_delta > max_direction_delta:
                continue
            if abs(left.distance - right.distance) > max_gap_delta:
                continue
            start_width = left_start.distance(right_start)
            end_width = left_end.distance(right_end)
            if max(start_width, end_width) > max_width:
                continue
            if abs(start_width - end_width) > max_gap_delta:
                continue
            supported.update((left_index, right_index))
    return [
        replace(
            candidate,
            score=min(1.0, candidate.score + bonus),
            parallel_pipe_support=True,
        ) if index in supported else candidate
        for index, candidate in enumerate(candidates)
    ]


def junction_candidates(
    lines: list[LineString],
    endpoints: list[Endpoint],
    free: set[int],
    rules: dict[str, Any],
) -> list[Candidate]:
    if not bool(rules.get("enable_junctions", True)) or not lines:
        return []
    max_gap = float(rules["max_junction_gap"])
    snap_tolerance = float(rules["snap_tolerance"])
    max_angle = float(rules["max_junction_angle_deg"])
    interior_margin = float(rules["junction_interior_margin"])
    tree = STRtree(lines)
    result: list[Candidate] = []
    for endpoint_index in sorted(free):
        endpoint = endpoints[endpoint_index]
        for raw_line_index in tree.query(endpoint.point.buffer(max_gap)):
            line_index = int(raw_line_index)
            if line_index == endpoint.line_index:
                continue
            target = lines[line_index]
            projected_distance = target.project(endpoint.point)
            if projected_distance <= interior_margin:
                continue
            if target.length - projected_distance <= interior_margin:
                continue
            projected = target.interpolate(projected_distance)
            distance = endpoint.point.distance(projected)
            if distance <= snap_tolerance or distance > max_gap:
                continue
            direction = unit_vector(
                projected.x - endpoint.point.x,
                projected.y - endpoint.point.y,
            )
            source_angle = angle_degrees(endpoint.outward, direction)
            if source_angle > max_angle:
                continue
            direction_score = 1.0 - source_angle / max_angle
            distance_score = 1.0 - distance / max_gap
            score = 0.65 * direction_score + 0.35 * distance_score
            result.append(Candidate(
                "junction",
                endpoint_index,
                None,
                line_index,
                projected,
                distance,
                source_angle,
                None,
                max(0.0, min(1.0, score)),
            ))
    return result


def candidate_connector(candidate: Candidate, endpoints: list[Endpoint]) -> LineString:
    source = endpoints[candidate.source_endpoint].point
    return LineString([(source.x, source.y), (candidate.target_point.x, candidate.target_point.y)])


def select_candidates(
    candidates: list[Candidate],
    endpoints: list[Endpoint],
    rules: dict[str, Any],
) -> tuple[list[Candidate], list[Candidate]]:
    accept_score = float(rules["accept_score"])
    review_score = float(rules["review_score"])
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.score,
            item.parallel_pipe_support,
            item.kind == "continuation",
            -item.distance,
        ),
        reverse=True,
    )
    allow_ambiguous = bool(rules.get("allow_ambiguous_continuations", True))
    max_per_endpoint = int(rules.get("max_connections_per_endpoint", 3))
    score_delta = float(rules.get("ambiguous_score_delta", 0.12))

    eligible = [item for item in ordered if item.score >= accept_score]
    by_endpoint: dict[int, list[Candidate]] = defaultdict(list)
    for candidate in eligible:
        by_endpoint[candidate.source_endpoint].append(candidate)
        if candidate.target_endpoint is not None:
            by_endpoint[candidate.target_endpoint].append(candidate)

    shortlisted_keys: dict[int, set[tuple[Any, ...]]] = {}
    for endpoint_index, endpoint_candidates in by_endpoint.items():
        best_score = endpoint_candidates[0].score
        limit = max_per_endpoint if allow_ambiguous else 1
        shortlisted_keys[endpoint_index] = {
            candidate_key(item)
            for item in endpoint_candidates[:limit]
            if best_score - item.score <= score_delta
        }

    accepted: list[Candidate] = []
    for candidate in eligible:
        key = candidate_key(candidate)
        if key not in shortlisted_keys.get(candidate.source_endpoint, set()):
            continue
        if (
            candidate.target_endpoint is not None
            and key not in shortlisted_keys.get(candidate.target_endpoint, set())
        ):
            continue
        accepted.append(candidate)

    endpoint_use = Counter(
        endpoint_index
        for candidate in accepted
        for endpoint_index in (
            candidate.source_endpoint,
            *(() if candidate.target_endpoint is None else (candidate.target_endpoint,)),
        )
    )
    accepted = [
        replace(
            candidate,
            wide_pipe_fan=(
                endpoint_use[candidate.source_endpoint] > 1
                or (
                    candidate.target_endpoint is not None
                    and endpoint_use[candidate.target_endpoint] > 1
                )
            ),
        )
        for candidate in accepted
    ]

    accepted_keys = {candidate_key(item) for item in accepted}
    review_by_source: dict[int, Candidate] = {}
    for candidate in ordered:
        key = candidate_key(candidate)
        if key in accepted_keys or candidate.score < review_score:
            continue
        current = review_by_source.get(candidate.source_endpoint)
        if current is None or candidate.score > current.score:
            review_by_source[candidate.source_endpoint] = candidate
    return accepted, list(review_by_source.values())


def candidate_key(candidate: Candidate) -> tuple[Any, ...]:
    return (
        candidate.kind,
        candidate.source_endpoint,
        candidate.target_endpoint,
        candidate.target_line,
        round(candidate.target_point.x, 9),
        round(candidate.target_point.y, 9),
    )


def default_rules() -> dict[str, Any]:
    return {
        "snap_tolerance_m": 0.05,
        "max_gap_m": 5.0,
        "max_angle_deg": 12.0,
        "accept_score": 0.58,
        "review_score": 0.38,
        "enable_junctions": True,
        "max_junction_gap_m": 2.0,
        "max_junction_angle_deg": 18.0,
        "junction_interior_margin_m": 0.10,
        "parallel_support_enabled": True,
        "parallel_pipe_max_width_m": 6.0,
        "parallel_gap_difference_m": 0.75,
        "parallel_direction_difference_deg": 5.0,
        "parallel_support_bonus": 0.08,
        "allow_ambiguous_continuations": True,
        "max_connections_per_endpoint": 3,
        "ambiguous_score_delta": 0.12,
    }


def scaled_rules(
    config: dict[str, Any],
    object_type: str,
    dxf_units_per_meter: float,
) -> dict[str, Any]:
    rules = default_rules()
    rules.update(config.get("defaults", {}))
    rules.update(config.get("network_types", {}).get(object_type, {}))
    for name in (
        "snap_tolerance_m",
        "max_gap_m",
        "max_junction_gap_m",
        "junction_interior_margin_m",
        "parallel_pipe_max_width_m",
        "parallel_gap_difference_m",
    ):
        value = float(rules[name])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{object_type}.{name} must be finite and non-negative")
        rules[name.removesuffix("_m")] = value * dxf_units_per_meter
    for name in (
        "max_angle_deg",
        "max_junction_angle_deg",
        "parallel_direction_difference_deg",
    ):
        value = float(rules[name])
        if not 0 < value <= 90:
            raise ValueError(f"{object_type}.{name} must be in (0, 90]")
    for name in ("accept_score", "review_score", "parallel_support_bonus"):
        value = float(rules[name])
        if not 0 <= value <= 1:
            raise ValueError(f"{object_type}.{name} must be in [0, 1]")
    if float(rules["review_score"]) > float(rules["accept_score"]):
        raise ValueError(f"{object_type}: review_score must not exceed accept_score")
    if int(rules["max_connections_per_endpoint"]) < 1:
        raise ValueError(
            f"{object_type}.max_connections_per_endpoint must be at least 1"
        )
    score_delta = float(rules["ambiguous_score_delta"])
    if not 0 <= score_delta <= 1:
        raise ValueError(f"{object_type}.ambiguous_score_delta must be in [0, 1]")
    return rules


def load_cleaned_lines(path: Path) -> dict[str, list[LineString]]:
    grouped: dict[str, list[LineString]] = defaultdict(list)
    with path.open(encoding="utf-8") as source:
        for line_number, row in enumerate(source, start=1):
            if not row.strip():
                continue
            feature = json.loads(row)
            if feature.get("type") != "Feature":
                raise ValueError(f"Line {line_number}: expected GeoJSON Feature")
            properties = feature.get("properties", {})
            if properties.get("decision") != "accepted":
                raise ValueError(f"Line {line_number}: expected decision=accepted")
            object_type = properties.get("object_type")
            if not object_type:
                raise ValueError(f"Line {line_number}: object_type is missing")
            geometry_data = feature.get("geometry")
            if not geometry_data:
                continue
            parts = list(line_parts(shape(geometry_data)))
            if not parts:
                raise ValueError(f"Line {line_number}: expected lineal geometry")
            grouped[str(object_type)].extend(parts)
    return dict(grouped)


def reconstruct_type(
    lines: list[LineString],
    rules: dict[str, Any],
) -> tuple[list[Candidate], list[Candidate], list[Endpoint]]:
    endpoints = build_endpoints(lines)
    free = free_endpoint_indices(lines, endpoints, float(rules["snap_tolerance"]))
    continuations = continuation_candidates(endpoints, free, rules)
    continuations = add_parallel_pipe_support(continuations, endpoints, rules)
    junctions = junction_candidates(lines, endpoints, free, rules)
    accepted, review = select_candidates(continuations + junctions, endpoints, rules)
    return accepted, review, endpoints


def reconstructed_feature(
    object_type: str,
    lines: list[LineString],
    accepted: list[Candidate],
    endpoints: list[Endpoint],
) -> dict[str, Any]:
    connectors = [candidate_connector(item, endpoints) for item in accepted]
    return {
        "type": "Feature",
        "properties": {
            "object_type": object_type,
            "decision": "accepted",
            "reason": "accepted_with_reconstructed_gaps",
            "status": "algorithmic_reconstruction",
            "coordinate_reference": "local_dxf_coordinates",
            "source_part_count": len(lines),
            "inferred_connection_count": len(connectors),
            "parallel_lines_preserved": True,
        },
        "geometry": mapping(merge_lines([*lines, *connectors])),
    }


def inferred_feature(
    object_type: str,
    candidate: Candidate,
    endpoints: list[Endpoint],
    decision: str,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "object_type": object_type,
            "decision": decision,
            "reason": f"inferred_{candidate.kind}",
            "inferred": True,
            "confidence": round(candidate.score, 6),
            "gap_length_dxf_units": candidate.distance,
            "source_angle_deg": candidate.source_angle_deg,
            "target_angle_deg": candidate.target_angle_deg,
            "source_line_index": endpoints[candidate.source_endpoint].line_index,
            "target_line_index": candidate.target_line,
            "parallel_pipe_support": candidate.parallel_pipe_support,
            "wide_pipe_fan": candidate.wide_pipe_fan,
            "coordinate_reference": "local_dxf_coordinates",
        },
        "geometry": mapping(candidate_connector(candidate, endpoints)),
    }


def write_jsonl(path: Path, features: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as destination:
        for feature in features:
            destination.write(json.dumps(feature, ensure_ascii=False) + "\n")


def safe_layer(prefix: str, object_type: str) -> str:
    return f"{prefix}_{object_type}".upper()[:255]


def write_debug_dxf(
    path: Path,
    grouped: dict[str, list[LineString]],
    accepted_by_type: dict[str, list[Candidate]],
    review_by_type: dict[str, list[Candidate]],
    endpoints_by_type: dict[str, list[Endpoint]],
) -> Path:
    document = ezdxf.new("R2018")
    modelspace = document.modelspace()
    for object_type, lines in sorted(grouped.items()):
        source_layer = safe_layer("SOURCE", object_type)
        inferred_layer = safe_layer("INFERRED", object_type)
        review_layer = safe_layer("REVIEW_GAP", object_type)
        document.layers.add(source_layer, color=8)
        document.layers.add(inferred_layer, color=4)
        document.layers.add(review_layer, color=2)
        for line in lines:
            modelspace.add_lwpolyline(list(line.coords), dxfattribs={"layer": source_layer})
        endpoints = endpoints_by_type[object_type]
        for candidate in accepted_by_type[object_type]:
            connector = candidate_connector(candidate, endpoints)
            modelspace.add_lwpolyline(
                list(connector.coords),
                dxfattribs={"layer": inferred_layer},
            )
        for candidate in review_by_type[object_type]:
            connector = candidate_connector(candidate, endpoints)
            modelspace.add_lwpolyline(
                list(connector.coords),
                dxfattribs={"layer": review_layer},
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    candidates = [path, *(
        path.with_name(f"{path.stem}_v{version}{path.suffix}")
        for version in range(2, 100)
    )]
    last_error: PermissionError | None = None
    for candidate in candidates:
        try:
            document.saveas(candidate)
            return candidate
        except PermissionError as error:
            last_error = error
    assert last_error is not None
    raise last_error


def reconstruct(
    input_path: Path,
    output_path: Path,
    inferred_output_path: Path,
    review_output_path: Path,
    report_path: Path,
    config_path: Path,
    dxf_units_per_meter: float = 1.0,
    debug_dxf_path: Path | None = None,
) -> dict[str, Any]:
    if not math.isfinite(dxf_units_per_meter) or dxf_units_per_meter <= 0:
        raise ValueError("dxf_units_per_meter must be finite and greater than zero")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    grouped = load_cleaned_lines(input_path)
    reconstructed: list[dict[str, Any]] = []
    inferred: list[dict[str, Any]] = []
    review_features: list[dict[str, Any]] = []
    accepted_by_type: dict[str, list[Candidate]] = {}
    review_by_type: dict[str, list[Candidate]] = {}
    endpoints_by_type: dict[str, list[Endpoint]] = {}
    report: dict[str, Any] = {
        "input": str(input_path),
        "output": str(output_path),
        "status": "algorithmic_reconstruction",
        "dxf_units_per_meter": dxf_units_per_meter,
        "parallel_lines_preserved": True,
        "network_types": {},
    }
    for object_type, lines in sorted(grouped.items()):
        rules = scaled_rules(config, object_type, dxf_units_per_meter)
        reconstruction_enabled = bool(rules.get("reconstruction_enabled", True))
        if reconstruction_enabled:
            accepted, review, endpoints = reconstruct_type(lines, rules)
            free_endpoint_count: int | None = len(free_endpoint_indices(
                lines, endpoints, float(rules["snap_tolerance"])
            ))
        else:
            # Preserve the accepted source geometry for audit/export, but do not
            # spend minutes inferring connections that no active rule consumes.
            accepted, review, endpoints = [], [], build_endpoints(lines)
            free_endpoint_count = None
        accepted_by_type[object_type] = accepted
        review_by_type[object_type] = review
        endpoints_by_type[object_type] = endpoints
        reconstructed.append(reconstructed_feature(
            object_type, lines, accepted, endpoints
        ))
        inferred.extend(
            inferred_feature(object_type, item, endpoints, "accepted")
            for item in accepted
        )
        review_features.extend(
            inferred_feature(object_type, item, endpoints, "manual_review")
            for item in review
        )
        kinds = Counter(item.kind for item in accepted)
        report["network_types"][object_type] = {
            "source_part_count": len(lines),
            "reconstruction_enabled": reconstruction_enabled,
            "free_endpoint_count": free_endpoint_count,
            "accepted_connection_count": len(accepted),
            "review_connection_count": len(review),
            "accepted_by_kind": dict(kinds),
            "parallel_supported_connection_count": sum(
                item.parallel_pipe_support for item in accepted
            ),
            "wide_pipe_fan_connection_count": sum(
                item.wide_pipe_fan for item in accepted
            ),
            "inferred_length_dxf_units": sum(item.distance for item in accepted),
            "effective_rules": {
                key: value
                for key, value in rules.items()
                if not key.endswith("_m")
            },
        }
    write_jsonl(output_path, reconstructed)
    write_jsonl(inferred_output_path, inferred)
    write_jsonl(review_output_path, review_features)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    actual_debug = None
    if debug_dxf_path is not None:
        actual_debug = write_debug_dxf(
            debug_dxf_path,
            grouped,
            accepted_by_type,
            review_by_type,
            endpoints_by_type,
        )
    print(f"Input: {input_path}")
    print(f"Reconstructed utilities: {output_path}")
    print(f"Inferred connections: {inferred_output_path}")
    print(f"Review candidates: {review_output_path}")
    print(f"Report: {report_path}")
    if actual_debug is not None:
        print(f"Debug DXF: {actual_debug}")
    for object_type, item in report["network_types"].items():
        print(
            f"  {object_type}: {item['accepted_connection_count']} inferred, "
            f"{item['review_connection_count']} review, "
            f"{item['parallel_supported_connection_count']} parallel-supported, "
            f"{item['wide_pipe_fan_connection_count']} wide-pipe fan"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconstruct gaps and junctions in cleaned utility linework."
    )
    parser.add_argument("input", type=Path, help="Accepted cleaned utility GeoJSONL")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reconstructed_utilities.geojsonl"),
    )
    parser.add_argument(
        "--inferred-output",
        type=Path,
        default=Path("inferred_utility_connections.geojsonl"),
    )
    parser.add_argument(
        "--review-output",
        type=Path,
        default=Path("review_utility_connections.geojsonl"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("network_reconstruction_report.json"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "core" / "network_reconstructor_config.yaml",
    )
    parser.add_argument("--debug-dxf", type=Path)
    parser.add_argument("--dxf-units-per-meter", type=float, default=1.0)
    args = parser.parse_args()
    try:
        reconstruct(
            args.input,
            args.output,
            args.inferred_output,
            args.review_output,
            args.report,
            args.config,
            args.dxf_units_per_meter,
            args.debug_dxf,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"Network reconstruction error: {error}") from error


if __name__ == "__main__":
    main()
