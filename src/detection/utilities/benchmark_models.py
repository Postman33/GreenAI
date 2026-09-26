"""Compare tabular classifiers for water-pipe primitive recognition."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .detector import build_feature_matrix, load_labeled_dxf, metrics


def load_dataset(paths: list[Path], object_type: str, label_color: int):
    primitives = []
    for path in paths:
        primitives.extend(load_labeled_dxf(path, {object_type}, label_color, 0.1)[object_type])
    features = build_feature_matrix(primitives)
    labels = np.asarray([int(item.label or 0) for item in primitives], dtype=int)
    return features, labels


def best_at_recall(labels: np.ndarray, probabilities: np.ndarray, target: float) -> dict[str, Any] | None:
    candidates = []
    for threshold in np.linspace(0.01, 0.99, 197):
        result = metrics(labels, probabilities, float(threshold))
        if result["recall"] >= target:
            candidates.append(result)
    return max(candidates, key=lambda item: (item["precision"], item["threshold"])) if candidates else None


def models() -> dict[str, Any]:
    return {
        "random_forest": RandomForestClassifier(
            n_estimators=500,
            max_depth=18,
            min_samples_leaf=2,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=42,
            n_jobs=-1,
        ),
        "extra_trees": ExtraTreesClassifier(
            n_estimators=500,
            max_depth=24,
            min_samples_leaf=2,
            max_features="sqrt",
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        ),
        "hist_gradient_boosting": HistGradientBoostingClassifier(
            learning_rate=0.06,
            max_iter=350,
            max_leaf_nodes=31,
            min_samples_leaf=20,
            l2_regularization=1.0,
            class_weight="balanced",
            random_state=42,
        ),
        "logistic_regression": make_pipeline(
            StandardScaler(),
            LogisticRegression(
                max_iter=2000,
                class_weight="balanced",
                random_state=42,
            ),
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("train", type=Path, nargs="+")
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--object-type", default="water_pipe")
    parser.add_argument("--label-color", type=int, default=3)
    args = parser.parse_args()

    train_x, train_y = load_dataset(args.train, args.object_type, args.label_color)
    validation_x, validation_y = load_dataset(
        [args.validation], args.object_type, args.label_color
    )
    report: dict[str, Any] = {
        "object_type": args.object_type,
        "training_files": [str(path) for path in args.train],
        "validation_file": str(args.validation),
        "training_count": len(train_y),
        "training_positive_count": int(train_y.sum()),
        "validation_count": len(validation_y),
        "validation_positive_count": int(validation_y.sum()),
        "models": {},
    }
    for name, model in models().items():
        started = time.perf_counter()
        model.fit(train_x, train_y)
        fit_seconds = time.perf_counter() - started
        started = time.perf_counter()
        probabilities = model.predict_proba(validation_x)[:, 1]
        predict_seconds = time.perf_counter() - started
        report["models"][name] = {
            "average_precision": float(average_precision_score(validation_y, probabilities)),
            "roc_auc": float(roc_auc_score(validation_y, probabilities)),
            "fit_seconds": fit_seconds,
            "predict_seconds": predict_seconds,
            "at_recall_100": best_at_recall(validation_y, probabilities, 1.0),
            "at_recall_995": best_at_recall(validation_y, probabilities, 0.995),
            "at_recall_98": best_at_recall(validation_y, probabilities, 0.98),
            "at_recall_95": best_at_recall(validation_y, probabilities, 0.95),
        }
        selected = report["models"][name]["at_recall_995"]
        print(
            f"{name}: AP={report['models'][name]['average_precision']:.3f}, "
            f"precision@recall>=99.5%={selected['precision']:.3f} "
            f"(recall={selected['recall']:.3f})"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
