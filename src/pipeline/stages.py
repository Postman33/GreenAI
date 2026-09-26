"""Small shared types for stage timing and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StageStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    CACHED = "cached"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class PipelineStage:
    stage_id: str
    name: str


@dataclass(frozen=True)
class StageResult:
    stage: PipelineStage
    status: StageStatus
    elapsed_seconds: float = 0.0
    artifacts: tuple[str, ...] = ()
    details: dict[str, Any] | None = None
