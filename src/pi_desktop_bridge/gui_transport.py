"""In-memory GUI SSH connection, with the standard bridge protocol and safety rules."""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import shlex
import socket
import subprocess
import threading
import time
from typing import Any

import paramiko

from . import cli
from .transport import (
    AGENT_BOOTSTRAP, EXPECTED_AGENT_SHA256, SSHTransport, TransportError,
    validate_host,
)


# Never let application debug logging emit Paramiko authentication details.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

_USERNAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z", re.ASCII)
_ALGORITHM_RE = re.compile(r"[A-Za-z0-9@._+-]{1,64}\Z", re.ASCII)
_DETAIL_KEYS = frozenset({
    "mode", "host", "port", "username", "password", "expected_fingerprint", "deploy",
})
_CONNECT_BUDGET = 90.0
_DEPLOY_BUDGET = 45.0


class GUIConnectionError(RuntimeError):
    """A safe error for the GUI, free of SSH stderr and submitted credentials."""

    def __init__(
        self, code: str, message: str, *, fingerprint: str | None = None,
        algorithm: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fingerprint = fingerprint
        self.algorithm = algorithm


def _fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")


class _ConfirmHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str | None) -> None:
        self.expected = expected

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        fingerprint = _fingerprint(key)
        algorithm = key.get_name()
        if self.expected != fingerprint:
            raise GUIConnectionError(
                "host_key_confirmation_required",
                "Confirm this server's SSH host-key fingerprint before connecting.",
                fingerprint=fingerprint,
                algorithm=algorithm if isinstance(algorithm, str) and _ALGORITHM_RE.fullmatch(algorithm) else None,
            )
        # Returning accepts this one connection in memory. No host-key store is
        # modified, and a known-host mismatch never reaches this callback.


def _details(details: dict[str, Any]) -> dict[str, Any]:
    if type(details) is not dict or set(details) - _DETAIL_KEYS:
        raise GUIConnectionError("invalid_details", "Connection details contain unsupported fields.")
    mode = details.get("mode")
    if not isinstance(mode, str) or mode not in {"existing", "password"}:
        raise GUIConnectionError("invalid_details", "Choose an existing SSH alias or a direct SSH connection.")
    host = details.get("host")
    if not isinstance(host, str):
        raise GUIConnectionError("invalid_details", "Enter a valid SSH host or alias.")
    try:
        host = validate_host(host)
    except ValueError as exc:
        raise GUIConnectionError("invalid_details", "Enter a valid DNS name, IPv4 address, or SSH alias.") from exc
    port = details.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise GUIConnectionError("invalid_details", "SSH port must be an integer from 1 to 65535.")
    username = details.get("username", "")
    if not isinstance(username, str) or (username and not _USERNAME_RE.fullmatch(username)):
        raise GUIConnectionError("invalid_details", "Enter a valid SSH username.")
    password = details.get("password", "")
    if not isinstance(password, str) or len(password) > 4096:
        raise GUIConnectionError("invalid_details", "Password must be at most 4096 characters.")
    expected = details.get("expected_fingerprint")
    if expected == "":
        expected = None
    if expected is not None and (not isinstance(expected, str) or not _FINGERPRINT_RE.fullmatch(expected)):
        raise GUIConnectionError("invalid_details", "Expected fingerprint must use OpenSSH SHA256 format.")
    deploy = details.get("deploy", True)
    if type(deploy) is not bool:
        raise GUIConnectionError("invalid_details", "Deploy must be true or false.")
    if mode == "existing":
        if port != 22 or username or password or expected:
            raise GUIConnectionError(
                "invalid_details", "Existing SSH alias mode uses its configured user, port, and host key.",
            )
    elif not username:
        raise GUIConnectionError("invalid_details", "Enter the SSH username for a direct connection.")
    return {"mode": mode, "host": host, "port": port, "username": username,
            "password": password, "expected_fingerprint": expected, "deploy": deploy}


class _BridgeProcess:
    """A Paramiko channel surfaced as the pipe interface SSHTransport expects."""

    def __init__(self, channel: paramiko.Channel) -> None:
        self.channel = channel
        self._closed = threading.Event()
        self._stdin_read, stdin_write = os.pipe()
        stdout_read, self._stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        os.close(stderr_write)  # stderr is drained inside the channel pump.
        self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
        self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
        self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
        self._threads = [
            threading.Thread(target=self._pump_input, daemon=True, name="pi-gui-ssh-input"),
            threading.Thread(target=self._pump_output, daemon=True, name="pi-gui-ssh-output"),
            threading.Thread(target=self._drain_remote_stderr, daemon=True, name="pi-gui-ssh-stderr"),
        ]
        for thread in self._threads:
            thread.start()

    def _pump_input(self) -> None:
        try:
            while not self._closed.is_set():
                data = os.read(self._stdin_read, 65536)
                if not data:
                    break
                view = memoryview(data)
                while view and not self._closed.is_set():
                    sent = self.channel.send(view)
                    if sent <= 0:
                        return
                    view = view[sent:]
            if not self._closed.is_set():
                self.channel.shutdown_write()
        except (OSError, EOFError, socket.timeout):
            self.channel.close()
        finally:
            try:
                os.close(self._stdin_read)
            except OSError:
                pass

    def _pump_output(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    data = self.channel.recv(65536)
                except socket.timeout:
                    if self.channel.closed or (self.channel.exit_status_ready() and not self.channel.recv_ready()):
                        break
                    continue
                if not data:
                    break
                view = memoryview(data)
                while view:
                    written = os.write(self._stdout_write, view)
                    if written <= 0:
                        return
                    view = view[written:]
        except (OSError, EOFError):
            pass
        finally:
            try:
                os.close(self._stdout_write)
            except OSError:
                pass

    def _drain_remote_stderr(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    data = self.channel.recv_stderr(4096)
                except socket.timeout:
                    if self.channel.closed or (self.channel.exit_status_ready() and not self.channel.recv_stderr_ready()):
                        break
                    continue
                if not data:
                    break
        except (OSError, EOFError):
            pass

    def poll(self) -> int | None:
        if self.channel.exit_status_ready():
            return self.channel.recv_exit_status()
        return 255 if self.channel.closed else None

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while (status := self.poll()) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("Paramiko SSH channel", timeout)
            time.sleep(0.01)
        return status

    def kill(self) -> None:
        self._closed.set()
        self.channel.close()
        try:
            self.stdin.close()
        except OSError:
            pass

    def close(self) -> None:
        self.kill()
        for stream in (self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=0.2)


def _exec_command_until(channel: paramiko.Channel, command: str, deadline: float) -> None:
    """Bound Paramiko's exec acknowledgment wait, which ignores Channel.settimeout."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("SSH exec deadline expired")
    finished = threading.Event()
    errors: list[Exception] = []

    def execute() -> None:
        try:
            channel.exec_command(command)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=execute, daemon=True, name="pi-gui-ssh-exec")
    worker.start()
    if not finished.wait(remaining):
        # Paramiko 4 waits on an Event with no deadline. Closing the channel
        # releases that wait; join separately within a bounded cleanup window.
        channel.close()
        worker.join(timeout=0.5)
        raise TimeoutError("SSH exec acknowledgment timed out")
    if errors:
        raise errors[0]


class GUITransport(SSHTransport):
    """Reuse SSHTransport's framing, hello, guards and response limits."""

    def __init__(self, host: str, client: paramiko.SSHClient, *, timeout: float = 60.0) -> None:
        super().__init__(host, timeout=timeout)
        self._client: paramiko.SSHClient | None = client
        self._thread_state = threading.local()

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, deadline: float | None = None,
    ) -> dict[str, Any]:
        budget = self._deadline(deadline)
        self._thread_state.deadline = budget
        try:
            return super().request(method, params, deadline=budget)
        finally:
            self._thread_state.deadline = None

    def _start(self) -> None:
        client = self._client
        if client is None or client.get_transport() is None or not client.get_transport().is_active():
            raise TransportError("GUI SSH connection is closed; reconnect in the browser.", input_state="not_started")
        deadline = getattr(self._thread_state, "deadline", None)
        remaining = self._remaining(deadline) if deadline is not None else self.timeout
        if remaining <= 0:
            raise TransportError("GUI SSH request timed out before starting", input_state="not_started")
        channel = None
        try:
            channel = client.get_transport().open_session(timeout=min(8.0, remaining))
            channel.settimeout(min(8.0, max(0.01, self._remaining(deadline))))
            _exec_command_until(
                channel, "python3 -u -c " + shlex.quote(AGENT_BOOTSTRAP),
                deadline if deadline is not None else time.monotonic() + self.timeout,
            )
            channel.settimeout(0.5)
            process = _BridgeProcess(channel)
        except Exception as exc:
            if channel is not None:
                channel.close()
            raise TransportError("Could not start the remote Pi agent.", input_state="not_started") from exc
        self._process = process
        self._responses = queue.Queue(maxsize=2)
        self._stderr_tail = deque(maxlen=16)
        self._hello = None
        threading.Thread(target=self._read_stdout, args=(process, self._responses), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(process, self._stderr_tail), daemon=True).start()

    def _discard(self) -> None:
        process = self._process
        try:
            super()._discard()
        finally:
            if isinstance(process, _BridgeProcess):
                process.close()

    def close(self) -> None:
        try:
            super().close()
        finally:
            client = self._client
            self._client = None
            if client is not None:
                client.close()


def _deploy_password(client: paramiko.SSHClient, deadline: float) -> None:
    """Run only the fixed, owner-protected installer over the authenticated channel."""
    source = Path(__file__).with_name("pi_agent.py").read_bytes()
    if not source or len(source) > 1_048_576:
        raise GUIConnectionError("deployment_failed", "Packaged Pi agent is unavailable or too large.")
    expected = hashlib.sha256(source).hexdigest()
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise GUIConnectionError("connection_failed", "SSH connection closed before deployment.")
    channel = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        channel = transport.open_session(timeout=min(8.0, remaining))
        channel.settimeout(min(8.0, remaining))
        _exec_command_until(channel, "python3 -c " + shlex.quote(cli._DEPLOY_CODE), deadline)
        channel.settimeout(1.0)
        view = memoryview(source)
        while view:
            if time.monotonic() >= deadline:
                raise TimeoutError
            sent = channel.send(view)
            if sent <= 0:
                raise OSError("remote channel closed")
            view = view[sent:]
        channel.shutdown_write()
        output = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            if channel.recv_ready():
                output.extend(channel.recv(4096))
                if len(output) > 4096:
                    raise ValueError("verification response too large")
            if channel.recv_stderr_ready():
                channel.recv_stderr(4096)  # Drain, never expose remote stderr.
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(0.01)
        if channel.recv_exit_status() != 0:
            raise ValueError("remote deployment failed")
        verification = json.loads(output.decode("utf-8"))
        if not isinstance(verification, dict) or verification.get("sha256") != expected:
            raise ValueError("remote checksum mismatch")
    except GUIConnectionError:
        raise
    except Exception as exc:
        raise GUIConnectionError("deployment_failed", "Could not deploy the Pi agent securely. Check the SSH account and retry.") from exc
    finally:
        if channel is not None:
            channel.close()


def _checked_transport(bridge: SSHTransport, deadline: float) -> SSHTransport:
    try:
        hello = bridge.request("hello", deadline=deadline)
        if hello.get("agent_sha256") != EXPECTED_AGENT_SHA256:
            raise GUIConnectionError(
                "agent_source_mismatch", "Deployed Pi agent differs from this client. Deploy the latest agent and reconnect.",
            )
        health = bridge.request("health", deadline=deadline)
        if health.get("desktop_ready") is not True:
            raise GUIConnectionError(
                "desktop_not_ready", "Pi desktop is not ready. Start a Wayland session and install wayvnc, grim, and wtype.",
            )
        return bridge
    except GUIConnectionError:
        bridge.close()
        raise
    except TransportError as exc:
        bridge.close()
        raise GUIConnectionError("connection_failed", "Could not verify the Pi agent. Check SSH access and deployment.") from exc
    except Exception as exc:
        bridge.close()
        raise GUIConnectionError("connection_failed", "Could not verify the Pi agent. Check SSH access and deployment.") from exc


def connect_gui(details: dict[str, Any]) -> SSHTransport:
    """Authenticate and verify a lease-free Pi agent before returning a bridge."""
    values = _details(details)
    deadline = time.monotonic() + _CONNECT_BUDGET
    host = values["host"]
    if values["mode"] == "existing":
        if values["deploy"]:
            try:
                cli.deploy(host)
            except Exception as exc:
                raise GUIConnectionError("deployment_failed", "Could not deploy the Pi agent through this SSH alias.") from exc
        bridge = SSHTransport(host)
        return _checked_transport(bridge, deadline)

    client = paramiko.SSHClient()
    try:
        known_host_paths = [Path.home() / ".ssh" / "known_hosts"]
        if os.name == "nt" and os.environ.get("PROGRAMDATA"):
            known_host_paths.append(Path(os.environ["PROGRAMDATA"]) / "ssh" / "ssh_known_hosts")
        elif os.name != "nt":
            known_host_paths.append(Path("/etc/ssh/ssh_known_hosts"))
        for known_hosts in known_host_paths:
            if known_hosts.is_file():
                client.load_system_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(_ConfirmHostKey(values["expected_fingerprint"]))
        password = values["password"]
        try:
            client.connect(
                hostname=host, port=values["port"], username=values["username"],
                password=password or None,
                look_for_keys=not bool(password), allow_agent=not bool(password),
                timeout=min(8.0, max(0.01, deadline - time.monotonic())),
                banner_timeout=8.0, auth_timeout=12.0, channel_timeout=8.0,
            )
        finally:
            password = None
            values["password"] = ""
        if values["deploy"]:
            _deploy_password(client, min(deadline, time.monotonic() + _DEPLOY_BUDGET))
        bridge = GUITransport(host, client)
        return _checked_transport(bridge, deadline)
    except GUIConnectionError:
        client.close()
        raise
    except paramiko.BadHostKeyException as exc:
        client.close()
        raise GUIConnectionError("host_key_changed", "SSH host key differs from the trusted known_hosts entry. Check the server before reconnecting.") from exc
    except paramiko.AuthenticationException as exc:
        client.close()
        raise GUIConnectionError("authentication_failed", "SSH authentication failed. Check the username and credentials.") from exc
    except Exception as exc:
        client.close()
        raise GUIConnectionError("connection_failed", "Could not connect to the SSH host. Check its address and availability.") from exc
