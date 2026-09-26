"""Stable domain contracts shared by the planting pipeline.

The domain package contains data structures only.  It deliberately does not
know about DXF, GeoJSON, databases, CLI output, or plotting.
"""

from .models import PlantingProfile, PlantingSelection

__all__ = ["PlantingProfile", "PlantingSelection"]
