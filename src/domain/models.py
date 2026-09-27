"""Core data contracts for planting planning.

Keeping these contracts independent from readers and exporters gives the
pipeline a single vocabulary while preserving the existing JSON interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlantingProfile:
    """Catalog and spacing parameters for one plant species."""

    plant_type: str
    species: str
    spacing_m: float
    footprint_radius_m: float
    symbol_radius_m: float
    avoid_other_plantings_m: float
    max_count: int
    catalog_reference: str
    selection_reasons: tuple[str, ...]
    # A species-specific design choice, separate from utility setback rules.
    understory_trunk_clearance_m: float | None = None
    allow_under_tree_canopy: bool = False
    existing_tree_clearance_m: float | None = None
    footprint_boundary: str = "allow_zone"


@dataclass(frozen=True)
class PlantingSelection:
    """One user or preset request to place a plant type in an area."""

    request_id: str
    plant_type: str
    species: str
    mode: str
    area: Any | None
    points: tuple[Any, ...]
    spacing_m: float | None
    max_count: int | None
    selection_reasons: tuple[str, ...]
    catalog_reference: str | None
    design_style: str = "auto"
    guide: Any | None = None
    band_width_m: float = 1.5
    row_count: int = 1
    seed: int = 0
    composition: tuple[dict[str, Any], ...] = ()
