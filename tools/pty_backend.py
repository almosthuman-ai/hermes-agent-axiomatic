"""PTY backend seam for Hermes' local terminal process registry.

The process registry owns Hermes' model-facing terminal PTY sessions. This
module keeps the registry talking to a small session-shaped interface while
the actual PTY substrate can remain either upstream Hermes' legacy PTY
implementation or Legion PTY.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import threading
from pathlib import Path
from typing import Any, Optional, Protocol

from tools.environments.local import _find_shell, _sanitize_subprocess_env

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"


class PtySession(Protocol):
    """Session shape consumed by ProcessRegistry."""

    pid: Optional[int]
    exitstatus: Optional[int]

    def isalive(self) -> bool:
        ...

    def read(self, size: int = 4096) -> str | bytes:
        ...

    def write(self, data: str | bytes) -> Any:
        ...

    def resize(self, rows: int, cols: int) -> Any:
        ...

    def sendeof(self) -> Any:
        ...

    def terminate(self, force: bool = False) -> Any:
        ...

    def wait(self) -> Any:
        ...


class PtyBackend(Protocol):
    """Spawner for local PTY sessions."""

    name: str

    def spawn(
        self,
        *,
        command: str,
        cwd: str,
        env_vars: Optional[dict[str, str]],
        rows: int = 30,
        cols: int = 120,
    ) -> PtySession:
        ...


class LegacyPtySession:
    """Adapter around upstream Hermes' direct winpty/ptyprocess handle."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._handle, "pid", None)

    @property
    def exitstatus(self) -> Optional[int]:
        return getattr(self._handle, "exitstatus", None)

    def isalive(self) -> bool:
        return bool(self._handle.isalive())

    def read(self, size: int = 4096) -> str | bytes:
        return self._handle.read(size)

    def write(self, data: str | bytes) -> Any:
        if _IS_WINDOWS and isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        if _IS_WINDOWS and isinstance(data, str):
            data = data.replace("\n", "\r")
        return self._handle.write(data)

    def resize(self, rows: int, cols: int) -> Any:
        if hasattr(self._handle, "setwinsize"):
            return self._handle.setwinsize(rows, cols)
        if hasattr(self._handle, "set_size"):
            return self._handle.set_size(rows, cols)
        return None

    def sendeof(self) -> Any:
        return self._handle.sendeof()

    def terminate(self, force: bool = False) -> Any:
        return self._handle.terminate(force=force)

    def wait(self) -> Any:
        return self._handle.wait()


class LegacyPtyBackend:
    """Existing Hermes PTY behavior, behind the backend seam."""

    name = "legacy"

    def spawn(
        self,
        *,
        command: str,
        cwd: str,
        env_vars: Optional[dict[str, str]],
        rows: int = 30,
        cols: int = 120,
    ) -> PtySession:
        if _IS_WINDOWS:
            from winpty import PtyProcess as _PtyProcessCls
        else:
            from ptyprocess import PtyProcess as _PtyProcessCls

        user_shell = _find_shell()
        pty_env = _sanitize_subprocess_env(os.environ, env_vars)
        pty_env["PYTHONUNBUFFERED"] = "1"
        handle = _PtyProcessCls.spawn(
            [user_shell, "-lic", f"set +m; {command}"],
            cwd=cwd,
            env=pty_env,
            dimensions=(rows, cols),
        )
        return LegacyPtySession(handle)


class LegionPtySession:
    """Adapter from Legion's observation/action surface to Hermes' PTY shape."""

    _EXITED_LIFECYCLES = {"exited", "terminated"}

    def __init__(self, client: Any, transport: Any, runtime: dict[str, Any]) -> None:
        self._client = client
        self._transport = transport
        self._runtime_id = runtime["runtimeId"]
        self.pid = runtime.get("processId")
        self.exitstatus: Optional[int] = None
        self._last_capture_bytes = 0
        self._last_rendered_text = ""
        self._last_lifecycle = runtime.get("lifecycle", "running")
        self._closed = False
        self._client_lock = threading.RLock()

    def isalive(self) -> bool:
        return self._last_lifecycle not in self._EXITED_LIFECYCLES and not self._closed

    def read(self, size: int = 4096) -> str:
        with self._client_lock:
            observation = self._client.observe(self._runtime_id)
        self._last_lifecycle = observation.get("lifecycle", self._last_lifecycle)
        self.pid = observation.get("processId", self.pid)

        text = self._read_capture_delta(observation)
        if text is None:
            text = self._rendered_frame_delta(observation)
        return text[-size:] if size and len(text) > size else text

    def write(self, data: str | bytes) -> Any:
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        if _IS_WINDOWS:
            text = text.replace("\n", "\r")
        with self._client_lock:
            return self._client.raw_input(
                self._runtime_id,
                text,
                submit=False,
                semantic_label="hermes-process-stdin",
            )

    def resize(self, rows: int, cols: int) -> Any:
        with self._client_lock:
            return self._client.resize(
                self._runtime_id,
                {"rows": rows, "cols": cols, "pixelWidth": 0, "pixelHeight": 0},
            )

    def sendeof(self) -> Any:
        with self._client_lock:
            return self._client.request_exit(self._runtime_id)

    def terminate(self, force: bool = False) -> Any:
        try:
            with self._client_lock:
                return self._client.terminate(self._runtime_id)
        finally:
            self._last_lifecycle = "terminated"
            self.exitstatus = -15

    def wait(self) -> Any:
        if not self._closed:
            try:
                with self._client_lock:
                    report = self._client.cleanup(self._runtime_id)
                self.exitstatus = _exitstatus_from_report(report, self.exitstatus)
            finally:
                self._closed = True
                try:
                    self._transport.close()
                except Exception:
                    logger.debug("Legion PTY transport close failed", exc_info=True)
        return self.exitstatus

    def _read_capture_delta(self, observation: dict[str, Any]) -> Optional[str]:
        raw_path = observation.get("rawCapturePath")
        raw_count = int(observation.get("rawByteCount") or 0)
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.exists():
            return None
        try:
            with path.open("rb") as fh:
                fh.seek(self._last_capture_bytes)
                data = fh.read(max(0, raw_count - self._last_capture_bytes))
            self._last_capture_bytes = max(self._last_capture_bytes, raw_count)
            return data.decode("utf-8", errors="replace")
        except OSError:
            logger.debug("Could not read Legion PTY raw capture %s", raw_path, exc_info=True)
            return None

    def _rendered_frame_delta(self, observation: dict[str, Any]) -> str:
        lines = (observation.get("renderedFrame") or {}).get("lines") or []
        text = "\n".join(lines)
        if text.startswith(self._last_rendered_text):
            delta = text[len(self._last_rendered_text):]
        else:
            delta = text
        self._last_rendered_text = text
        return delta


class LegionPtyBackend:
    """Legion PTY host backend for Hermes local PTY sessions."""

    name = "legion"

    def spawn(
        self,
        *,
        command: str,
        cwd: str,
        env_vars: Optional[dict[str, str]],
        rows: int = 30,
        cols: int = 120,
    ) -> PtySession:
        from legion_pty import (
            LegionPtyClient,
            RuntimeLaunchRequest,
            TerminalSize,
        )

        transport = _make_legion_transport()
        client = LegionPtyClient(transport)
        user_shell = _find_shell()
        pty_env = _sanitize_subprocess_env(os.environ, env_vars)
        pty_env["PYTHONUNBUFFERED"] = "1"
        runtime = client.launch(
            RuntimeLaunchRequest(
                adapter_id="hermes",
                launch_profile_id="hermes-terminal-tool",
                argv=[user_shell, "-lic", f"set +m; {command}"],
                env=pty_env,
                cwd=cwd,
                size=TerminalSize(rows=rows, cols=cols),
                evidence_policy_id="hermes-terminal-tool",
            )
        )
        return LegionPtySession(client, transport, runtime)


def select_pty_backend() -> PtyBackend:
    """Resolve the local PTY backend from HERMES_PTY_BACKEND."""

    backend = os.getenv("HERMES_PTY_BACKEND", "").strip().lower()
    if not backend or backend == "legacy":
        return LegacyPtyBackend()
    if backend == "legion":
        return LegionPtyBackend()
    raise ValueError(
        f"Unknown HERMES_PTY_BACKEND={backend!r}; expected 'legacy' or 'legion'"
    )


def _make_legion_transport() -> Any:
    from legion_pty import StdioJsonlTransport

    command = os.getenv("HERMES_LEGION_PTY_COMMAND", "legion-pty")
    args_raw = os.getenv("HERMES_LEGION_PTY_ARGS")
    args = shlex.split(args_raw) if args_raw else ["serve-stdio"]
    cwd = os.getenv("HERMES_LEGION_PTY_CWD") or None
    return StdioJsonlTransport(command, args, cwd=cwd)


def _exitstatus_from_report(report: Any, fallback: Optional[int]) -> Optional[int]:
    if isinstance(report, dict):
        status = str(report.get("status") or "")
        if status.startswith("exited:"):
            try:
                return int(status.split(":", 1)[1])
            except ValueError:
                return fallback
        if "killed" in status or "terminated" in status:
            return -15
    return fallback


__all__ = [
    "LegacyPtyBackend",
    "LegionPtyBackend",
    "PtyBackend",
    "PtySession",
    "select_pty_backend",
]
