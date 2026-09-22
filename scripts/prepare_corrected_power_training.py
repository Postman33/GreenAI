"""Convert a manually corrected model-preview DXF into explicit training labels."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import ezdxf


SUPPORTED_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC"}
TRAINING_LAYER = "Кабель электрический"


def decision(entity, document, yellow_negative: bool = False) -> str:
    """Read a correction while keeping untouched yellow predictions out."""
    color = int(entity.dxf.get("color", 256))
    if color == 3:
        return "positive"
    if color == 7:
        return "negative"
    if color != 256:
        return "skip"

    layer_name = str(entity.dxf.get("layer", ""))
    layer = document.layers.get(layer_name)
    layer_color = abs(int(layer.dxf.get("color", 7)))
    if layer_color == 3:
        return "positive"
    if layer_color == 8:
        return "negative"
    if layer_color == 2 and yellow_negative:
        return "negative"
    return "skip"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--yellow-negative",
        action="store_true",
        help="Treat untouched yellow manual-review geometry as negative.",
    )
    args = parser.parse_args()

    source = ezdxf.readfile(args.input)
    target = ezdxf.new("R2018")
    target.layers.add(TRAINING_LAYER, color=7)
    target_space = target.modelspace()
    counts: Counter[str] = Counter()

    for entity in source.modelspace():
        if entity.dxftype() not in SUPPORTED_TYPES:
            continue
        label = decision(entity, source, yellow_negative=args.yellow_negative)
        counts[label] += 1
        if label == "skip":
            continue
        copied = entity.copy()
        copied.dxf.layer = TRAINING_LAYER
        copied.dxf.color = 3 if label == "positive" else 256
        target_space.add_entity(copied)

    if not counts["positive"] or not counts["negative"]:
        raise ValueError(f"Expected both positive and negative corrections, got {dict(counts)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    target.saveas(args.output)
    report = {
        "input": str(args.input),
        "output": str(args.output),
        "training_layer": TRAINING_LAYER,
        "positive_color": 3,
        "counts": dict(sorted(counts.items())),
        "interpretation": {
            "explicit_green": "positive",
            "explicit_white": "negative",
            "untouched_green_layer": "positive",
            "untouched_gray_layer": "negative",
            "untouched_yellow_layer": (
                "negative" if args.yellow_negative else "excluded"
            ),
        },
    }
    report_path = args.report or args.output.with_suffix(".json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Training DXF: {args.output}")
    print(f"Report: {report_path}")
    print(f"Counts: {dict(counts)}")


if __name__ == "__main__":
    main()
