"""Input/output boundary for CAD pipeline integrations.

This is the only public entry point new callers need for the legacy readers
and exporters.  The underlying modules remain import-compatible during the
migration.
"""

def __getattr__(name: str):
    """Load public functions lazily so CLI submodules stay warning-free."""
    if name == "extract":
        from .loader import extract
        return extract
    if name == "normalize":
        from ..geometry.normalizer import normalize
        return normalize
    if name == "export_zones":
        from .dxf_exporter import export_zones
        return export_zones
    raise AttributeError(name)

__all__ = ["extract", "normalize", "export_zones"]
