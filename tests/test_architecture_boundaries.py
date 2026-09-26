"""Regression tests for the public package boundaries introduced by the refactor."""

from pathlib import Path

from src.cad_io import export_zones, extract, normalize
from src.domain import PlantingProfile, PlantingSelection
from src.geometry import polygon_parts
from src.pipeline import PipelineArtifacts, StageStatus
from src.planting import plan
from src.rules import build_checks, required_spacing


def test_public_boundaries_are_importable():
    assert callable(extract)
    assert callable(normalize)
    assert callable(export_zones)
    assert callable(plan)
    assert callable(build_checks)
    assert callable(required_spacing)
    assert callable(polygon_parts)


def test_domain_models_are_shared_by_legacy_modules():
    from src.planting.placement_generator import PlantingProfile as LegacyProfile
    from src.planting.service import PlantingSelection as LegacySelection

    assert LegacyProfile is PlantingProfile
    assert LegacySelection is PlantingSelection


def test_artifact_paths_are_canonical(tmp_path: Path):
    artifacts = PipelineArtifacts(tmp_path)
    assert artifacts.constraint_map == tmp_path / "constraint_map.geojsonl"
    assert artifacts.plant_allow_zones == tmp_path / "plant_allow_zones.geojsonl"
    assert artifacts.planting_overlay == tmp_path / "planting_overlay.dxf"
    assert StageStatus.CACHED.value == "cached"
