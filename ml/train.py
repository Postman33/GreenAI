"""Train and version utility-network models from DXFs in ml/train."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from utility_detector.detector import (  # noqa: E402
    Primitive,
    build_feature_matrix,
    load_labeled_dxf,
    train_models,
)
from utility_detector.onnx_model import export_bundle as export_onnx_bundle  # noqa: E402


NETWORK_DIRECTORIES = {
    "gas_pipe": "gas",
    "water_pipe": "water",
    "heat_pipe": "heat",
    "power_cable": "power",
}
DEFAULT_TRAIN_ROOT = Path(__file__).resolve().parent / "train"
DEFAULT_OUTPUT_ROOT = WORKSPACE / "models" / "utility_detector"


def parse_types(value: str) -> list[str]:
    requested = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(requested) - set(NETWORK_DIRECTORIES))
    if unknown:
        raise argparse.ArgumentTypeError(
            "Unknown network type(s): " + ", ".join(unknown)
        )
    if not requested:
        raise argparse.ArgumentTypeError("At least one network type is required")
    return requested


def training_paths(directory: Path) -> list[Path]:
    paths = sorted(
        (path.resolve() for path in directory.glob("*.dxf") if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    if not paths:
        raise FileNotFoundError(f"No training DXF files were found in: {directory}")
    return paths


def load_training_set(
    paths: list[Path],
    object_type: str,
    label_color: int,
    curve_tolerance: float,
) -> tuple[list[Primitive], list[dict[str, Any]]]:
    combined: list[Primitive] = []
    file_reports: list[dict[str, Any]] = []
    for path in paths:
        loaded = load_labeled_dxf(path, {object_type}, label_color, curve_tolerance)
        primitives = loaded.get(object_type, [])
        positives = sum(item.label == 1 for item in primitives)
        negatives = len(primitives) - positives
        if not primitives:
            raise ValueError(f"No {object_type} primitives were found in: {path}")
        if positives == 0:
            raise ValueError(
                f"No explicit ACI {label_color} {object_type} labels were found in: {path}"
            )
        if negatives == 0:
            raise ValueError(f"No negative {object_type} examples were found in: {path}")
        combined.extend(primitives)
        file_reports.append(
            {
                "path": str(path),
                "primitive_count": len(primitives),
                "positive_count": positives,
                "negative_count": negatives,
                "layers": sorted({item.source_layer for item in primitives}),
            }
        )
        print(
            f"  {path.name}: primitives={len(primitives)}, "
            f"positive={positives}, negative={negatives}"
        )
    return combined, file_reports


def version_name(explicit: str | None) -> str:
    if explicit:
        if any(character in explicit for character in '<>:"/\\|?*'):
            raise ValueError("Version contains a character forbidden in a Windows path")
        return explicit
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-root", type=Path, default=DEFAULT_TRAIN_ROOT)
    parser.add_argument(
        "--types",
        type=parse_types,
        default=list(NETWORK_DIRECTORIES),
        help="Comma-separated types: gas_pipe,water_pipe,heat_pipe,power_cable",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--version")
    parser.add_argument("--label-color", type=int, default=3)
    parser.add_argument("--curve-tolerance", type=float, default=0.1)
    parser.add_argument("--connect-tolerance", type=float, default=0.25)
    parser.add_argument("--marker-max-length", type=float, default=2.0)
    args = parser.parse_args()

    train_root = (
        args.train_root if args.train_root.is_absolute() else WORKSPACE / args.train_root
    ).resolve()
    output_root = (
        args.output_root if args.output_root.is_absolute() else WORKSPACE / args.output_root
    ).resolve()
    output_directory = output_root / version_name(args.version)
    if output_directory.exists():
        raise FileExistsError(f"Model version already exists: {output_directory}")

    grouped: dict[str, list[Primitive]] = defaultdict(list)
    reports_by_type: dict[str, list[dict[str, Any]]] = {}
    sources_by_type: dict[str, list[str]] = {}
    for object_type in args.types:
        directory = train_root / NETWORK_DIRECTORIES[object_type]
        paths = training_paths(directory)
        lock_files = sorted(directory.glob("*.dwl*"))
        if lock_files:
            print(
                f"WARNING: CAD lock files found in {directory}; "
                "save and close drawings before final training:"
            )
            for path in lock_files:
                print(f"  {path.name}")

        print(f"{object_type} training directory: {directory}")
        primitives, file_reports = load_training_set(
            paths, object_type, args.label_color, args.curve_tolerance
        )
        grouped[object_type].extend(primitives)
        reports_by_type[object_type] = file_reports
        sources_by_type[object_type] = [str(path) for path in paths]

    bundle, training_report = train_models(
        grouped, args.connect_tolerance, args.marker_max_length
    )
    all_sources = [
        path for object_type in args.types for path in sources_by_type[object_type]
    ]
    bundle.update(
        {
            "training_source": all_sources,
            "training_sources_by_type": sources_by_type,
            "label_color": args.label_color,
            "created_at": datetime.now().astimezone().isoformat(),
            "model_role": "utility_primitive_classifier",
        }
    )
    training_report["training_files"] = reports_by_type

    verification_features = {
        object_type: build_feature_matrix(
            primitives, args.connect_tolerance, args.marker_max_length
        )
        for object_type, primitives in grouped.items()
    }
    metadata = export_onnx_bundle(
        bundle,
        output_directory,
        metadata_extra={
            "training_source": all_sources,
            "training_sources_by_type": sources_by_type,
            "label_color": args.label_color,
            "model_role": "utility_primitive_classifier",
        },
        verification_features=verification_features,
    )
    training_report["onnx_verification"] = {
        object_type: network["verification"]
        for object_type, network in metadata["networks"].items()
    }
    report_path = output_directory / "training_report.json"
    report_path.write_text(
        json.dumps(training_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    output_root.mkdir(parents=True, exist_ok=True)
    latest_path = output_root / "latest"
    latest_path.mkdir(parents=True, exist_ok=True)
    for stale_model in latest_path.glob("*.onnx"):
        stale_model.unlink()
    for object_type, network in metadata["networks"].items():
        shutil.copy2(
            output_directory / network["model_file"],
            latest_path / network["model_file"],
        )
    shutil.copy2(output_directory / "metadata.json", latest_path / "metadata.json")
    shutil.copy2(report_path, latest_path / "training_report.json")
    (output_root / "latest.json").write_text(
        json.dumps(
            {
                "version": output_directory.name,
                "bundle": str(output_directory),
                "metadata": str(output_directory / "metadata.json"),
                "training_sources_by_type": sources_by_type,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"ONNX bundle: {output_directory}")
    print(f"Latest ONNX bundle: {latest_path}")
    for object_type, network in sorted(training_report["networks"].items()):
        validation = network["accepted_validation"]
        print(
            f"{object_type}: positives={network['positive_count']}, "
            f"negatives={network['negative_count']}, "
            f"internal precision={validation['precision']:.3f}, "
            f"internal recall={validation['recall']:.3f}"
        )


if __name__ == "__main__":
    main()
