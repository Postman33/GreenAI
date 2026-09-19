"""Portable ONNX storage and inference for utility classifiers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import onnx
import onnxruntime as ort
import sklearn
import skl2onnx
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType


FORMAT_NAME = "green-cad-utility-onnx-bundle"
FORMAT_VERSION = 1
DEFAULT_OPSET = 18


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probability_output(
    outputs: list[np.ndarray], class_count: int, positive_column: int
) -> np.ndarray:
    for output in outputs:
        values = np.asarray(output)
        if values.ndim == 2 and values.shape[1] == class_count:
            return values[:, positive_column].astype(float)
    raise ValueError("ONNX model did not return a probability tensor")


def export_bundle(
    training_bundle: dict[str, Any],
    output_directory: Path,
    metadata_extra: dict[str, Any] | None = None,
    verification_features: dict[str, np.ndarray] | None = None,
    opset: int = DEFAULT_OPSET,
) -> dict[str, Any]:
    """Write classifiers and all runtime settings without pickle serialization."""
    if output_directory.exists():
        raise FileExistsError(f"ONNX bundle already exists: {output_directory}")
    feature_names = list(training_bundle["feature_names"])
    output_directory.mkdir(parents=True)
    networks: dict[str, Any] = {}

    for object_type, model_data in sorted(training_bundle["models"].items()):
        classifier = model_data["classifier"]
        feature_count = int(classifier.n_features_in_)
        if feature_count != len(feature_names):
            raise ValueError(
                f"{object_type}: classifier expects {feature_count} features, "
                f"metadata contains {len(feature_names)}"
            )
        classes = [
            value.item() if hasattr(value, "item") else value
            for value in classifier.classes_
        ]
        if 1 not in classes:
            raise ValueError(f"{object_type}: classifier has no positive class 1")
        positive_column = classes.index(1)
        model = convert_sklearn(
            classifier,
            initial_types=[("features", FloatTensorType([None, feature_count]))],
            options={id(classifier): {"zipmap": False}},
            target_opset=opset,
        )
        onnx.checker.check_model(model)
        model_path = output_directory / f"{object_type}.onnx"
        model_path.write_bytes(model.SerializeToString())

        session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        accepted_threshold = float(model_data["accepted_threshold"])
        review_threshold = float(model_data["review_threshold"])
        verification: dict[str, Any] = {
            "status": "structure_checked",
            "providers": session.get_providers(),
        }
        matrix = (verification_features or {}).get(object_type)
        if matrix is not None and len(matrix):
            features = np.asarray(matrix, dtype=np.float32)
            expected = classifier.predict_proba(features)[:, positive_column]
            outputs = session.run(None, {session.get_inputs()[0].name: features})
            actual = probability_output(outputs, len(classes), positive_column)
            absolute_error = np.abs(expected - actual)
            verification.update(
                {
                    "status": "probabilities_compared",
                    "sample_count": int(len(features)),
                    "maximum_absolute_error": float(absolute_error.max(initial=0.0)),
                    "mean_absolute_error": float(absolute_error.mean()),
                    "accepted_decisions_match": bool(
                        np.array_equal(
                            expected >= accepted_threshold,
                            actual >= accepted_threshold,
                        )
                    ),
                    "review_decisions_match": bool(
                        np.array_equal(
                            expected >= review_threshold,
                            actual >= review_threshold,
                        )
                    ),
                }
            )

        networks[object_type] = {
            "model_file": model_path.name,
            "sha256": sha256(model_path),
            "classes": classes,
            "positive_class": 1,
            "positive_probability_column": positive_column,
            "accepted_threshold": accepted_threshold,
            "review_threshold": review_threshold,
            "threshold_policy": model_data.get("threshold_policy", "unspecified"),
            "verification": verification,
        }

    metadata: dict[str, Any] = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now().astimezone().isoformat(),
        "onnx_opset": opset,
        "input": {
            "name": "features",
            "dtype": "float32",
            "shape": [None, len(feature_names)],
            "feature_names": feature_names,
        },
        "feature_builder": {
            "implementation": "utility_detector.detector.build_feature_matrix",
            "connect_tolerance": float(training_bundle["connect_tolerance"]),
            "marker_max_length": float(training_bundle["marker_max_length"]),
        },
        "networks": networks,
        "library_versions": {
            "scikit_learn": sklearn.__version__,
            "skl2onnx": skl2onnx.__version__,
            "onnx": onnx.__version__,
            "onnxruntime": ort.__version__,
            "numpy": np.__version__,
        },
    }
    if metadata_extra:
        metadata.update(metadata_extra)
    metadata_path = output_directory / "metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metadata


def metadata_path(model: Path) -> Path:
    path = model.resolve()
    if path.is_dir():
        path = path / "metadata.json"
    if not path.is_file():
        raise FileNotFoundError(f"ONNX metadata was not found: {path}")
    return path


def load_bundle(model: Path, verify_hashes: bool = True) -> dict[str, Any]:
    path = metadata_path(model)
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata.get("format") != FORMAT_NAME:
        raise ValueError(f"Unsupported ONNX bundle format: {metadata.get('format')}")
    if metadata.get("format_version") != FORMAT_VERSION:
        raise ValueError(
            f"Unsupported ONNX bundle version: {metadata.get('format_version')}"
        )
    sessions: dict[str, ort.InferenceSession] = {}
    for object_type, network in metadata.get("networks", {}).items():
        model_path = path.parent / network["model_file"]
        if not model_path.is_file():
            raise FileNotFoundError(f"ONNX model was not found: {model_path}")
        if verify_hashes and sha256(model_path) != network["sha256"]:
            raise ValueError(f"ONNX checksum mismatch: {model_path}")
        sessions[object_type] = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
    if not sessions:
        raise ValueError("ONNX bundle does not contain any networks")
    return {
        "metadata_path": path,
        "metadata": metadata,
        "sessions": sessions,
    }


def predict_probabilities(
    runtime_bundle: dict[str, Any], object_type: str, features: np.ndarray
) -> np.ndarray:
    metadata = runtime_bundle["metadata"]
    network = metadata["networks"][object_type]
    session = runtime_bundle["sessions"][object_type]
    matrix = np.asarray(features, dtype=np.float32)
    outputs = session.run(None, {session.get_inputs()[0].name: matrix})
    return probability_output(
        outputs,
        len(network["classes"]),
        int(network["positive_probability_column"]),
    )
