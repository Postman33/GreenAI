"""Print every modelspace entity from a DXF file as JSON Lines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

import ezdxf


def json_value(value: Any) -> Any:
    """Convert ezdxf values, including vectors, to JSON-compatible values."""
    if hasattr(value, "x") and hasattr(value, "y"):
        result = [value.x, value.y]
        if hasattr(value, "z"):
            result.append(value.z)
        return result
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def geometry(entity: Any) -> dict[str, Any]:
    """Extract the geometry needed for a first inspection of common entities."""
    entity_type = entity.dxftype()

    if entity_type == "LINE":
        return {"start": entity.dxf.start, "end": entity.dxf.end}
    if entity_type == "POINT":
        return {"location": entity.dxf.location}
    if entity_type == "CIRCLE":
        return {"center": entity.dxf.center, "radius": entity.dxf.radius}
    if entity_type == "ARC":
        return {
            "center": entity.dxf.center,
            "radius": entity.dxf.radius,
            "start_angle": entity.dxf.start_angle,
            "end_angle": entity.dxf.end_angle,
        }
    if entity_type == "LWPOLYLINE":
        return {
            "closed": entity.closed,
            "points": list(entity.get_points("xyseb")),
        }
    if entity_type == "POLYLINE":
        return {
            "closed": entity.is_closed,
            "points": [vertex.dxf.location for vertex in entity.vertices],
        }
    if entity_type == "INSERT":
        return {
            "block_name": entity.dxf.name,
            "insert": entity.dxf.insert,
            "rotation": entity.dxf.get("rotation", 0),
            "xscale": entity.dxf.get("xscale", 1),
            "yscale": entity.dxf.get("yscale", 1),
        }
    if entity_type in {"TEXT", "MTEXT"}:
        return {"text": entity.plain_text() if entity_type == "MTEXT" else entity.dxf.text}

    return {}


def entity_record(entity: Any, *, block_path: list[str] | None = None, parent_insert: str | None = None) -> dict[str, Any]:
    record = {
        "handle": entity.dxf.get("handle", None),
        "type": entity.dxftype(),
        "layer": entity.dxf.get("layer", "0"),
        "dxf_attributes": entity.dxfattribs(),
        "geometry": geometry(entity),
    }
    if block_path:
        record["block_path"] = block_path
        record["parent_insert_handle"] = parent_insert
    return json_value(record)


def records_for_entity(
    entity: Any,
    *,
    block_path: list[str] | None = None,
    parent_insert: str | None = None,
    max_block_depth: int = 20,
) -> Iterator[dict[str, Any]]:
    """Return an entity and all geometry nested in its INSERT blocks.

    ``virtual_entities()`` applies the INSERT transform, so the returned child
    geometry is already positioned in the drawing coordinate system.
    """
    yield entity_record(entity, block_path=block_path, parent_insert=parent_insert)
    if entity.dxftype() != "INSERT" or len(block_path or []) >= max_block_depth:
        return

    block_name = entity.dxf.name
    current_path = [*(block_path or []), block_name]
    insert_handle = entity.dxf.get("handle", None)

    try:
        virtual_entities = entity.virtual_entities()
        for child in virtual_entities:
            yield from records_for_entity(
                child,
                block_path=current_path,
                parent_insert=insert_handle,
                max_block_depth=max_block_depth,
            )
    except Exception as error:  # Keep inspection running for malformed blocks.
        yield {
            "type": "BLOCK_EXPANSION_ERROR",
            "parent_insert_handle": insert_handle,
            "block_path": current_path,
            "error": str(error),
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write every DXF modelspace entity to a JSON Lines file."
    )
    parser.add_argument("input_dxf", type=Path, help="Path to the input DXF file")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("objects.jsonl"),
        help="Output JSONL file (default: objects.jsonl)",
    )
    parser.add_argument(
        "--expand-blocks",
        action="store_true",
        help="Also write geometry contained in INSERT blocks.",
    )
    args = parser.parse_args()

    doc = ezdxf.readfile(args.input_dxf)
    modelspace = doc.modelspace()

    entity_count = 0
    record_count = 0
    with args.output.open("w", encoding="utf-8") as output:
        for entity in modelspace:
            entity_count += 1
            records = (
                records_for_entity(entity)
                if args.expand_blocks
                else (entity_record(entity),)
            )
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                record_count += 1

    print(f"Read: {args.input_dxf}")
    print(f"Modelspace entities: {entity_count}")
    print(f"Records written: {record_count}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
