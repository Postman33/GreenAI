"""Compatibility boundary for placement validation rules."""

from __future__ import annotations

from ..planting.placement_generator import (
    build_checks,
    configured_existing_tree_clearance_m,
    required_spacing,
    rule_geometry,
    safe_scope,
)

__all__ = [
    "build_checks",
    "configured_existing_tree_clearance_m",
    "required_spacing",
    "rule_geometry",
    "safe_scope",
]
