"""Evaluate a trained utility model on independent labeled DXF files."""

from __future__ import annotations

import argparse
import json
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
    classify_onnx,
    evaluate_classification,
    load_labeled_dxf,
    write_validation_debug_dxf,
)
from utility_detector.onnx_model import load_bundle as load_onnx_bundle  # noqa: E402


NETWORK_DIRECTORIES = {
    "gas_pipe": "gas",
    "water_pipe": "water",
    "heat_pipe": "heat",
    "power_cable": "power",
}
DEFAULT_MODEL = WORKSPACE / "models" / "utility_detector" / "latest"
DEFAULT_OUTPUT_ROOT = WORKSPACE / "output" / "ml_validation"


def validation_paths(directory: Path) -> list[Path]:
    paths = sorted(
        (path.resolve() for path in directory.glob("*.dxf") if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    if not paths:
        raise FileNotFoundError(f"No validation DXF files were found in: {directory}")
    return paths


def safe_stem(path: Path) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in path.stem
    )


def version_name(explicit: str | None) -> str:
    if explicit:
        if any(character in explicit for character in '<>:"/\\|?*'):
            raise ValueError("Version contains a character forbidden in a Windows path")
        return explicit
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def load_validation_file(
    path: Path,
    object_type: str,
    label_color: int,
    curve_tolerance: float,
) -> dict[str, list[Primitive]]:
    grouped = load_labeled_dxf(path, {object_type}, label_color, curve_tolerance)
    primitives = grouped.get(object_type, [])
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
    print(
        f"  {path.name}: primitives={len(primitives)}, "
        f"positive={positives}, negative={negatives}"
    )
    return grouped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--type", choices=sorted(NETWORK_DIRECTORIES), default="water_pipe"
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--version")
    parser.add_argument("--label-color", type=int)
    parser.add_argument("--curve-tolerance", type=float, default=0.1)
    args = parser.parse_args()

    model_path = (
        args.model if args.model.is_absolute() else WORKSPACE / args.model
    ).resolve()
    default_data_directory = (
        Path(__file__).resolve().parent
        / "validate"
        / NETWORK_DIRECTORIES[args.type]
    )
    data_directory = args.data_dir or default_data_directory
    validation_directory = (
        data_directory if data_directory.is_absolute() else WORKSPACE / data_directory
    ).resolve()
    output_root = (
        args.output_root if args.output_root.is_absolute() else WORKSPACE / args.output_root
    ).resolve()
    output_directory = output_root / NETWORK_DIRECTORIES[args.type] / version_name(
        args.version
    )

    if not model_path.exists():
        raise FileNotFoundError(f"ONNX bundle was not found: {model_path}")
    if output_directory.exists():
        raise FileExistsError(f"Validation version already exists: {output_directory}")

    bundle = load_onnx_bundle(model_path)
    metadata: dict[str, Any] = bundle["metadata"]
    if args.type not in metadata.get("networks", {}):
        raise ValueError(f"The model bundle does not contain a {args.type} model")
    label_color = args.label_color
    if label_color is None:
        label_color = int(metadata.get("label_color", 3))

    paths = validation_paths(validation_directory)
    training_sources = {
        str(Path(path).resolve()).casefold()
        for path in metadata.get("training_source", [])
    }
    overlap = [path for path in paths if str(path).casefold() in training_sources]
    if overlap:
        raise ValueError(
            "Validation includes a training DXF: " + ", ".join(str(path) for path in overlap)
        )

    lock_files = sorted(validation_directory.glob("*.dwl*"))
    if lock_files:
        print("WARNING: CAD lock files found; save and close drawings before validation:")
        for path in lock_files:
            print(f"  {path.name}")

    print(f"Model: {model_path}")
    print(f"Network type: {args.type}")
    print(f"Validation directory: {validation_directory}")
    output_directory.mkdir(parents=True)

    combined: dict[str, list[Primitive]] = defaultdict(list)
    file_reports: list[dict[str, Any]] = []
    for path in paths:
        grouped = load_validation_file(
            path, args.type, label_color, args.curve_tolerance
        )
        classified, _ = classify_onnx(bundle, grouped)
        report = evaluate_classification(classified)
        network = report["networks"][args.type]
        report.update(
            {
                "input": str(path),
                "model": str(model_path),
                "network_type": args.type,
                "label_color": label_color,
            }
        )
        stem = safe_stem(path)
        report_path = output_directory / f"{stem}_report.json"
        debug_path = output_directory / f"{stem}_debug.dxf"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_validation_debug_dxf(debug_path, classified)
        file_reports.append(
            {
                "input": str(path),
                "report": str(report_path),
                "debug_dxf": str(debug_path),
                "metrics": network,
            }
        )
        combined[args.type].extend(grouped[args.type])

    combined_classified, _ = classify_onnx(bundle, combined)
    aggregate = evaluate_classification(combined_classified)
    aggregate.update(
        {
            "model": str(model_path),
            "network_type": args.type,
            "label_color": label_color,
            "validation_files": file_reports,
        }
    )
    aggregate_path = output_directory / "validation_report.json"
    aggregate_path.write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    metrics = aggregate["networks"][args.type]
    accepted = metrics["accepted_only"]
    with_review = metrics["accepted_or_manual_review"]
    print(f"Report: {aggregate_path}")
    print(
        "Accepted only: "
        f"precision={accepted['precision']:.3f}, recall={accepted['recall']:.3f}, "
        f"f1={accepted['f1']:.3f}"
    )
    print(
        "Accepted + manual review: "
        f"precision={with_review['precision']:.3f}, "
        f"recall={with_review['recall']:.3f}, f1={with_review['f1']:.3f}"
    )


if __name__ == "__main__":
    main()
