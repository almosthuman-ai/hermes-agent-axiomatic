from __future__ import annotations

import platform
from pathlib import Path

from tools.pty_backend import LegionPtyBackend, LegionPtySession, select_pty_backend


def test_select_pty_backend_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("HERMES_PTY_BACKEND", raising=False)

    assert select_pty_backend().name == "legacy"


def test_select_pty_backend_legion(monkeypatch):
    monkeypatch.setenv("HERMES_PTY_BACKEND", "legion")

    assert isinstance(select_pty_backend(), LegionPtyBackend)


class _FakeLegionClient:
    def __init__(self, capture_path: Path) -> None:
        self.capture_path = capture_path
        self.raw_inputs = []
        self.cleaned = False

    def observe(self, runtime_id):
        data = self.capture_path.read_bytes()
        return {
            "runtimeId": runtime_id,
            "processId": 123,
            "lifecycle": "running",
            "rawByteCount": len(data),
            "rawCapturePath": str(self.capture_path),
            "renderedFrame": {"lines": [data.decode("utf-8", "replace")]},
        }

    def raw_input(self, runtime_id, text, submit=False, semantic_label=""):
        self.raw_inputs.append((runtime_id, text, submit, semantic_label))

    def cleanup(self, runtime_id):
        self.cleaned = True
        return {"status": "exited:0"}


class _FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_legion_session_reads_raw_capture_deltas(tmp_path):
    capture = tmp_path / "raw.bin"
    capture.write_bytes(b"alpha")
    client = _FakeLegionClient(capture)
    transport = _FakeTransport()
    session = LegionPtySession(
        client,
        transport,
        {"runtimeId": "runtime-1", "processId": 123, "lifecycle": "running"},
    )

    assert session.read() == "alpha"

    capture.write_bytes(b"alpha\nbeta")

    assert session.read() == "\nbeta"


def test_legion_session_writes_raw_input_and_closes_transport(tmp_path):
    capture = tmp_path / "raw.bin"
    capture.write_bytes(b"")
    client = _FakeLegionClient(capture)
    transport = _FakeTransport()
    session = LegionPtySession(
        client,
        transport,
        {"runtimeId": "runtime-1", "processId": 123, "lifecycle": "running"},
    )

    session.write(b"hello\n")
    expected_text = "hello\r" if platform.system() == "Windows" else "hello\n"
    assert client.raw_inputs == [
        ("runtime-1", expected_text, False, "hermes-process-stdin")
    ]

    assert session.wait() == 0
    assert client.cleaned is True
    assert transport.closed is True
