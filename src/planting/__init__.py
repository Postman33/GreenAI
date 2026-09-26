"""Public planting-planner API.

CLI scripts may continue importing the legacy modules, while new integrations
should use this package.  The implementation remains in the existing modules
until each stage is moved behind a stable contract.
"""

from ..domain.models import PlantingProfile, PlantingSelection


def __getattr__(name: str):
    """Load the service lazily so ``python -m src.planting.service`` is clean."""
    if name in {"plan", "load_profiles", "load_zones"}:
        from .api import load_profiles, load_zones, plan
        return {"plan": plan, "load_profiles": load_profiles, "load_zones": load_zones}[name]
    raise AttributeError(name)

__all__ = [
    "PlantingProfile",
    "PlantingSelection",
    "plan",
    "load_profiles",
    "load_zones",
]
