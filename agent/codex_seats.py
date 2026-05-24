"""Codex seat selection for the app-server runtime.

Seats are Codex-owned CODEX_HOME directories. Hermes may choose and pass a
seat home to a Codex subprocess, but it must not read, copy, refresh, or mutate
Codex auth. This module stores only log-safe routing and lease metadata.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


_RETIRE_OUTCOMES = {
    "auth",
    "quota",
    "subprocess-failure",
    "timeout",
    "usage-limit",
}


@dataclass
class CodexSeatSelection:
    seat_id: str
    codex_home: str
    reason: str
    lease_id: str = field(default_factory=lambda: f"codex-seat-{uuid.uuid4().hex[:12]}")
    leased_at: float = field(default_factory=time.time)
    released_at: Optional[float] = None
    outcome: Optional[str] = None
    retired: bool = False

    def to_metadata(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "codex_home": self.codex_home,
            "reason": self.reason,
            "lease_id": self.lease_id,
            "leased_at": self.leased_at,
            "released_at": self.released_at,
            "outcome": self.outcome,
            "retired": self.retired,
        }


class CodexSeatRouter:
    """Small in-memory router for Codex app-server CODEX_HOME seats."""

    def __init__(self) -> None:
        self._retired: set[str] = set()
        self._active: Optional[CodexSeatSelection] = None

    def select(self) -> Optional[CodexSeatSelection]:
        direct = _clean_home(os.getenv("HERMES_CODEX_SEAT_HOME"))
        if direct:
            selection = CodexSeatSelection(
                seat_id=_seat_id_for_home(direct),
                codex_home=direct,
                reason="HERMES_CODEX_SEAT_HOME",
            )
            self._active = selection
            return selection

        homes = _configured_seat_homes()
        for home in homes:
            seat_id = _seat_id_for_home(home)
            if seat_id in self._retired:
                continue
            selection = CodexSeatSelection(
                seat_id=seat_id,
                codex_home=home,
                reason="HERMES_CODEX_SEAT_HOMES",
            )
            self._active = selection
            return selection
        self._active = None
        return None

    def record_outcome(
        self,
        selection: Optional[CodexSeatSelection],
        outcome: str,
    ) -> None:
        if selection is None:
            return
        selection.outcome = outcome
        selection.released_at = time.time()
        if outcome in _RETIRE_OUTCOMES:
            selection.retired = True
            self._retired.add(selection.seat_id)
        if self._active is selection:
            self._active = None


def get_codex_seat_router(agent) -> CodexSeatRouter:
    router = getattr(agent, "_codex_seat_router", None)
    if router is None:
        router = CodexSeatRouter()
        agent._codex_seat_router = router
    return router


def classify_codex_seat_outcome(error: Optional[str]) -> str:
    text = (error or "").lower()
    if any(marker in text for marker in ("usage limit", "usage-limit", "rate limit")):
        return "usage-limit"
    if any(marker in text for marker in ("quota", "capacity")):
        return "quota"
    if any(marker in text for marker in ("auth", "oauth", "login", "unauthorized", "401")):
        return "auth"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "subprocess" in text or "app-server startup failed" in text:
        return "subprocess-failure"
    if error:
        return "turn-error"
    return "retired"


def _configured_seat_homes() -> list[str]:
    raw = os.getenv("HERMES_CODEX_SEAT_HOMES", "")
    if not raw.strip():
        return []
    homes: list[str] = []
    for item in raw.split(os.pathsep):
        home = _clean_home(item)
        if home and home not in homes:
            homes.append(home)
    return homes


def _clean_home(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = os.path.expandvars(os.path.expanduser(value.strip()))
    return value or None


def _seat_id_for_home(codex_home: str) -> str:
    label = os.path.basename(os.path.normpath(codex_home))
    return label or codex_home


__all__ = [
    "CodexSeatRouter",
    "CodexSeatSelection",
    "classify_codex_seat_outcome",
    "get_codex_seat_router",
]
