"""Application readiness state and unresolved resource counts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ReadinessPhase(StrEnum):
    STARTING = "starting"
    RECOVERING = "recovering"
    READY = "ready"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class UnresolvedCounts:
    error_sessions: int = 0
    orphaned_resources: int = 0

    def as_dict(self) -> Mapping[str, int]:
        return {
            "error_sessions": self.error_sessions,
            "orphaned_resources": self.orphaned_resources,
        }


@dataclass(frozen=True)
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

    Transitions are awaited under a lock; reads return a frozen snapshot
    so request handlers and observers cannot observe a torn state.
    """

    def __init__(self) -> None:
        self._state: ReadinessState = ReadinessState()
        self._lock = asyncio.Lock()

    @property
    def phase(self) -> ReadinessPhase:
        return self._state.phase

    @property
    def is_ready(self) -> bool:
        return self._state.phase == ReadinessPhase.READY

    def snapshot(self) -> ReadinessState:
        return self._state

    async def begin_recovery(self) -> None:
        async with self._lock:
            self._state = ReadinessState(phase=ReadinessPhase.RECOVERING)

    async def mark_ready(self) -> None:
        async with self._lock:
            self._state = ReadinessState(phase=ReadinessPhase.READY)

    async def mark_degraded(self, reason: str, unresolved: UnresolvedCounts) -> None:
        async with self._lock:
            self._state = ReadinessState(
                phase=ReadinessPhase.DEGRADED,
                reason=reason,
                unresolved=unresolved,
            )

    async def fail(self, reason: str) -> None:
        async with self._lock:
            self._state = ReadinessState(
                phase=ReadinessPhase.DEGRADED,
                reason=reason,
                unresolved=UnresolvedCounts(),
            )
