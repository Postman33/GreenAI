"""Recover compact heat chambers drawn as short, disconnected CAD strokes."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from shapely.geometry import (
    GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon, box,
)
from shapely.ops import unary_union
from shapely.strtree import STRtree


@dataclass(frozen=True)
class Stroke:
    start: tuple[float, float]
    end: tuple[float, float]
    length: float
    angle: float


@dataclass(frozen=True)
class Run:
    offset: float
    start: float
    end: float
    intervals: tuple[tuple[float, float], ...]


def _line_parts(geometry: Any) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, (MultiLineString, GeometryCollection)):
        for part in geometry.geoms:
            yield from _line_parts(part)


def _polygon_parts(geometry: Any) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from _polygon_parts(part)


def _angle_distance(left: float, right: float) -> float:
    delta = abs(left - right) % 180.0
    return min(delta, 180.0 - delta)


def _dot(point: tuple[float, float], axis: tuple[float, float]) -> float:
    return point[0] * axis[0] + point[1] * axis[1]


def _strokes(geometry: Any, scale: float) -> list[Stroke]:
    result = []
    for line in _line_parts(geometry):
        coordinates = list(line.coords)
        for first, second in zip(coordinates, coordinates[1:]):
            start = (first[0], first[1])
            end = (second[0], second[1])
            length = math.dist(start, end)
            if not 0.3 * scale <= length <= 1.3 * scale:
                continue
            result.append(Stroke(
                start, end, length,
                math.degrees(math.atan2(end[1] - start[1], end[0] - start[0])) % 180,
            ))
    return result


def _runs(
    strokes: list[Stroke],
    axis: tuple[float, float],
    normal: tuple[float, float],
    angle: float,
    scale: float,
) -> list[Run]:
    projected = []
    for stroke in strokes:
        if _angle_distance(stroke.angle, angle) > 4.0:
            continue
        offset = (_dot(stroke.start, normal) + _dot(stroke.end, normal)) / 2
        start, end = sorted((_dot(stroke.start, axis), _dot(stroke.end, axis)))
        projected.append((offset, start, end, stroke.length))
    projected.sort()

    collinear_groups: list[list[tuple[float, float, float, float]]] = []
    for item in projected:
        if not collinear_groups or item[0] - collinear_groups[-1][-1][0] > 0.22 * scale:
            collinear_groups.append([item])
        else:
            collinear_groups[-1].append(item)

    result = []
    for group in collinear_groups:
        group.sort(key=lambda item: item[1])
        chunks: list[list[tuple[float, float, float, float]]] = []
        chunk_end = -math.inf
        for item in group:
            if not chunks or item[1] - chunk_end > 1.0 * scale:
                chunks.append([item])
                chunk_end = item[2]
            else:
                chunks[-1].append(item)
                chunk_end = max(chunk_end, item[2])
        for chunk in chunks:
            support = sum(item[3] for item in chunk)
            start = min(item[1] for item in chunk)
            end = max(item[2] for item in chunk)
            if support < 0.7 * scale or end - start < 1.2 * scale:
                continue
            result.append(Run(
                offset=sum(item[0] * item[3] for item in chunk) / support,
                start=start,
                end=end,
                intervals=tuple((item[1], item[2]) for item in chunk),
            ))
    return result


def _coverage(run: Run, start: float, end: float) -> float:
    intervals = sorted(
        (max(start, first), min(end, last))
        for first, last in run.intervals
        if min(end, last) > max(start, first)
    )
    if not intervals:
        return 0.0
    merged = [list(intervals[0])]
    for first, last in intervals[1:]:
        if first > merged[-1][1]:
            merged.append([first, last])
        else:
            merged[-1][1] = max(merged[-1][1], last)
    return sum(last - first for first, last in merged) / (end - start)


def _mean_angle(strokes: list[Stroke], bucket: int) -> float:
    matching = [
        stroke for stroke in strokes
        if _angle_distance(stroke.angle, bucket) <= 4.0
    ]
    sine = sum(
        stroke.length * math.sin(math.radians(2 * stroke.angle))
        for stroke in matching
    )
    cosine = sum(
        stroke.length * math.cos(math.radians(2 * stroke.angle))
        for stroke in matching
    )
    return math.degrees(math.atan2(sine, cosine)) / 2 % 180


def _buffered_chambers(
    heat_lines: Any,
    wells: Polygon | MultiPolygon,
    work_boundary: Polygon | MultiPolygon,
    short_stroke_tree: STRtree,
    short_stroke_lines: list[LineString],
    scale: float,
) -> list[tuple[float, Polygon]]:
    """Find dashed chambers whose sides form a jog or a narrow rectangle.

    Round buffering joins the half-metre gaps in the source dashes.  The
    resulting enclosed hole is accepted only when most of its boundary is
    supported by short source strokes, so ordinary continuous pipe corridors
    do not become chambers merely because they enclose a well.
    """
    raw_lines = list(_line_parts(heat_lines))
    if not raw_lines:
        return []
    raw_tree = STRtree(raw_lines)
    candidates: list[tuple[float, Polygon]] = []
    search_radius = 14.0 * scale
    gap_radius = 0.3 * scale
    for well in _polygon_parts(wells):
        if not well.intersects(work_boundary):
            continue
        x, y = well.centroid.coords[0]
        search = box(
            x - search_radius, y - search_radius,
            x + search_radius, y + search_radius,
        )
        if len(short_stroke_tree.query(search)) < 8:
            continue
        indexes = raw_tree.query(search)
        if len(indexes) < 4:
            continue
        local_lines = [
            part
            for index in indexes
            for part in _line_parts(raw_lines[int(index)].intersection(search))
            if part.length > 0.05 * scale
        ]
        if len(local_lines) < 4:
            continue
        thick_lines = MultiLineString(local_lines).buffer(gap_radius)
        for region in _polygon_parts(thick_lines):
            for ring in region.interiors:
                hole = Polygon(ring)
                if not 4.0 * scale**2 <= hole.area <= 150.0 * scale**2:
                    continue
                rectangle = hole.minimum_rotated_rectangle
                if hole.area / rectangle.area < 0.65:
                    continue
                corners = list(rectangle.exterior.coords)
                sides = [
                    math.dist(corners[index], corners[index + 1])
                    for index in range(4)
                ]
                if (
                    min(sides) < 1.8 * scale
                    or max(sides) > 15.0 * scale
                    or max(sides) / min(sides) > 2.2
                ):
                    continue
                candidate = hole.buffer(gap_radius, join_style=2)
                if well.intersection(
                    candidate.buffer(0.45 * scale)
                ).area < 0.1 * scale**2:
                    continue
                if candidate.intersection(work_boundary).area < candidate.area * 0.5:
                    continue
                stroke_indexes = short_stroke_tree.query(
                    candidate.buffer(0.4 * scale)
                )
                if len(stroke_indexes) < 4:
                    continue
                nearby_strokes = MultiLineString([
                    short_stroke_lines[int(index)] for index in stroke_indexes
                ])
                support = candidate.boundary.intersection(
                    nearby_strokes.buffer(0.25 * scale)
                ).length / candidate.length
                if support < 0.6:
                    continue
                # Buffering closes the dash gaps but leaves one tooth at each
                # source stroke.  Keep the tested footprint, then remove those
                # sub-metre teeth while retaining real bends in the chamber.
                smooth = candidate.simplify(
                    0.6 * scale, preserve_topology=True
                ).buffer(0.05 * scale, join_style=2)
                candidates.append((support, smooth))
    return candidates


def detect_dashed_square_chambers(
    heat_lines: Any,
    well_footprints: Polygon | MultiPolygon,
    work_boundary: Polygon | MultiPolygon,
    units_per_meter: float,
) -> tuple[Polygon | MultiPolygon, dict[str, Any]]:
    """Infer compact four-sided chambers from short strokes near wells.

    Every proposed side must have source strokes covering at least 30% of its
    length, and all four sides together must cover at least half the perimeter.
    This avoids closing arbitrary nearby pipe endpoints.
    """
    empty = Polygon()
    if heat_lines.is_empty or well_footprints.is_empty:
        return empty, {"tested_wells": 0, "hypotheses": 0, "accepted": 0}

    strokes = _strokes(heat_lines, units_per_meter)
    if not strokes:
        return empty, {"tested_wells": 0, "hypotheses": 0, "accepted": 0}
    stroke_lines = [LineString([stroke.start, stroke.end]) for stroke in strokes]
    tree = STRtree(stroke_lines)
    hypotheses: list[tuple[float, Polygon]] = []
    tested_wells = 0
    for well in _polygon_parts(well_footprints):
        if not well.intersects(work_boundary):
            continue
        tested_wells += 1
        x, y = well.centroid.coords[0]
        # A well can sit near one corner of a long, narrow chamber.  Searching
        # only eight metres around it loses the opposite dashed side.
        radius = 14.0 * units_per_meter
        local = [
            strokes[int(index)]
            for index in tree.query(box(x - radius, y - radius, x + radius, y + radius))
        ]
        if len(local) < 8:
            continue
        buckets: dict[int, float] = defaultdict(float)
        for stroke in local:
            buckets[round(stroke.angle / 5) * 5 % 180] += stroke.length
        for bucket, total in buckets.items():
            perpendicular = (bucket + 90) % 180
            if total < 1.5 * units_per_meter or not any(
                _angle_distance(other, perpendicular) <= 5
                and other_total >= 1.5 * units_per_meter
                for other, other_total in buckets.items()
            ):
                continue
            angle = _mean_angle(local, bucket)
            radians = math.radians(angle)
            u = (math.cos(radians), math.sin(radians))
            v = (-u[1], u[0])
            parallel = _runs(local, u, v, angle, units_per_meter)
            cross = _runs(local, v, u, (angle + 90) % 180, units_per_meter)
            for first_index, first in enumerate(parallel):
                for second in parallel[first_index + 1:]:
                    s1, s2 = sorted((first.offset, second.offset))
                    height = s2 - s1
                    if not 2 * units_per_meter <= height <= 15 * units_per_meter:
                        continue
                    for third_index, third in enumerate(cross):
                        for fourth in cross[third_index + 1:]:
                            t1, t2 = sorted((third.offset, fourth.offset))
                            width = t2 - t1
                            if not 2 * units_per_meter <= width <= 15 * units_per_meter:
                                continue
                            if max(width, height) / min(width, height) > 2.2:
                                continue
                            coverage = (
                                _coverage(first, t1, t2),
                                _coverage(second, t1, t2),
                                _coverage(third, s1, s2),
                                _coverage(fourth, s1, s2),
                            )
                            if min(coverage) < 0.3 or sum(coverage) < 2.0:
                                continue
                            corners = [
                                (u[0] * t + v[0] * s, u[1] * t + v[1] * s)
                                for t, s in (
                                    (t1, s1), (t2, s1), (t2, s2), (t1, s2)
                                )
                            ]
                            polygon = Polygon(corners)
                            if polygon.intersection(work_boundary).area < polygon.area * 0.5:
                                continue
                            if well.intersection(
                                polygon.buffer(0.45 * units_per_meter)
                            ).area < 0.1 * units_per_meter**2:
                                continue
                            hypotheses.append((sum(coverage), polygon))

    accepted: list[Polygon] = []
    for _score, polygon in sorted(hypotheses, key=lambda item: -item[0]):
        if any(
            polygon.intersection(other).area
            / min(polygon.area, other.area) > 0.5
            for other in accepted
        ):
            continue
        accepted.append(polygon)
    buffered_hypotheses = _buffered_chambers(
        heat_lines, well_footprints, work_boundary, tree, stroke_lines,
        units_per_meter,
    )
    buffered_accepted: list[Polygon] = []
    buffered_refinements: list[Polygon] = []
    for _score, polygon in sorted(
        buffered_hypotheses, key=lambda item: -item[0]
    ):
        if any(
            polygon.intersection(other).area
            / min(polygon.area, other.area) > 0.5
            for other in buffered_accepted + buffered_refinements
        ):
            continue
        if any(
            polygon.intersection(other).area
            / min(polygon.area, other.area) > 0.5
            for other in accepted
        ):
            # The fitted rectangle is supported by all four dashed sides.
            # A nearby annotation leader can join the buffered outline and
            # create a triangular spur.  Once a rectangle is corroborated by
            # the buffered outline, keep its straight sides and allow only a
            # small drafting tolerance around them.
            matching = max(
                accepted,
                key=lambda other: polygon.intersection(other).area
                / min(polygon.area, other.area),
            )
            corners = list(matching.exterior.coords)
            sides = [
                math.dist(corners[index], corners[index + 1])
                for index in range(2)
            ]
            if max(sides) / min(sides) <= 1.3:
                buffered_refinements.append(
                    matching.buffer(0.05 * units_per_meter, join_style=2)
                )
            else:
                # A longer chamber may genuinely jog; retain its supported
                # outline instead of forcing it into a single rectangle.
                buffered_refinements.append(polygon)
            continue
        accepted.append(polygon)
        buffered_accepted.append(polygon)
    geometry = unary_union(
        accepted + buffered_refinements
    ) if accepted else empty
    return geometry, {
        "tested_wells": tested_wells,
        "hypotheses": len(hypotheses),
        "accepted": len(accepted),
        "buffered_hypotheses": len(buffered_hypotheses),
        "buffered_accepted": len(buffered_accepted),
        "buffered_refinements": len(buffered_refinements),
        "buffered_candidate_bounds": [
            [round(value, 3) for value in polygon.bounds]
            for polygon in buffered_accepted
        ],
        "candidate_bounds": [
            [round(value, 3) for value in polygon.bounds]
            for polygon in accepted
        ],
    }
