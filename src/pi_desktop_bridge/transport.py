"""Persistent, bounded request/response transport to the remote desktop agent."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from collections import deque
from typing import Any


AGENT_PATH = "~/.local/share/pi-desktop-bridge/pi_agent.py"
# The agent permits PNG data up to 16 MP * 4 bytes plus 1 MiB. Base64 can
# exceed 86 MiB, so retain a bounded pipe with room for JSON framing.
MAX_RESPONSE_BYTES = 96 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$", re.ASCII)


class TransportError(RuntimeError):
    """The remote agent could not complete a request reliably."""


def validate_host(host: str) -> str:
    """Accept an SSH host alias, never an option, URI, or shell fragment."""
    if not _HOST_RE.fullmatch(host):
        raise ValueError("host must be a simple SSH hostname or alias")
    return host


def ssh_argv(host: str, remote_command: str) -> list[str]:
    return [
        "ssh", "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
        "-oConnectTimeout=8", "-oServerAliveInterval=15",
        validate_host(host), remote_command,
    ]


def _ssh_environment() -> dict[str, str]:
    """Restore Windows OpenSSH's system config path under filtered MCP envs."""
    environment = os.environ.copy()
    if os.name == "nt" and not environment.get("PROGRAMDATA"):
        drive = environment.get("SYSTEMDRIVE")
        if not drive and environment.get("SYSTEMROOT"):
            drive = os.path.splitdrive(environment["SYSTEMROOT"])[0]
        if drive:
            environment["PROGRAMDATA"] = os.path.join(drive + "\\", "ProgramData")
    return environment


class SSHTransport:
    """One SSH child and one in-flight request, with no automatic action retry."""

    def __init__(self, host: str, *, timeout: float = 60.0) -> None:
        self.host = validate_host(host)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[bytes | BaseException] | None = None
        self._stderr_tail: deque[bytes] = deque(maxlen=16)
        self._next_id = 0

    def _start(self) -> None:
        # The remote path is fixed code, not derived from tool arguments. The
        # leading tilde is intentionally unquoted for the remote shell to expand.
        proc = subprocess.Popen(
            ssh_argv(self.host, f"python3 -u {AGENT_PATH}"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, env=_ssh_environment(),
        )
        self._process = proc
        self._responses = queue.Queue(maxsize=2)
        self._stderr_tail.clear()
        threading.Thread(target=self._read_stdout, args=(proc, self._responses), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()

    def _read_stdout(self, proc: subprocess.Popen[bytes], responses: queue.Queue[bytes | BaseException]) -> None:
        assert proc.stdout is not None
        pending = bytearray()
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise TransportError("SSH agent closed its output")
                previous_length = len(pending)
                pending.extend(chunk)
                # A PNG response can be tens of MB. Search only the new bytes
                # until a delimiter appears, avoiding a full rescan per chunk.
                newline = pending.find(b"\n", previous_length)
                while newline >= 0:
                    line = bytes(pending[:newline])
                    del pending[:newline + 1]
                    if len(line) > MAX_RESPONSE_BYTES:
                        raise TransportError("SSH agent response exceeds size limit")
                    try:
                        responses.put_nowait(line)
                    except queue.Full as exc:
                        raise TransportError("SSH agent sent unsolicited responses") from exc
                    newline = pending.find(b"\n")
                if len(pending) > MAX_RESPONSE_BYTES:
                    raise TransportError("SSH agent response exceeds size limit")
        except BaseException as exc:
            try:
                responses.put_nowait(exc)
            except queue.Full:
                pass

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        try:
            while chunk := os.read(proc.stderr.fileno(), 4096):
                # Drain continuously to keep SSH from blocking. Never include
                # remote stderr in MCP errors: it could contain private data.
                self._stderr_tail.append(chunk[-256:])
        except OSError:
            pass

    def _discard(self) -> None:
        proc = self._process
        self._process = None
        self._responses = None
        if proc is None:
            return
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdout, proc.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    def _write_request(self, proc: subprocess.Popen[bytes], wire: bytes) -> None:
        if len(wire) > MAX_REQUEST_BYTES:
            raise TransportError("SSH agent request exceeds size limit")
        assert proc.stdin is not None
        outcome: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

        def write() -> None:
            try:
                view = memoryview(wire)
                while view:
                    count = os.write(proc.stdin.fileno(), view)
                    if count <= 0:
                        raise OSError("SSH stdin closed")
                    view = view[count:]
                outcome.put_nowait(None)
            except BaseException as exc:
                outcome.put_nowait(exc)

        threading.Thread(target=write, daemon=True).start()
        try:
            failure = outcome.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise TransportError(f"SSH agent write timed out after {self.timeout:g} seconds") from exc
        if failure is not None:
            raise TransportError("SSH agent write failed") from failure

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request; failed actions are never repeated automatically."""
        with self._lock:
            if self._process is None:
                self._start()
            assert self._process is not None and self._responses is not None
            self._next_id += 1
            request_id = self._next_id
            wire = json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            try:
                self._write_request(self._process, wire)
                response = self._responses.get(timeout=self.timeout)
                if isinstance(response, BaseException):
                    raise response
                value = json.loads(response)
                if not isinstance(value, dict) or value.get("id") != request_id:
                    raise TransportError("SSH agent returned an invalid response id")
                if "error" in value:
                    error = value["error"]
                    message = error.get("message") if isinstance(error, dict) else None
                    raise TransportError(str(message or "remote agent error"))
                result = value.get("result")
                if not isinstance(result, dict):
                    raise TransportError("SSH agent returned an invalid result")
                return result
            except queue.Empty as exc:
                self._discard()
                raise TransportError(f"SSH agent timed out after {self.timeout:g} seconds") from exc
            except (OSError, ValueError, TypeError, TransportError) as exc:
                self._discard()
                if isinstance(exc, TransportError):
                    raise
                raise TransportError("SSH agent communication failed") from exc

    def close(self) -> None:
        with self._lock:
            self._discard()

    def __enter__(self) -> SSHTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
