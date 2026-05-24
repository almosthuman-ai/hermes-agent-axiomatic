from __future__ import annotations

import os

from agent.codex_seats import CodexSeatRouter, classify_codex_seat_outcome


def test_direct_env_seat_selection(monkeypatch, tmp_path):
    seat_home = tmp_path / "codex-seat-a"
    monkeypatch.setenv("HERMES_CODEX_SEAT_HOME", str(seat_home))
    monkeypatch.delenv("HERMES_CODEX_SEAT_HOMES", raising=False)

    selection = CodexSeatRouter().select()

    assert selection is not None
    assert selection.codex_home == str(seat_home)
    assert selection.seat_id == "codex-seat-a"
    assert selection.reason == "HERMES_CODEX_SEAT_HOME"


def test_usage_limit_retirement_selects_next_configured_seat(monkeypatch, tmp_path):
    seat_a = tmp_path / "seat-a"
    seat_b = tmp_path / "seat-b"
    monkeypatch.delenv("HERMES_CODEX_SEAT_HOME", raising=False)
    monkeypatch.setenv(
        "HERMES_CODEX_SEAT_HOMES",
        os.pathsep.join([str(seat_a), str(seat_b)]),
    )
    router = CodexSeatRouter()

    first = router.select()
    router.record_outcome(first, "usage-limit")
    second = router.select()

    assert first is not None
    assert first.retired is True
    assert second is not None
    assert second.codex_home == str(seat_b)


def test_codex_seat_outcome_classification():
    assert classify_codex_seat_outcome("usage limit reached") == "usage-limit"
    assert classify_codex_seat_outcome("turn timed out after 10s") == "timeout"
    assert classify_codex_seat_outcome("401 unauthorized") == "auth"
    assert classify_codex_seat_outcome("subprocess exited") == "subprocess-failure"
