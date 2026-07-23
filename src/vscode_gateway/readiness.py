"""Application readiness state and unresolved resource counts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ReadinessPhase(StrEnum):
    STARTING = "starting"
    RECOVERING = "recovering"
    READY = "ready"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class UnresolvedCounts:
    error_sessions: int = 0
    orphaned_resources: int = 0

    def as_dict(self) -> Mapping[str, int]:
        return {
            "error_sessions": self.error_sessions,
            "orphaned_resources": self.orphaned_resources,
        }


_EMPTY_UNRESOLVED_COUNTS = UnresolvedCounts()


@dataclass(frozen=True, slots=True)
class ReadinessState:
    phase: ReadinessPhase = ReadinessPhase.STARTING
    reason: str = ""
    unresolved: UnresolvedCounts = field(default_factory=UnresolvedCounts)

    def as_response_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"phase": self.phase.value}
        if self.reason:
            data["reason"] = self.reason
        if self.phase != ReadinessPhase.READY:
            data["unresolved"] = dict(self.unresolved.as_dict())
        return data


class Readiness:
    """Single-process readiness gate.

    Transitions replace the complete frozen snapshot so request handlers and
    observers cannot observe a torn state.
    """

    def __init__(self) -> None:
        self._state: ReadinessState = ReadinessState()

    @property
    def phase(self) -> ReadinessPhase:
        return self._state.phase

    @property
    def is_ready(self) -> bool:
        return self._state.phase == ReadinessPhase.READY

    def snapshot(self) -> ReadinessState:
        return self._state

    def begin_recovery(self) -> None:
        self._state = ReadinessState(phase=ReadinessPhase.RECOVERING)

    def mark_ready(self) -> None:
        self._state = ReadinessState(phase=ReadinessPhase.READY)

    def mark_degraded(
        self, reason: str, unresolved: UnresolvedCounts = _EMPTY_UNRESOLVED_COUNTS
    ) -> None:
        self._state = ReadinessState(
            phase=ReadinessPhase.DEGRADED,
            reason=reason,
            unresolved=unresolved,
        )

    def fail(self, reason: str) -> None:
        self.mark_degraded(reason)
