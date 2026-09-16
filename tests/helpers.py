from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import mapping


def feature(object_type: str, geometry: Any, **properties: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"object_type": object_type, **properties},
        "geometry": mapping(geometry),
    }


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def raw_polyline(
    object_type: str,
    coordinates: list[tuple[float, float]],
    *,
    closed: bool = False,
    layer: str = "TEST",
) -> dict[str, Any]:
    return {
        "object_type": object_type,
        "source_layer": layer,
        "source_layer_tail": layer,
        "dxf_type": "LWPOLYLINE",
        "geometry": {
            "kind": "polyline",
            "closed": closed,
            "points": [[x, y, 0, 0, 0] for x, y in coordinates],
        },
    }


def raw_hatch(
    coordinates: list[tuple[float, float]], *, layer: str
) -> dict[str, Any]:
    ring = coordinates if coordinates[0] == coordinates[-1] else [*coordinates, coordinates[0]]
    return {
        "source_layer": layer,
        "source_layer_tail": layer,
        "dxf_type": "HATCH",
        "geometry": {
            "kind": "hatch",
            "solid_fill": 1,
            "pattern_name": "SOLID",
            "boundary_paths": [[[x, y] for x, y in ring]],
        },
    }

