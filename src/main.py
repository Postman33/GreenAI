"""Extract semantic CAD objects from a bound DXF using ``core/config.yaml``.

The command writes JSON Lines. It does not modify the input DXF and does not
yet merge line segments or build normative buffers.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

import ezdxf
import yaml


DEFAULT_CONFIG = Path(__file__).parent / "core" / "config.yaml"


def json_value(value: Any) -> Any:
    """Convert ezdxf vectors and nested values to JSON-compatible values."""
    if hasattr(value, "x") and hasattr(value, "y"):
        result = [value.x, value.y]
        if hasattr(value, "z"):
            result.append(value.z)
        return result
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    return value


def layer_tail(layer_name: str) -> str:
    """Return the source layer name after the last Bind namespace marker."""
    return layer_name.rsplit("$0$", maxsplit=1)[-1]


def geometry(entity: Any) -> dict[str, Any] | None:
    """Extract the geometry needed by the next geometry-normalization step."""
    entity_type = entity.dxftype()

    if entity_type == "LINE":
        return {"kind": "line", "start": entity.dxf.start, "end": entity.dxf.end}
    if entity_type == "POINT":
        return {"kind": "point", "location": entity.dxf.location}
    if entity_type == "CIRCLE":
        return {
            "kind": "circle",
            "center": entity.dxf.center,
            "radius": entity.dxf.radius,
        }
    if entity_type == "ARC":
        return {
            "kind": "arc",
            "center": entity.dxf.center,
            "radius": entity.dxf.radius,
            "start_angle": entity.dxf.start_angle,
            "end_angle": entity.dxf.end_angle,
        }
    if entity_type == "ELLIPSE":
        return {
            "kind": "ellipse",
            "center": entity.dxf.center,
            "major_axis": entity.dxf.major_axis,
            "ratio": entity.dxf.ratio,
            "start_param": entity.dxf.start_param,
            "end_param": entity.dxf.end_param,
        }
    if entity_type == "LWPOLYLINE":
        return {
            "kind": "polyline",
            "closed": entity.closed,
            "points": list(entity.get_points("xyseb")),
        }
    if entity_type == "POLYLINE":
        return {
            "kind": "polyline",
            "closed": entity.is_closed,
            "points": [vertex.dxf.location for vertex in entity.vertices],
        }
    if entity_type == "INSERT":
        return {
            "kind": "insert_point",
            "location": entity.dxf.insert,
            "block_name": entity.dxf.name,
        }
    if entity_type == "HATCH":
        # Hatch loops will be converted to polygons in the geometry module.
        # Saving basic attributes preserves traceability without pretending the
        # hatch is already a usable polygon.
        return {
            "kind": "hatch",
            "solid_fill": entity.dxf.get("solid_fill", 0),
            "pattern_name": entity.dxf.get("pattern_name", None),
        }
    return None


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict) or "layer_mapping" not in config:
        raise ValueError("Config must contain a 'layer_mapping' object.")
    return config


def mapping_matches(mapping: dict[str, Any], entity: Any, source: str) -> bool:
    if mapping["source"] != source:
        return False
    if entity.dxftype() not in mapping["dxf_types"]:
        return False

    source_layer = entity.dxf.get("layer", "0")
    normalized_tail = layer_tail(source_layer).casefold()
    normalized_layer = source_layer.casefold()

    allowed_tails = [name.casefold() for name in mapping.get("layer_tail_in", [])]
    if allowed_tails and normalized_tail not in allowed_tails:
        return False

    layer_pattern = mapping.get("layer_regex")
    if layer_pattern and not re.search(layer_pattern, source_layer, flags=re.IGNORECASE):
        return False

    excluded = [item.casefold() for item in mapping.get("exclude_layer_contains", [])]
    return not any(item in normalized_layer for item in excluded)


def match_mappings(
    mappings: dict[str, dict[str, Any]], entity: Any, source: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    for object_name, mapping in mappings.items():
        if mapping_matches(mapping, entity, source):
            yield object_name, mapping


def walk_virtual_entities(
    insert: Any,
    block_path: list[str],
    max_depth: int,
) -> Iterator[tuple[Any, list[str]]]:
    """Yield transformed geometry nested in a selected geobase INSERT."""
    if len(block_path) >= max_depth:
        return

    try:
        for child in insert.virtual_entities():
            child_path = [*block_path, child.dxf.name] if child.dxftype() == "INSERT" else block_path
            yield child, child_path
            if child.dxftype() == "INSERT":
                yield from walk_virtual_entities(child, child_path, max_depth)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Could not expand block %s: %s", insert.dxf.name, error
        )


def record(
    object_name: str,
    mapping: dict[str, Any],
    entity: Any,
    block_path: list[str],
) -> dict[str, Any]:
    source_layer = entity.dxf.get("layer", "0")
    return json_value(
        {
            "object_type": object_name,
            "semantic_type": mapping["semantic_type"],
            "target_geometry": mapping["geometry"],
            "conversion": mapping["conversion"],
            "source_layer": source_layer,
            "source_layer_tail": layer_tail(source_layer),
            "dxf_type": entity.dxftype(),
            "handle": entity.dxf.get("handle", None),
            "block_path": block_path,
            "geometry": geometry(entity),
        }
    )


def extract(
    input_path: Path,
    config: dict[str, Any],
    output_path: Path,
) -> tuple[Counter[str], Counter[str], set[str]]:
    """Extract modelspace and selected geobase-block objects to JSONL."""
    mappings = config["layer_mapping"]
    block_rules = config.get("geobase_blocks", {})
    block_patterns = [re.compile(item, flags=re.IGNORECASE) for item in block_rules["name_regex"]]
    max_depth = int(block_rules.get("max_depth", 12))

    # ezdxf warns while copying unsupported AutoCAD FIELD/DIMASSOC objects
    # during virtual block expansion. They are not requested by the config and
    # do not affect the extracted geometry.
    logging.getLogger("ezdxf").setLevel(logging.ERROR)

    doc = ezdxf.readfile(input_path)
    modelspace = doc.modelspace()
    counts: Counter[str] = Counter()
    dxf_counts: Counter[str] = Counter()
    selected_roots: set[str] = set()

    def write_matches(entity: Any, source: str, path: list[str]) -> None:
        for object_name, mapping in match_mappings(mappings, entity, source):
            item = record(object_name, mapping, entity, path)
            if item["geometry"] is None:
                continue
            output.write(json.dumps(item, ensure_ascii=False) + "\n")
            counts[object_name] += 1
            dxf_counts[entity.dxftype()] += 1

    with output_path.open("w", encoding="utf-8") as output:
        for entity in modelspace:
            write_matches(entity, "modelspace", [])

            if entity.dxftype() != "INSERT":
                continue
            block_name = entity.dxf.name
            if not any(pattern.search(block_name) for pattern in block_patterns):
                continue
            selected_roots.add(block_name)
            for child, path in walk_virtual_entities(entity, [block_name], max_depth):
                if child.dxftype() != "INSERT":
                    write_matches(child, "geobase_blocks", path)

    required_without_records = {
        name
        for name, mapping in mappings.items()
        if mapping.get("required") and counts[name] == 0
    }
    return counts, dxf_counts, required_without_records


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract configured semantic objects from a bound DXF file."
    )
    parser.add_argument("input_dxf", type=Path, help="Path to input bound DXF")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="Path to YAML layer mapping"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("extracted_objects.jsonl"),
        help="Output JSONL path",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    counts, dxf_counts, missing_required = extract(args.input_dxf, config, args.output)

    print(f"Read: {args.input_dxf}")
    print(f"Output: {args.output}")
    print("Extracted objects:")
    for object_name, count in sorted(counts.items()):
        print(f"  {object_name}: {count}")

    # A missing optional type must not stop the run: different streets may
    # simply have no gas, overhead lines, trees, and so on. It is still
    # important to surface the fact, because a wrong layer name in the
    # configuration produces the same symptom.
    missing_configured = [
        object_name
        for object_name in config["layer_mapping"]
        if counts[object_name] == 0
    ]
    for object_name in missing_configured:
        print(f"WARNING: configured object type was not found: {object_name}")

    print("DXF types:")
    for entity_type, count in sorted(dxf_counts.items()):
        print(f"  {entity_type}: {count}")
    if missing_required:
        raise SystemExit(
            "Required object types were not found: " + ", ".join(sorted(missing_required))
        )


if __name__ == "__main__":
    main()
