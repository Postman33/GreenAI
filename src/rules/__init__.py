"""Public rule-engine API.

The implementation is still hosted in the placement module for compatibility;
new orchestration code should depend on this boundary instead of importing
implementation details directly.
"""

from .placement_checks import (
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
