"""Persistent, bounded request/response transport to the remote desktop agent."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION


AGENT_PATH = "~/.local/share/pi-desktop-bridge/pi_agent.py"
# Pin the packaged source for this client process. Updating the package on disk
# requires restarting the client before it can use a newly deployed agent.
EXPECTED_AGENT_SHA256 = hashlib.sha256(Path(__file__).with_name("pi_agent.py").read_bytes()).hexdigest()
# Execute the same bounded snapshot whose hash hello will advertise, even if
# deployment atomically replaces the file while its imports are running.
AGENT_BOOTSTRAP = """
import hashlib
from pathlib import Path
path = Path.home() / '.local/share/pi-desktop-bridge/pi_agent.py'
with path.open('rb') as stream:
    source = stream.read(1048577)
if not source or len(source) > 1048576:
    raise RuntimeError('Pi agent source is empty or exceeds the byte limit')
namespace = {'__name__': '__pi_desktop_agent_snapshot__', '__file__': str(path)}
exec(compile(source, str(path), 'exec'), namespace)
namespace['AGENT_SHA256'] = hashlib.sha256(source).hexdigest()
namespace['main']()
""".strip()
# The agent permits PNG data up to 16 MP * 4 bytes plus 1 MiB. Base64 can
# exceed 86 MiB, so retain a bounded pipe with room for JSON framing.
MAX_RESPONSE_BYTES = 96 * 1024 * 1024
# Match the agent's line limit so oversized input is rejected before any write.
MAX_REQUEST_BYTES = 65_536
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$", re.ASCII)
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_AGENT_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
REQUIRED_CAPABILITIES = frozenset({
    "hello", "health", "status", "screenshot", "wait_for_stable", "move", "click", "drag",
    "scroll", "type_text", "key", "disconnect",
})


class TransportError(RuntimeError):
    """The remote agent could not complete a request reliably."""

    def __init__(
        self, message: str, *, code: str = "transport_error",
        input_state: str = "may_have_executed",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.input_state = input_state


class RemoteAgentError(TransportError):
    """A valid remote rejection; the SSH session remains healthy."""

    def __init__(self, code: str, message: str, input_state: str) -> None:
        super().__init__(message, code=code, input_state=input_state)


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
        self._hello: dict[str, Any] | None = None

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _deadline(self, deadline: float | None) -> float:
        own = time.monotonic() + self.timeout
        return min(own, deadline) if deadline is not None else own

    def _start(self) -> None:
        # The bootstrap and agent path are fixed code, never tool arguments.
        try:
            proc = subprocess.Popen(
                ssh_argv(self.host, "python3 -u -c " + shlex.quote(AGENT_BOOTSTRAP)),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, env=_ssh_environment(),
            )
        except OSError as exc:
            raise TransportError("Could not start SSH", input_state="not_started") from exc
        self._process = proc
        self._responses = queue.Queue(maxsize=2)
        self._stderr_tail = deque(maxlen=16)
        self._hello = None
        threading.Thread(target=self._read_stdout, args=(proc, self._responses), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc, self._stderr_tail), daemon=True).start()

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

    def _drain_stderr(self, proc: subprocess.Popen[bytes], tail: deque[bytes]) -> None:
        assert proc.stderr is not None
        try:
            while chunk := os.read(proc.stderr.fileno(), 4096):
                # Drain continuously to keep SSH from blocking. Never include
                # remote stderr in MCP errors: it could contain private data.
                tail.append(chunk[-256:])
        except OSError:
            pass

    def _discard(self) -> None:
        proc = self._process
        self._process = None
        self._responses = None
        self._hello = None
        if proc is None:
            return
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        # Cleanup has a separate bounded allowance after the request deadline.
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
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

    def _write_request(self, proc: subprocess.Popen[bytes], wire: bytes, deadline: float) -> None:
        if len(wire) > MAX_REQUEST_BYTES:
            raise TransportError("SSH agent request exceeds size limit", input_state="not_started")
        if self._remaining(deadline) <= 0:
            raise TransportError("SSH agent request timed out before input was sent", input_state="not_started")
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
            failure = outcome.get(timeout=self._remaining(deadline))
        except queue.Empty as exc:
            raise TransportError("SSH agent write timed out; outcome is unknown") from exc
        if failure is not None:
            raise TransportError("SSH agent write failed") from failure

    def _exchange(self, method: str, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        assert self._process is not None and self._responses is not None
        self._next_id += 1
        request_id = self._next_id
        try:
            wire = json.dumps(
                {"id": request_id, "method": method, "params": params},
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise TransportError("Request arguments are not JSON serializable", input_state="not_started") from exc
        self._write_request(self._process, wire, deadline)
        try:
            response = self._responses.get(timeout=self._remaining(deadline))
        except queue.Empty as exc:
            raise TransportError("SSH agent response timed out; outcome is unknown") from exc
        if isinstance(response, BaseException):
            raise TransportError("SSH agent closed its output or sent an invalid response") from response
        try:
            value = json.loads(response)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise TransportError("SSH agent returned malformed JSON") from exc
        if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != request_id:
            raise TransportError("SSH agent returned an invalid response id")
        if ("result" in value) == ("error" in value):
            raise TransportError("SSH agent returned an invalid response shape")
        if "error" in value:
            error = value["error"]
            if not isinstance(error, dict):
                raise TransportError("SSH agent returned a malformed error")
            code = error.get("code")
            message = error.get("message")
            input_state = error.get("input_state")
            if (
                not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code)
                or not isinstance(message, str) or not 0 < len(message) <= 1024
                or not isinstance(input_state, str)
                or input_state not in {"not_started", "may_have_executed"}
            ):
                raise TransportError("SSH agent returned a malformed error")
            raise RemoteAgentError(code, message, input_state)
        result = value["result"]
        if not isinstance(result, dict):
            raise TransportError("SSH agent returned an invalid result")
        return result

    def _negotiate(self, deadline: float) -> None:
        try:
            hello = self._exchange("hello", {}, deadline)
            capabilities = hello.get("capabilities")
            agent_version = hello.get("agent_version")
            agent_sha256 = hello.get("agent_sha256")
            if (
                type(hello.get("protocol_version")) is not int
                or hello["protocol_version"] != PROTOCOL_VERSION
                or not isinstance(agent_version, str) or not 0 < len(agent_version) <= 64
                or not agent_version.strip()
                or not isinstance(agent_sha256, str) or _AGENT_SHA256_RE.fullmatch(agent_sha256) is None
                or not isinstance(capabilities, list)
                or not all(isinstance(item, str) for item in capabilities)
                or not REQUIRED_CAPABILITIES.issubset(capabilities)
            ):
                raise TransportError("Agent protocol or capabilities are incompatible", input_state="not_started")
            self._hello = hello
        except (TransportError, OSError) as exc:
            self._discard()
            raise TransportError(
                f"Could not negotiate Pi desktop protocol {PROTOCOL_VERSION}. Deploy the latest agent and reconnect.",
                code="incompatible_agent", input_state="not_started",
            ) from exc

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, deadline: float | None = None,
    ) -> dict[str, Any]:
        """One budget includes lock wait, hello, write and response; never retry inputs."""
        budget_end = self._deadline(deadline)
        if not self._lock.acquire(timeout=self._remaining(budget_end)):
            # Another request owns the process; never discard it on lock timeout.
            raise TransportError("SSH transport is busy; request timed out before starting", input_state="not_started")
        try:
            if self._remaining(budget_end) <= 0:
                raise TransportError("SSH request timed out before starting", input_state="not_started")
            if self._process is not None and self._process.poll() is not None:
                self._discard()
            if self._process is None:
                self._start()
                self._negotiate(budget_end)
            if method == "hello":
                assert self._hello is not None
                return dict(self._hello)
            assert self._hello is not None
            if method not in {"health", "disconnect"} and self._hello["agent_sha256"] != EXPECTED_AGENT_SHA256:
                raise TransportError(
                    "Deployed Pi agent source differs from this client. Deploy the latest agent and restart the MCP server.",
                    code="agent_source_mismatch", input_state="not_started",
                )
            try:
                return self._exchange(method, params or {}, budget_end)
            except RemoteAgentError:
                # A well-formed rejection is safe to follow with another call.
                raise
            except TransportError:
                self._discard()
                raise
        finally:
            self._lock.release()

    def disconnect(self, *, deadline: float | None = None) -> dict[str, Any]:
        """Release an existing session without opening a new SSH process."""
        budget_end = self._deadline(deadline)
        if not self._lock.acquire(timeout=self._remaining(budget_end)):
            raise TransportError("SSH transport is busy; disconnect timed out before starting", input_state="not_started")
        try:
            if self._process is not None and self._process.poll() is not None:
                self._discard()
            if self._process is None:
                return {"ok": True}
            try:
                return self._exchange("disconnect", {}, budget_end)
            finally:
                self._discard()
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._discard()

    def __enter__(self) -> SSHTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
