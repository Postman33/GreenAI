"""Names of canonical pipeline artifacts.

Centralising paths prevents individual stages from inventing slightly
different filenames and makes cache invalidation explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PipelineArtifacts:
    root: Path

    def path(self, name: str) -> Path:
        return self.root / name

    @property
    def extracted_objects(self) -> Path:
        return self.path("extracted_objects.jsonl")

    @property
    def normalized_objects(self) -> Path:
        return self.path("normalized_objects.geojsonl")

    @property
    def constraint_map(self) -> Path:
        return self.path("constraint_map.geojsonl")

    @property
    def plant_allow_zones(self) -> Path:
        return self.path("plant_allow_zones.geojsonl")

    @property
    def planting_plan(self) -> Path:
        return self.path("planting_plan.geojsonl")

    @property
    def planting_overlay(self) -> Path:
        return self.path("planting_overlay.dxf")

