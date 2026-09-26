"""Small geometry helpers with no project-specific policy."""

from __future__ import annotations

from typing import Any, Iterator

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon


def polygon_parts(geometry: Any) -> Iterator[Polygon]:
    """Yield polygon components without changing or repairing the geometry."""

    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from polygon_parts(part)
