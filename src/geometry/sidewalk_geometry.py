"""Keep sidewalk setbacks tied to geometry near the work boundary."""

from __future__ import annotations

from typing import Any

from shapely.ops import unary_union


def relevant_sidewalk_geometry(
    raw_sidewalk: Any | None,
    reconstructed_sidewalk: Any | None,
    work_area: Any | None,
    setback_in_dxf_units: float,
) -> Any | None:
    """Merge mapped sidewalk faces with raw geometry near the work area."""
    sources = []
    if reconstructed_sidewalk is not None and not reconstructed_sidewalk.is_empty:
        sources.append(reconstructed_sidewalk)
    if raw_sidewalk is not None and not raw_sidewalk.is_empty:
        if work_area is not None and not work_area.is_empty:
            raw_sidewalk = raw_sidewalk.intersection(
                work_area.buffer(max(0.0, setback_in_dxf_units))
            )
        else:
            raw_sidewalk = None
        if raw_sidewalk is not None and not raw_sidewalk.is_empty:
            sources.append(raw_sidewalk)
    return unary_union(sources) if sources else None
