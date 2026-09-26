"""Stable reader API for DXF extraction and geometry normalization."""

from __future__ import annotations

from .loader import extract
from ..geometry.normalizer import normalize

__all__ = ["extract", "normalize"]
