"""Train and run a utility-axis classifier on vector CAD primitives.

The training drawing uses an explicit AutoCAD colour override as the label:
ACI 3 means a manually verified utility axis.  Objects left BYLAYER are the
negative/reference class.  Colour is only a development label; prediction on
new drawings uses geometry and graph context and does not require colouring.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import matplotlib
import numpy as np
from shapely.geometry import LineString, MultiLineString, mapping
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import precision_recall_fscore_support
from sklearn.model_selection import GroupShuffleSplit, train_test_split

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.plotting import plot_line

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from src.loader import geometry as dxf_geometry, layer_tail  # noqa: E402
from src.normalizer import primitive_to_geometry  # noqa: E402
from utility_detector.onnx_model import (  # noqa: E402
    export_bundle as export_onnx_bundle,
    load_bundle as load_onnx_bundle,
    predict_probabilities as predict_onnx_probabilities,
)


NETWORK_LAYERS = {
    "gas_pipe": {"Сущ_сети_Газопровод", "Газопровод"},
    "water_pipe": {
        "Сущ_сети_Водопровод",
        "Водопровод",
        "Сущ_сети_Промводопровод",
        "Промводопровод",
    },
    "heat_pipe": {"Сущ_сети_Теплосеть", "Теплосеть"},
    "sewer_pipe": {"Сущ_сети_Канализация самотёчная", "Канализация самотёчная"},
    "storm_drain": {"Сущ_сети_Водосток", "Водосток", "Сущ_сети_Дренаж", "Дренаж"},
    "power_cable": {"Сущ_сети_Кабель электрический", "Кабель электрический"},
}
LINE_DXF_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC"}
FEATURE_NAMES = [
    "log_length",
    "straightness",
    "log_span",
    "log_vertex_count",
    "closed",
    "log_bbox_short",
    "log_bbox_long",
    "bbox_aspect",
    "start_degree",
    "end_degree",
    "both_ends_connected",
    "best_continuation",
    "collinear_neighbor_count",
    "short_perpendicular_count",
    "nearby_primitive_count",
    "log_component_length",
    "log_component_span",
    "log_component_size",
    "component_straightness",
]


@dataclass(frozen=True)
class Primitive:
    geometry: LineString
    object_type: str
    dxf_type: str
    source_layer: str
    handle: str | None = None
    label: int | None = None
    dataset_id: str | None = None


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
        left, right = self.find(left), self.find(right)
        if left == right:
            return
        if self.rank[left] < self.rank[right]:
            left, right = right, left
        self.parent[right] = left
        if self.rank[left] == self.rank[right]:
            self.rank[left] += 1


def line_parts(geometry: Any) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        if not geometry.is_empty and len(geometry.coords) >= 2:
            yield geometry
    elif isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            yield from line_parts(part)


def as_line_geometry(raw_record: dict[str, Any], tolerance: float) -> LineString | None:
    geometry = primitive_to_geometry(raw_record, tolerance)
    parts = list(line_parts(geometry))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    merged = linemerge(unary_union(parts))
    merged_parts = list(line_parts(merged))
    return max(merged_parts, key=lambda item: item.length) if merged_parts else None


def network_for_layer(layer: str) -> str | None:
    tail = layer_tail(layer).casefold()
    for object_type, names in NETWORK_LAYERS.items():
        if tail in {name.casefold() for name in names}:
            return object_type
    return None


def load_labeled_dxf(
    path: Path,
    object_types: set[str],
    label_color: int,
    tolerance: float,
) -> dict[str, list[Primitive]]:
    document = ezdxf.readfile(path)
    grouped: dict[str, list[Primitive]] = defaultdict(list)
    for entity in document.modelspace():
        if entity.dxftype() not in LINE_DXF_TYPES:
            continue
        source_layer = str(entity.dxf.get("layer", "0"))
        object_type = network_for_layer(source_layer)
        if object_type not in object_types:
            continue
        raw = {
            "geometry": dxf_geometry(entity),
        }
        if raw["geometry"] is None:
            continue
        geometry = as_line_geometry(raw, tolerance)
        if geometry is None or geometry.is_empty or geometry.length <= 1e-9:
            continue
        grouped[object_type].append(
            Primitive(
                geometry=geometry,
                object_type=object_type,
                dxf_type=entity.dxftype(),
                source_layer=source_layer,
                handle=entity.dxf.get("handle", None),
                label=int(entity.dxf.get("color", 256) == label_color),
                dataset_id=str(path.resolve()),
            )
        )
    return grouped


def load_jsonl(
    path: Path,
    object_types: set[str],
    tolerance: float,
) -> dict[str, list[Primitive]]:
    grouped: dict[str, list[Primitive]] = defaultdict(list)
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            object_type = record.get("object_type")
            if object_type not in object_types:
                continue
            try:
                geometry = as_line_geometry(record, tolerance)
            except (KeyError, TypeError, ValueError):
                continue
            if geometry is None or geometry.is_empty or geometry.length <= 1e-9:
                continue
            grouped[object_type].append(
                Primitive(
                    geometry=geometry,
                    object_type=object_type,
                    dxf_type=str(record.get("dxf_type", "")),
                    source_layer=str(record.get("source_layer", "")),
                    handle=(str(record["handle"]) if record.get("handle") else None),
                    dataset_id=str(path.resolve()),
                )
            )
    return grouped


def endpoints(line: LineString) -> tuple[tuple[float, float], tuple[float, float]]:
    coordinates = list(line.coords)
    return coordinates[0][:2], coordinates[-1][:2]


def outward_vector(line: LineString, at_start: bool) -> tuple[float, float]:
    coordinates = list(line.coords)
    origin = coordinates[0] if at_start else coordinates[-1]
    candidates = coordinates[1:] if at_start else reversed(coordinates[:-1])
    for point in candidates:
        dx, dy = point[0] - origin[0], point[1] - origin[1]
        norm = math.hypot(dx, dy)
        if norm > 1e-9:
            return dx / norm, dy / norm
    return 0.0, 0.0


def chord_vector(line: LineString) -> tuple[float, float]:
    start, end = endpoints(line)
    dx, dy = end[0] - start[0], end[1] - start[1]
    norm = math.hypot(dx, dy)
    return (dx / norm, dy / norm) if norm > 1e-9 else (0.0, 0.0)


def span(line: LineString) -> float:
    minx, miny, maxx, maxy = line.bounds
    return math.hypot(maxx - minx, maxy - miny)


def build_feature_matrix(
    primitives: list[Primitive],
    connect_tolerance: float = 0.25,
    marker_max_length: float = 2.0,
) -> np.ndarray:
    """Describe each primitive using geometry and endpoint graph context."""
    if not primitives:
        return np.empty((0, len(FEATURE_NAMES)), dtype=float)
    dataset_ids = {item.dataset_id for item in primitives}
    if len(dataset_ids) > 1:
        matrix = np.empty((len(primitives), len(FEATURE_NAMES)), dtype=float)
        indices_by_dataset: dict[str | None, list[int]] = defaultdict(list)
        for index, primitive in enumerate(primitives):
            indices_by_dataset[primitive.dataset_id].append(index)
        for indices in indices_by_dataset.values():
            subset = [primitives[index] for index in indices]
            matrix[indices] = build_feature_matrix(
                subset,
                connect_tolerance=connect_tolerance,
                marker_max_length=marker_max_length,
            )
        return matrix
    lines = [item.geometry for item in primitives]
    endpoint_points = []
    endpoint_owner: list[tuple[int, bool]] = []
    from shapely.geometry import Point

    for index, line in enumerate(lines):
        start, end = endpoints(line)
        endpoint_points.extend((Point(start), Point(end)))
        endpoint_owner.extend(((index, True), (index, False)))
    endpoint_tree = STRtree(endpoint_points)
    line_tree = STRtree(lines)
    union = UnionFind(len(lines))
    endpoint_neighbors: list[set[int]] = [set() for _ in endpoint_points]

    for endpoint_index, point in enumerate(endpoint_points):
        owner, _ = endpoint_owner[endpoint_index]
        for raw_other in endpoint_tree.query(point.buffer(connect_tolerance), predicate="intersects"):
            other_endpoint = int(raw_other)
            other_owner, _ = endpoint_owner[other_endpoint]
            if other_owner == owner:
                continue
            endpoint_neighbors[endpoint_index].add(other_owner)
            union.union(owner, other_owner)

    members: dict[int, list[int]] = defaultdict(list)
    for index in range(len(lines)):
        members[union.find(index)].append(index)
    component_values: dict[int, tuple[float, float, int, float]] = {}
    for root, indices in members.items():
        total_length = sum(lines[index].length for index in indices)
        minx = min(lines[index].bounds[0] for index in indices)
        miny = min(lines[index].bounds[1] for index in indices)
        maxx = max(lines[index].bounds[2] for index in indices)
        maxy = max(lines[index].bounds[3] for index in indices)
        component_span = math.hypot(maxx - minx, maxy - miny)
        component_values[root] = (
            total_length,
            component_span,
            len(indices),
            component_span / total_length if total_length else 0.0,
        )

    rows: list[list[float]] = []
    for index, line in enumerate(lines):
        start, end = endpoints(line)
        chord = math.dist(start, end)
        line_span = span(line)
        minx, miny, maxx, maxy = line.bounds
        width, height = maxx - minx, maxy - miny
        short_side, long_side = sorted((width, height))
        start_neighbors = endpoint_neighbors[index * 2]
        end_neighbors = endpoint_neighbors[index * 2 + 1]
        current_vectors = (
            outward_vector(line, True),
            outward_vector(line, False),
        )
        best_continuation = 0.0
        collinear_count = 0
        for endpoint_offset, neighbors in enumerate((start_neighbors, end_neighbors)):
            for other_index in neighbors:
                other = lines[other_index]
                other_start, other_end = endpoints(other)
                point = start if endpoint_offset == 0 else end
                at_other_start = math.dist(point, other_start) <= math.dist(point, other_end)
                other_vector = outward_vector(other, at_other_start)
                dot = current_vectors[endpoint_offset][0] * other_vector[0] + current_vectors[endpoint_offset][1] * other_vector[1]
                continuation = max(0.0, min(1.0, (1.0 - dot) / 2.0))
                best_continuation = max(best_continuation, continuation)
                if continuation >= 0.933:  # approximately 150 degrees
                    collinear_count += 1

        nearby: set[int] = set()
        short_perpendicular = 0
        direction = chord_vector(line)
        search = line.buffer(connect_tolerance, cap_style=2)
        for raw_other in line_tree.query(search, predicate="intersects"):
            other_index = int(raw_other)
            if other_index == index:
                continue
            nearby.add(other_index)
            other = lines[other_index]
            other_direction = chord_vector(other)
            absolute_dot = abs(direction[0] * other_direction[0] + direction[1] * other_direction[1])
            if other.length <= marker_max_length and absolute_dot <= 0.35:
                short_perpendicular += 1

        component_length, component_span, component_size, component_straightness = component_values[union.find(index)]
        rows.append(
            [
                math.log1p(line.length),
                chord / line.length if line.length else 0.0,
                math.log1p(line_span),
                math.log1p(len(line.coords)),
                float(line.is_ring),
                math.log1p(short_side),
                math.log1p(long_side),
                short_side / long_side if long_side else 0.0,
                float(len(start_neighbors)),
                float(len(end_neighbors)),
                float(bool(start_neighbors) and bool(end_neighbors)),
                best_continuation,
                float(collinear_count),
                float(short_perpendicular),
                float(len(nearby)),
                math.log1p(component_length),
                math.log1p(component_span),
                math.log1p(component_size),
                component_straightness,
            ]
        )
    return np.asarray(rows, dtype=float)


def spatial_groups(primitives: list[Primitive], tile_size: float = 100.0) -> np.ndarray:
    groups = []
    for primitive in primitives:
        midpoint = primitive.geometry.interpolate(0.5, normalized=True)
        groups.append(
            f"{primitive.dataset_id or 'dataset'}:"
            f"{math.floor(midpoint.x / tile_size)}:{math.floor(midpoint.y / tile_size)}"
        )
    return np.asarray(groups)


def validation_split(labels: np.ndarray, groups: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    indices = np.arange(len(labels))
    for seed in range(20):
        splitter = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=seed)
        train, test = next(splitter.split(indices, labels, groups))
        if len(set(labels[train])) == 2 and len(set(labels[test])) == 2:
            return train, test
    return train_test_split(indices, test_size=0.25, random_state=42, stratify=labels)


def metrics(labels: np.ndarray, probabilities: np.ndarray, threshold: float) -> dict[str, Any]:
    predictions = probabilities >= threshold
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, predictions, average="binary", zero_division=0
    )
    tp = int(np.sum((labels == 1) & predictions))
    fp = int(np.sum((labels == 0) & predictions))
    fn = int(np.sum((labels == 1) & ~predictions))
    tn = int(np.sum((labels == 0) & ~predictions))
    return {
        "threshold": float(threshold),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
    }


def choose_threshold(labels: np.ndarray, probabilities: np.ndarray, target_recall: float) -> float:
    best: tuple[float, float] | None = None
    for threshold in np.linspace(0.02, 0.98, 193):
        result = metrics(labels, probabilities, float(threshold))
        if result["recall"] + 1e-12 < target_recall:
            continue
        candidate = (result["precision"], float(threshold))
        if best is None or candidate > best:
            best = candidate
    if best is not None:
        return best[1]
    # Fall back to the threshold with the best F2 score if the requested
    # recall cannot be reached on a very small validation region.
    scored = []
    for threshold in np.linspace(0.02, 0.98, 193):
        result = metrics(labels, probabilities, float(threshold))
        precision, recall = result["precision"], result["recall"]
        f2 = 5 * precision * recall / (4 * precision + recall) if precision + recall else 0.0
        scored.append((f2, float(threshold)))
    return max(scored)[1]


def train_models(
    grouped: dict[str, list[Primitive]],
    connect_tolerance: float,
    marker_max_length: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle: dict[str, Any] = {
        "format_version": 1,
        "feature_names": FEATURE_NAMES,
        "connect_tolerance": connect_tolerance,
        "marker_max_length": marker_max_length,
        "models": {},
    }
    report: dict[str, Any] = {"status": "trained", "networks": {}}
    for object_type, primitives in sorted(grouped.items()):
        labels = np.asarray([int(item.label or 0) for item in primitives], dtype=int)
        positives = int(labels.sum())
        negatives = int(len(labels) - positives)
        if positives < 10 or negatives < 10:
            report["networks"][object_type] = {
                "status": "skipped",
                "reason": "at least 10 positive and 10 negative primitives are required",
                "positive_count": positives,
                "negative_count": negatives,
            }
            continue
        features = build_feature_matrix(primitives, connect_tolerance, marker_max_length)
        train_indices, test_indices = validation_split(labels, spatial_groups(primitives))
        validation_model = RandomForestClassifier(
            n_estimators=350,
            max_depth=18,
            min_samples_leaf=2,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=42,
            n_jobs=-1,
        )
        validation_model.fit(features[train_indices], labels[train_indices])
        probabilities = validation_model.predict_proba(features[test_indices])[:, 1]
        accepted_threshold = choose_threshold(labels[test_indices], probabilities, 0.98)
        # Keep a real uncertainty band even when a small validation region
        # cannot reach the stricter recall target at a distinct threshold.
        # Those primitives remain visible for CAD review and are never mixed
        # into either the confirmed axis or annotation classes.
        review_threshold = min(
            accepted_threshold * 0.70,
            choose_threshold(labels[test_indices], probabilities, 0.995),
        )
        final_model = RandomForestClassifier(
            n_estimators=500,
            max_depth=18,
            min_samples_leaf=2,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=42,
            n_jobs=-1,
        )
        final_model.fit(features, labels)
        bundle["models"][object_type] = {
            "classifier": final_model,
            "accepted_threshold": accepted_threshold,
            "review_threshold": review_threshold,
        }
        importances = sorted(
            zip(FEATURE_NAMES, final_model.feature_importances_),
            key=lambda item: item[1],
            reverse=True,
        )
        report["networks"][object_type] = {
            "status": "trained",
            "primitive_count": len(primitives),
            "positive_count": positives,
            "negative_count": negatives,
            "training_tile_count": len(set(spatial_groups(primitives)[train_indices])),
            "validation_tile_count": len(set(spatial_groups(primitives)[test_indices])),
            "accepted_validation": metrics(labels[test_indices], probabilities, accepted_threshold),
            "review_or_accepted_validation": metrics(labels[test_indices], probabilities, review_threshold),
            "top_features": [
                {"name": name, "importance": float(value)} for name, value in importances[:10]
            ],
        }
    if not bundle["models"]:
        raise ValueError("No detector model could be trained")
    return bundle, report


def classify(
    bundle: dict[str, Any],
    grouped: dict[str, list[Primitive]],
) -> tuple[dict[str, list[tuple[Primitive, float, str]]], dict[str, Any]]:
    results: dict[str, list[tuple[Primitive, float, str]]] = defaultdict(list)
    report: dict[str, Any] = {"status": "predicted", "networks": {}}
    for object_type, model_data in bundle["models"].items():
        primitives = grouped.get(object_type, [])
        if not primitives:
            report["networks"][object_type] = {"status": "missing", "primitive_count": 0}
            continue
        features = build_feature_matrix(
            primitives,
            float(bundle["connect_tolerance"]),
            float(bundle["marker_max_length"]),
        )
        probabilities = model_data["classifier"].predict_proba(features)[:, 1]
        accepted_threshold = float(model_data["accepted_threshold"])
        review_threshold = float(model_data["review_threshold"])
        counts: Counter[str] = Counter()
        lengths: Counter[str] = Counter()
        for primitive, probability in zip(primitives, probabilities):
            if probability >= accepted_threshold:
                decision = "accepted"
            elif probability >= review_threshold:
                decision = "manual_review"
            else:
                decision = "rejected"
            results[object_type].append((primitive, float(probability), decision))
            counts[decision] += 1
            lengths[decision] += primitive.geometry.length
        report["networks"][object_type] = {
            "status": "predicted",
            "primitive_count": len(primitives),
            "accepted_threshold": accepted_threshold,
            "review_threshold": review_threshold,
            "counts": dict(counts),
            "lengths_in_dxf_units": {key: float(value) for key, value in lengths.items()},
        }
    return results, report


def classify_onnx(
    runtime_bundle: dict[str, Any],
    grouped: dict[str, list[Primitive]],
) -> tuple[dict[str, list[tuple[Primitive, float, str]]], dict[str, Any]]:
    """Classify primitives with a portable ONNX + JSON bundle."""
    metadata = runtime_bundle["metadata"]
    feature_names = metadata["input"]["feature_names"]
    if feature_names != FEATURE_NAMES:
        raise ValueError("ONNX feature order differs from the current feature builder")
    feature_builder = metadata["feature_builder"]
    connect_tolerance = float(feature_builder["connect_tolerance"])
    marker_max_length = float(feature_builder["marker_max_length"])
    results: dict[str, list[tuple[Primitive, float, str]]] = defaultdict(list)
    report: dict[str, Any] = {"status": "predicted", "networks": {}}
    for object_type, model_data in metadata["networks"].items():
        primitives = grouped.get(object_type, [])
        if not primitives:
            report["networks"][object_type] = {
                "status": "missing",
                "primitive_count": 0,
            }
            continue
        features = build_feature_matrix(
            primitives, connect_tolerance, marker_max_length
        )
        probabilities = predict_onnx_probabilities(
            runtime_bundle, object_type, features
        )
        accepted_threshold = float(model_data["accepted_threshold"])
        review_threshold = float(model_data["review_threshold"])
        counts: Counter[str] = Counter()
        lengths: Counter[str] = Counter()
        for primitive, probability in zip(primitives, probabilities):
            if probability >= accepted_threshold:
                decision = "accepted"
            elif probability >= review_threshold:
                decision = "manual_review"
            else:
                decision = "rejected"
            results[object_type].append((primitive, float(probability), decision))
            counts[decision] += 1
            lengths[decision] += primitive.geometry.length
        report["networks"][object_type] = {
            "status": "predicted",
            "primitive_count": len(primitives),
            "accepted_threshold": accepted_threshold,
            "review_threshold": review_threshold,
            "counts": dict(counts),
            "lengths_in_dxf_units": {
                key: float(value) for key, value in lengths.items()
            },
        }
    return results, report


def decision_metrics(
    classified: list[tuple[Primitive, float, str]],
    positive_decisions: set[str],
) -> dict[str, Any]:
    """Measure primitive counts and lengths against manual DXF colour labels."""
    labels = np.asarray([int(item.label or 0) for item, _, _ in classified], dtype=int)
    predictions = np.asarray(
        [decision in positive_decisions for _, _, decision in classified], dtype=bool
    )
    lengths = np.asarray([item.geometry.length for item, _, _ in classified], dtype=float)
    true_positive = (labels == 1) & predictions
    false_positive = (labels == 0) & predictions
    false_negative = (labels == 1) & ~predictions
    true_negative = (labels == 0) & ~predictions

    tp = int(true_positive.sum())
    fp = int(false_positive.sum())
    fn = int(false_negative.sum())
    tn = int(true_negative.sum())
    tp_length = float(lengths[true_positive].sum())
    fp_length = float(lengths[false_positive].sum())
    fn_length = float(lengths[false_negative].sum())
    tn_length = float(lengths[true_negative].sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    length_precision = tp_length / (tp_length + fp_length) if tp_length + fp_length else 0.0
    length_recall = tp_length / (tp_length + fn_length) if tp_length + fn_length else 0.0
    return {
        "positive_decisions": sorted(positive_decisions),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "length_precision": length_precision,
        "length_recall": length_recall,
        "true_positive_length": tp_length,
        "false_positive_length": fp_length,
        "false_negative_length": fn_length,
        "true_negative_length": tn_length,
    }


def evaluate_classification(
    results: dict[str, list[tuple[Primitive, float, str]]],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "evaluated_on_independent_labeled_dxf",
        "label_definition": "explicit ACI colour from the model bundle",
        "networks": {},
    }
    for object_type, classified in sorted(results.items()):
        decisions = Counter(decision for _, _, decision in classified)
        report["networks"][object_type] = {
            "primitive_count": len(classified),
            "positive_count": sum(item.label == 1 for item, _, _ in classified),
            "negative_count": sum(item.label != 1 for item, _, _ in classified),
            "decisions": dict(decisions),
            "accepted_only": decision_metrics(classified, {"accepted"}),
            "accepted_or_manual_review": decision_metrics(
                classified, {"accepted", "manual_review"}
            ),
        }
    return report


def merged_geometry(items: Iterable[Primitive]):
    lines = [item.geometry for item in items]
    if not lines:
        return MultiLineString([])
    combined = unary_union(lines)
    try:
        return linemerge(combined)
    except ValueError:
        return combined


def write_geojsonl(
    path: Path,
    results: dict[str, list[tuple[Primitive, float, str]]],
    requested_decision: str,
) -> None:
    with path.open("w", encoding="utf-8") as destination:
        for object_type, classified in sorted(results.items()):
            selected = [item for item, _, decision in classified if decision == requested_decision]
            if not selected:
                continue
            probabilities = [probability for _, probability, decision in classified if decision == requested_decision]
            feature = {
                "type": "Feature",
                "properties": {
                    "object_type": object_type,
                    "decision": requested_decision,
                    "reason": "supervised_vector_classifier",
                    "status": "model_requires_cross_drawing_validation",
                    "minimum_probability": min(probabilities),
                    "maximum_probability": max(probabilities),
                    "primitive_count": len(selected),
                    "coordinate_reference": "local_dxf_coordinates",
                },
                "geometry": mapping(merged_geometry(selected)),
            }
            destination.write(json.dumps(feature, ensure_ascii=False) + "\n")


def safe_layer(prefix: str, object_type: str) -> str:
    return f"ML_{prefix}_{object_type}".upper()[:255]


def add_line_geometry(modelspace: Any, geometry: Any, layer: str) -> None:
    for line in line_parts(geometry):
        coordinates = list(line.coords)
        if len(coordinates) >= 2:
            modelspace.add_lwpolyline(coordinates, dxfattribs={"layer": layer})


def write_debug_dxf(
    path: Path,
    results: dict[str, list[tuple[Primitive, float, str]]],
    include_truth: bool = False,
) -> None:
    document = ezdxf.new("R2018")
    modelspace = document.modelspace()
    colors = {"accepted": 3, "manual_review": 2, "rejected": 8}
    for object_type, classified in sorted(results.items()):
        for decision, color in colors.items():
            layer = safe_layer(decision, object_type)
            document.layers.add(layer, color=color)
            for primitive, _, actual_decision in classified:
                if actual_decision == decision:
                    add_line_geometry(modelspace, primitive.geometry, layer)
        if include_truth:
            truth_layer = safe_layer("TRUTH", object_type)
            document.layers.add(truth_layer, color=4)
            for primitive, _, _ in classified:
                if primitive.label == 1:
                    add_line_geometry(modelspace, primitive.geometry, truth_layer)
    document.saveas(path)


def write_validation_debug_dxf(
    path: Path,
    results: dict[str, list[tuple[Primitive, float, str]]],
) -> None:
    """Write mutually exclusive validation outcomes for inspection in CAD."""
    document = ezdxf.new("R2018")
    modelspace = document.modelspace()
    categories = {
        "TP": (3, lambda label, decision: label == 1 and decision == "accepted"),
        "FP": (1, lambda label, decision: label != 1 and decision == "accepted"),
        "REVIEW_TRUE": (
            4,
            lambda label, decision: label == 1 and decision == "manual_review",
        ),
        "REVIEW_FALSE": (
            2,
            lambda label, decision: label != 1 and decision == "manual_review",
        ),
        "FN": (6, lambda label, decision: label == 1 and decision == "rejected"),
        "TN": (8, lambda label, decision: label != 1 and decision == "rejected"),
    }
    for object_type, classified in sorted(results.items()):
        for category, (color, matches) in categories.items():
            layer = safe_layer(category, object_type)
            document.layers.add(layer, color=color)
            for primitive, _, decision in classified:
                if matches(primitive.label, decision):
                    add_line_geometry(modelspace, primitive.geometry, layer)
    document.saveas(path)


def write_prelabeled_dxf(
    source_path: Path,
    output_path: Path,
    results: dict[str, list[tuple[Primitive, float, str]]],
    accepted_color: int = 3,
    review_color: int = 2,
) -> dict[str, int]:
    """Colour predicted source entities in a full DXF copy for human correction."""
    document = ezdxf.readfile(source_path)
    decisions_by_handle = {
        primitive.handle: decision
        for classified in results.values()
        for primitive, _, decision in classified
        if primitive.handle is not None
    }
    counts: Counter[str] = Counter()
    for entity in document.modelspace():
        handle = entity.dxf.get("handle", None)
        decision = decisions_by_handle.get(handle)
        if decision is None:
            continue
        counts[decision] += 1
        if decision == "accepted":
            entity.dxf.color = accepted_color
        elif decision == "manual_review":
            entity.dxf.color = review_color
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.saveas(output_path)
    return dict(counts)


def write_debug_png(
    path: Path,
    results: dict[str, list[tuple[Primitive, float, str]]],
) -> None:
    rows = max(1, len(results))
    figure, axes = plt.subplots(rows, 1, figsize=(10, 7 * rows), dpi=150)
    axes_list = list(getattr(axes, "flat", [axes]))
    for axis, (object_type, classified) in zip(axes_list, sorted(results.items())):
        for decision, color, width, alpha in (
            ("rejected", "#9E9E9E", 0.20, 0.22),
            ("manual_review", "#F9A825", 0.55, 0.75),
            ("accepted", "#00A152", 0.85, 0.95),
        ):
            geometry = merged_geometry(
                item for item, _, actual_decision in classified if actual_decision == decision
            )
            if not geometry.is_empty:
                plot_line(geometry, axis, add_points=False, color=color, linewidth=width, alpha=alpha)
        axis.set_title(object_type)
        axis.set_aspect("equal")
        axis.grid(True, alpha=0.12)
    figure.tight_layout()
    figure.savefig(path, bbox_inches="tight")
    plt.close(figure)


def parse_types(raw: str) -> set[str]:
    result = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = result - set(NETWORK_LAYERS)
    if unknown:
        raise ValueError(f"Unknown network types: {', '.join(sorted(unknown))}")
    return result


def command_train(args: argparse.Namespace) -> None:
    object_types = parse_types(args.types)
    grouped: dict[str, list[Primitive]] = defaultdict(list)
    for input_path in args.input:
        loaded = load_labeled_dxf(
            input_path, object_types, args.label_color, args.curve_tolerance
        )
        for object_type, primitives in loaded.items():
            grouped[object_type].extend(primitives)
    bundle, report = train_models(grouped, args.connect_tolerance, args.marker_max_length)
    verification_features = {
        object_type: build_feature_matrix(
            primitives, args.connect_tolerance, args.marker_max_length
        )
        for object_type, primitives in grouped.items()
    }
    export_onnx_bundle(
        bundle,
        args.model,
        metadata_extra={
            "training_source": [str(path) for path in args.input],
            "label_color": args.label_color,
            "model_role": "utility_primitive_classifier",
        },
        verification_features=verification_features,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    classified, _ = classify(bundle, grouped)
    if args.debug_dxf:
        args.debug_dxf.parent.mkdir(parents=True, exist_ok=True)
        write_debug_dxf(args.debug_dxf, classified, include_truth=True)
    if args.debug_png:
        args.debug_png.parent.mkdir(parents=True, exist_ok=True)
        write_debug_png(args.debug_png, classified)
    print(f"ONNX bundle: {args.model}")
    print(f"Report: {args.report}")
    for object_type, item in report["networks"].items():
        if item["status"] != "trained":
            print(f"  {object_type}: {item['status']}")
            continue
        validation = item["accepted_validation"]
        print(
            f"  {object_type}: positives={item['positive_count']}, "
            f"precision={validation['precision']:.3f}, recall={validation['recall']:.3f}, "
            f"threshold={validation['threshold']:.3f}"
        )


def command_predict(args: argparse.Namespace) -> None:
    bundle = load_onnx_bundle(args.model)
    object_types = set(bundle["metadata"]["networks"])
    grouped = load_jsonl(args.input, object_types, args.curve_tolerance)
    classified, report = classify_onnx(bundle, grouped)
    for path in (args.output, args.review_output, args.rejected_output, args.report):
        path.parent.mkdir(parents=True, exist_ok=True)
    write_geojsonl(args.output, classified, "accepted")
    write_geojsonl(args.review_output, classified, "manual_review")
    write_geojsonl(args.rejected_output, classified, "rejected")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.debug_dxf:
        args.debug_dxf.parent.mkdir(parents=True, exist_ok=True)
        write_debug_dxf(args.debug_dxf, classified)
    if args.debug_png:
        args.debug_png.parent.mkdir(parents=True, exist_ok=True)
        write_debug_png(args.debug_png, classified)
    print(f"Output: {args.output}")
    print(f"Report: {args.report}")
    for object_type, item in report["networks"].items():
        if item["status"] == "predicted":
            print(f"  {object_type}: {item['counts']}")
        else:
            print(f"  {object_type}: {item['status']}")


def command_evaluate(args: argparse.Namespace) -> None:
    bundle = load_onnx_bundle(args.model)
    metadata = bundle["metadata"]
    object_types = set(metadata["networks"])
    label_color = int(
        args.label_color if args.label_color is not None else metadata.get("label_color", 3)
    )
    grouped = load_labeled_dxf(args.input, object_types, label_color, args.curve_tolerance)
    classified, _ = classify_onnx(bundle, grouped)
    report = evaluate_classification(classified)
    report["input"] = str(args.input)
    report["model"] = str(args.model)
    report["label_color"] = label_color
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.debug_dxf:
        args.debug_dxf.parent.mkdir(parents=True, exist_ok=True)
        write_validation_debug_dxf(args.debug_dxf, classified)
    print(f"Validation report: {args.report}")
    if args.debug_dxf:
        print(f"Validation DXF: {args.debug_dxf}")
    for object_type, item in report["networks"].items():
        accepted = item["accepted_only"]
        print(
            f"  {object_type}: positives={item['positive_count']}, "
            f"precision={accepted['precision']:.3f}, recall={accepted['recall']:.3f}, "
            f"f1={accepted['f1']:.3f}"
        )


def command_prelabel(args: argparse.Namespace) -> None:
    bundle = load_onnx_bundle(args.model)
    model_object_types = set(bundle["metadata"]["networks"])
    object_types = parse_types(args.types) if args.types else model_object_types
    missing_model_types = object_types - model_object_types
    if missing_model_types:
        raise ValueError(
            "Model does not contain network types: "
            + ", ".join(sorted(missing_model_types))
        )
    grouped = load_labeled_dxf(args.input, object_types, args.accepted_color, args.curve_tolerance)
    classified, report = classify_onnx(bundle, grouped)
    written_counts = write_prelabeled_dxf(
        args.input,
        args.output,
        classified,
        accepted_color=args.accepted_color,
        review_color=args.review_color,
    )
    report.update(
        {
            "status": "prelabeled_dxf",
            "input": str(args.input),
            "output": str(args.output),
            "accepted_color": args.accepted_color,
            "review_color": args.review_color,
            "written_counts": written_counts,
        }
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Prelabeled DXF: {args.output}")
    print(f"Report: {args.report}")
    for object_type, item in report["networks"].items():
        print(f"  {object_type}: {item.get('counts', item.get('status'))}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    train = subparsers.add_parser("train", help="train from an explicitly coloured DXF")
    train.add_argument("input", type=Path, nargs="+")
    train.add_argument("--types", default="gas_pipe,water_pipe")
    train.add_argument("--label-color", type=int, default=3)
    train.add_argument(
        "--model", type=Path, required=True, help="output ONNX bundle directory"
    )
    train.add_argument("--report", type=Path, required=True)
    train.add_argument("--debug-dxf", type=Path)
    train.add_argument("--debug-png", type=Path)
    train.add_argument("--curve-tolerance", type=float, default=0.1)
    train.add_argument("--connect-tolerance", type=float, default=0.25)
    train.add_argument("--marker-max-length", type=float, default=2.0)
    train.set_defaults(handler=command_train)

    predict = subparsers.add_parser("predict", help="classify extracted JSONL primitives")
    predict.add_argument("input", type=Path)
    predict.add_argument("--model", type=Path, required=True)
    predict.add_argument("--output", type=Path, required=True)
    predict.add_argument("--review-output", type=Path, required=True)
    predict.add_argument("--rejected-output", type=Path, required=True)
    predict.add_argument("--report", type=Path, required=True)
    predict.add_argument("--debug-dxf", type=Path)
    predict.add_argument("--debug-png", type=Path)
    predict.add_argument("--curve-tolerance", type=float, default=0.1)
    predict.set_defaults(handler=command_predict)

    evaluate = subparsers.add_parser(
        "evaluate", help="evaluate a saved model on a separately labelled DXF"
    )
    evaluate.add_argument("input", type=Path)
    evaluate.add_argument("--model", type=Path, required=True)
    evaluate.add_argument("--report", type=Path, required=True)
    evaluate.add_argument("--debug-dxf", type=Path)
    evaluate.add_argument("--label-color", type=int)
    evaluate.add_argument("--curve-tolerance", type=float, default=0.1)
    evaluate.set_defaults(handler=command_evaluate)

    prelabel = subparsers.add_parser(
        "prelabel", help="colour model predictions in a full DXF copy for manual correction"
    )
    prelabel.add_argument("input", type=Path)
    prelabel.add_argument("--model", type=Path, required=True)
    prelabel.add_argument("--output", type=Path, required=True)
    prelabel.add_argument("--report", type=Path, required=True)
    prelabel.add_argument("--accepted-color", type=int, default=3)
    prelabel.add_argument("--review-color", type=int, default=2)
    prelabel.add_argument(
        "--types",
        help="Optional comma-separated network types to prelabel, for example heat_pipe",
    )
    prelabel.add_argument("--curve-tolerance", type=float, default=0.1)
    prelabel.set_defaults(handler=command_prelabel)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
