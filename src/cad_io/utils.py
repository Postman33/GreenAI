"""Compatibility exports for code that previously imported ``src.utils``."""

from .loader import extract, match_mappings, record, walk_virtual_entities

__all__ = ["extract", "match_mappings", "record", "walk_virtual_entities"]
