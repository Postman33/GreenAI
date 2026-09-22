"""Create a compact semantic manifest by streaming one ASCII DXF."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ezdxf.entities import factory
from ezdxf.filemanagement import dxf_file_info
from ezdxf.lldxf.extendedtags import ExtendedTags
from ezdxf.lldxf.tagger import ascii_tags_loader, tag_compiler
from ezdxf.lldxf.tagwriter import TagCollector


SUBENTITY_TYPES = {"ATTRIB", "SEQEND", "VERTEX"}


def stable(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 12)
    if isinstance(value, bytes):
        return value.hex()
    return str(value) if not isinstance(value, (str, int)) else value


def semantic_fingerprint(
    entity_type: str, tags: Iterable[Any], dxfversion: str
) -> str:
    source_tags = list(tags)
    loadable_tags = []
    extension_dictionary_tags = []
    inside_extension_dictionary = False
    for tag in source_tags:
        if tag.code == 102 and str(tag.value) == "{ACAD_XDICTIONARY":
            inside_extension_dictionary = True
            extension_dictionary_tags.append(tag)
            continue
        if inside_extension_dictionary:
            extension_dictionary_tags.append(tag)
            if tag.code == 102 and str(tag.value) == "}":
                inside_extension_dictionary = False
            continue
        loadable_tags.append(tag)
    try:
        entity = factory.load(ExtendedTags(loadable_tags), doc=None)
        collector = TagCollector(dxfversion=dxfversion)
        entity.export_dxf(collector)
        canonical_tags: Iterable[Any] = [
            *collector.tags,
            *extension_dictionary_tags,
        ]
    except (AssertionError, TypeError, ValueError, AttributeError, NotImplementedError):
        # Preserve strict raw comparison for unsupported custom entity types.
        canonical_tags = source_tags
    canonical_tags = list(canonical_tags)
    extrusion_tags = {tag.code: tag.value for tag in canonical_tags if tag.code in {210, 220, 230}}
    default_ellipse_extrusion = (
        entity_type == "ELLIPSE"
        and float(extrusion_tags.get(210, 0.0)) == 0.0
        and float(extrusion_tags.get(220, 0.0)) == 0.0
        and float(extrusion_tags.get(230, 1.0)) == 1.0
    )
    digest = hashlib.sha256()
    for tag in canonical_tags:
        if tag.code == 330:
            continue
        if (
            entity_type == "LWPOLYLINE"
            and tag.code == 43
            and float(tag.value) == 0.0
        ):
            continue
        if entity_type == "MTEXT" and (
            (tag.code == 73 and int(tag.value) == 1)
            or (tag.code == 44 and float(tag.value) == 1.0)
        ):
            continue
        if (
            entity_type in {"INSERT", "ATTRIB", "SEQEND"}
            and tag.code == 280
            and int(tag.value) == 0
        ):
            continue
        if entity_type == "VIEWPORT" and tag.code == 292 and int(tag.value) == 1:
            continue
        if default_ellipse_extrusion and tag.code in {210, 220, 230}:
            # ezdxf omits an explicit default extrusion on save. The ellipse
            # still lies in the same plane, so this is not a geometry change.
            continue
        digest.update(str(tag.code).encode("ascii"))
        digest.update(b"\0")
        digest.update(repr(stable(tag.value)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def first_value(tags: list[Any], code: int, default: Any = None) -> Any:
    for tag in tags:
        if tag.code == code:
            return tag.value
    return default


def green_ai_id(tags: list[Any]) -> str | None:
    in_green_ai = False
    for tag in tags:
        if tag.code == 1001:
            in_green_ai = str(tag.value) == "GREEN_AI"
            continue
        if in_green_ai and tag.code == 1000:
            value = str(tag.value)
            if value.startswith("id="):
                return value[3:]
    return None


def last_value(tags: list[Any], code: int, default: Any = None) -> Any:
    for tag in reversed(tags):
        if tag.code == code:
            return tag.value
    return default


def owner_value(tags: list[Any]) -> str:
    inside_application_group = False
    for tag in tags:
        if tag.code == 102 and str(tag.value).startswith("{"):
            inside_application_group = True
            continue
        if tag.code == 102 and str(tag.value) == "}":
            inside_application_group = False
            continue
        if not inside_application_group and tag.code == 330:
            return str(tag.value)
    return ""


def entity_record(
    entity_type: str, tags: list[Any], dxfversion: str
) -> dict[str, Any]:
    layer = str(first_value(tags, 8, "0"))
    record = {
        "handle": str(first_value(tags, 5, "")),
        "type": entity_type,
        "layer": layer,
        "owner": owner_value(tags),
        "fingerprint": semantic_fingerprint(entity_type, tags, dxfversion),
    }
    if layer.startswith("GREEN_AI_") and entity_type in {
        "CIRCLE",
        "HATCH",
        "LWPOLYLINE",
    }:
        record["green_ai_id"] = green_ai_id(tags)
    return record


def build_manifest(path: Path) -> dict[str, Any]:
    info = dxf_file_info(path)
    entities: list[dict[str, Any]] = []
    layer_states: dict[str, dict[str, bool]] = {}
    section = ""
    pending_section = False
    current_type: str | None = None
    current_tags: list[Any] = []
    modelspace_owner = ""

    def flush() -> None:
        nonlocal current_type, current_tags, modelspace_owner
        if current_type is None:
            return
        if section == "ENTITIES":
            entities.append(
                entity_record(current_type, current_tags, info.version)
            )
        elif section == "TABLES" and current_type == "LAYER":
            name = str(first_value(current_tags, 2, ""))
            if name.startswith("GREEN_AI_"):
                flags = int(first_value(current_tags, 70, 0))
                color = int(first_value(current_tags, 62, 7))
                layer_states[name] = {
                    "is_off": color < 0,
                    "is_frozen": bool(flags & 1),
                }
        elif section == "TABLES" and current_type == "BLOCK_RECORD":
            if str(first_value(current_tags, 2, "")).casefold() == "*model_space":
                modelspace_owner = str(first_value(current_tags, 5, ""))
        current_type = None
        current_tags = []

    with path.open("rt", encoding=info.encoding, errors="surrogateescape") as stream:
        for tag in tag_compiler(ascii_tags_loader(stream)):
            if pending_section:
                pending_section = False
                if tag.code == 2:
                    section = str(tag.value)
                continue
            if tag.code == 0 and tag.value == "SECTION":
                flush()
                pending_section = True
                section = ""
                continue
            if tag.code == 0 and tag.value == "ENDSEC":
                flush()
                section = ""
                continue
            if section in {"ENTITIES", "TABLES"} and tag.code == 0:
                flush()
                current_type = str(tag.value)
                current_tags = [tag]
                continue
            if current_type is not None:
                current_tags.append(tag)
        flush()

    return {
        "path": str(path.resolve()),
        "dxf_version": info.version,
        "encoding": info.encoding,
        "insert_units": int(info.insert_units),
        "modelspace_owner_handle": modelspace_owner,
        "modelspace_entity_count": sum(
            record["type"] not in SUBENTITY_TYPES
            and record["owner"] == modelspace_owner
            for record in entities
        ),
        "raw_entity_record_count": len(entities),
        "entities": entities,
        "green_ai_layer_states": layer_states,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dxf", type=Path)
    parser.add_argument("output_json", type=Path)
    args = parser.parse_args()
    manifest = build_manifest(args.input_dxf)
    args.output_json.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
