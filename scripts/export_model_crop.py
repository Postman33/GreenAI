"""Export a small spatial crop of one utility model prediction to DXF/PNG."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from shapely.geometry import box


WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from utility_detector.detector import (  # noqa: E402
    classify_onnx,
    line_parts,
    load_jsonl,
    write_debug_dxf,
    write_debug_png,
)
from utility_detector.onnx_model import load_bundle  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Extracted object JSONL")
    parser.add_argument("--model", type=Path, default=Path("models/utility_detector/latest"))
    parser.add_argument("--type", default="power_cable")
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=("MIN_X", "MIN_Y", "MAX_X", "MAX_Y"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--curve-tolerance", type=float, default=0.1)
    args = parser.parse_args()

    bundle = load_bundle(args.model)
    if args.type not in bundle["metadata"].get("networks", {}):
        raise ValueError(f"Model bundle has no {args.type!r} network")

    grouped = load_jsonl(args.input, {args.type}, args.curve_tolerance)
    classified, model_report = classify_onnx(bundle, grouped)
    crop = box(*args.bbox)
    cropped = []
    for primitive, probability, decision in classified.get(args.type, []):
        clipped = primitive.geometry.intersection(crop)
        for part in line_parts(clipped):
            cropped.append((replace(primitive, geometry=part), probability, decision))

    if not cropped:
        raise ValueError("The requested crop contains no classified primitives")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_debug_dxf(args.output, {args.type: cropped})
    if args.png:
        args.png.parent.mkdir(parents=True, exist_ok=True)
        write_debug_png(args.png, {args.type: cropped})

    counts = Counter(decision for _, _, decision in cropped)
    probabilities = [probability for _, probability, _ in cropped]
    report = {
        "input": str(args.input),
        "model": str(args.model),
        "object_type": args.type,
        "bbox": args.bbox,
        "counts": dict(sorted(counts.items())),
        "minimum_probability": min(probabilities),
        "maximum_probability": max(probabilities),
        "model_report": model_report.get("networks", {}).get(args.type),
        "layers": {
            f"ML_ACCEPTED_{args.type}".upper(): "green, accepted",
            f"ML_MANUAL_REVIEW_{args.type}".upper(): "yellow, requires review",
            f"ML_REJECTED_{args.type}".upper(): "gray, rejected as annotation/noise",
        },
    }
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"DXF: {args.output}")
    if args.png:
        print(f"PNG: {args.png}")
    print(f"Report: {args.report}")
    print(f"Counts: {dict(counts)}")


if __name__ == "__main__":
    main()
