"""GUI SSH trust, credentials, deployment and Paramiko pipe integration."""

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paramiko

from pi_desktop_bridge import PROTOCOL_VERSION
from pi_desktop_bridge.gui_transport import (
    GUIConnectionError, _deploy_password, _fingerprint, connect_gui,
)
from pi_desktop_bridge.transport import EXPECTED_AGENT_SHA256, REQUIRED_CAPABILITIES, TransportError


class _LocalSSHServer:
    """Disposable loopback SSH server that speaks only the bridge test protocol."""

    def __init__(
        self, *, accepted_password: str = "secret", click_delay: float = 0.0,
        agent_hash: str = EXPECTED_AGENT_SHA256, desktop_ready: bool = True,
        close_on_click: bool = False, blob_size: int = 0, stderr_bytes: int = 0,
        hold_exec_ack: bool = False,
    ) -> None:
        self.key = paramiko.RSAKey.generate(2048)
        self.accepted_password = accepted_password
        self.click_delay = click_delay
        self.agent_hash = agent_hash
        self.desktop_ready = desktop_ready
        self.close_on_click = close_on_click
        self.blob_size = blob_size
        self.stderr_bytes = stderr_bytes
        self.exec_request_seen = threading.Event()
        self.exec_ack_gate = threading.Event()
        if not hold_exec_ack:
            self.exec_ack_gate.set()
        self.auth_count = 0
        self.commands: list[str] = []
        self.methods: list[str] = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self._transport: paramiko.Transport | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        owner = self

        class Interface(paramiko.ServerInterface):
            def __init__(self) -> None:
                self.exec_event = threading.Event()
                self.command = ""

            def check_auth_password(self, username: str, password: str) -> int:
                owner.auth_count += 1
                return (paramiko.AUTH_SUCCESSFUL if username == "pi" and password == owner.accepted_password
                        else paramiko.AUTH_FAILED)

            def get_allowed_auths(self, username: str) -> str:
                return "password"

            def check_channel_request(self, kind: str, channel_id: int) -> int:
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes) -> bool:
                owner.exec_request_seen.set()
                owner.exec_ack_gate.wait(timeout=5)
                self.command = command.decode("utf-8")
                self.exec_event.set()
                return True

        try:
            connection, _ = self.listener.accept()
            transport = paramiko.Transport(connection)
            self._transport = transport
            transport.add_server_key(self.key)
            interface = Interface()
            transport.start_server(server=interface)
            while transport.is_active():
                channel = transport.accept(timeout=0.5)
                if channel is None:
                    continue
                interface.exec_event.wait(timeout=3)
                self.commands.append(interface.command)
                interface.exec_event.clear()
                channel.settimeout(0.2)
                pending = bytearray()
                while transport.is_active() and not channel.closed:
                    try:
                        chunk = channel.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        request = json.loads(line)
                        method = request["method"]
                        self.methods.append(method)
                        if method == "click" and self.close_on_click:
                            channel.close()
                            break
                        if method == "click" and self.click_delay:
                            time.sleep(self.click_delay)
                        if method == "status" and self.stderr_bytes:
                            channel.send_stderr(b"x" * self.stderr_bytes)
                        if method == "hello":
                            result = {
                                "protocol_version": PROTOCOL_VERSION,
                                "agent_version": "0.6.0",
                                "agent_sha256": self.agent_hash,
                                "capabilities": sorted(REQUIRED_CAPABILITIES),
                            }
                        elif method == "health":
                            result = {"desktop_ready": self.desktop_ready, "session_active": False, "checks": []}
                        elif method == "status" and self.blob_size:
                            result = {"blob": "x" * self.blob_size}
                        else:
                            result = {"ok": True}
                        try:
                            channel.sendall((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
                        except (OSError, EOFError):
                            break
                        if method == "disconnect":
                            channel.close()
                            break
                channel.close()
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            if self._transport is not None:
                self._transport.close()
            self.listener.close()

    def close(self) -> None:
        self.exec_ack_gate.set()
        if self._transport is not None:
            self._transport.close()
        self.listener.close()
        self._thread.join(timeout=2)


def _details(server: _LocalSSHServer, *, expected: str | None, password: str = "secret") -> dict:
    return {"mode": "password", "host": "127.0.0.1", "port": server.port,
            "username": "pi", "password": password, "expected_fingerprint": expected,
            "deploy": False}


class GUITransportTests(unittest.TestCase):
    def test_exec_acknowledgment_deadline_bounds_startup_and_deployment(self) -> None:
        for deploy, code in ((False, "connection_failed"), (True, "deployment_failed")):
            with self.subTest(deploy=deploy):
                server = _LocalSSHServer(hold_exec_ack=True)
                try:
                    with tempfile.TemporaryDirectory() as directory, patch(
                        "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
                    ), patch("pi_desktop_bridge.gui_transport._CONNECT_BUDGET", 2.0):
                        values = _details(server, expected=_fingerprint(server.key))
                        values["deploy"] = deploy
                        started = time.monotonic()
                        with self.assertRaises(GUIConnectionError) as caught:
                            connect_gui(values)
                        self.assertLess(time.monotonic() - started, 3.0)
                        self.assertEqual(caught.exception.code, code)
                        self.assertTrue(server.exec_request_seen.is_set())
                        self.assertFalse(any(thread.name == "pi-gui-ssh-exec" for thread in threading.enumerate()))
                finally:
                    server.close()

    def test_strict_connection_details_reject_extra_and_wrong_types(self) -> None:
        baseline = {"mode": "password", "host": "127.0.0.1", "username": "pi", "deploy": False}
        invalid = [
            {**baseline, "password": "x", "secret_file": "x"},
            {**baseline, "mode": []},
            {**baseline, "port": True},
            {**baseline, "port": 65536},
            {**baseline, "host": "-oProxyCommand=bad"},
            {**baseline, "username": "pi;bad"},
            {**baseline, "password": "x" * 4097},
            {**baseline, "expected_fingerprint": "SHA256:short"},
            {**baseline, "deploy": "false"},
            {"mode": "existing", "host": "pi", "password": "must-not-be-ignored"},
        ]
        for details in invalid:
            with self.subTest(details=list(details)), self.assertRaises(GUIConnectionError) as caught:
                connect_gui(details)
            self.assertEqual(caught.exception.code, "invalid_details")

    def test_unknown_host_fingerprint_is_reported_before_authentication(self) -> None:
        server = _LocalSSHServer()
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                with self.assertRaises(GUIConnectionError) as caught:
                    connect_gui(_details(server, expected=None))
                self.assertEqual(caught.exception.code, "host_key_confirmation_required")
                self.assertEqual(caught.exception.fingerprint, _fingerprint(server.key))
                self.assertEqual(caught.exception.algorithm, server.key.get_name())
                self.assertEqual(server.auth_count, 0)
                self.assertFalse((Path(directory) / ".ssh" / "known_hosts").exists())
        finally:
            server.close()

    def test_matching_fingerprint_authenticates_in_memory_and_pumps_protocol(self) -> None:
        server = _LocalSSHServer()
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                bridge = connect_gui(_details(server, expected=_fingerprint(server.key)))
                try:
                    self.assertEqual(bridge.request("status"), {"ok": True})
                    self.assertEqual(bridge.disconnect(), {"ok": True})
                finally:
                    bridge.close()
                self.assertEqual(server.auth_count, 1)
                self.assertEqual(server.methods[:3], ["hello", "health", "status"])
                self.assertEqual(server.methods[-1], "disconnect")
                self.assertTrue(all(command.startswith("python3 -u -c ") for command in server.commands))
                self.assertFalse((Path(directory) / ".ssh" / "known_hosts").exists())
                self.assertFalse(any(thread.name.startswith("pi-gui-ssh-") for thread in threading.enumerate()))
        finally:
            server.close()

    def test_large_response_and_stderr_are_drained_without_exposing_stderr(self) -> None:
        server = _LocalSSHServer(blob_size=1_048_576, stderr_bytes=131_072)
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                bridge = connect_gui(_details(server, expected=_fingerprint(server.key)))
                try:
                    self.assertEqual(bridge.request("status")["blob"], "x" * 1_048_576)
                finally:
                    bridge.close()
        finally:
            server.close()

    def test_changed_known_host_key_cannot_be_overridden(self) -> None:
        server = _LocalSSHServer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                ssh_directory = home / ".ssh"
                ssh_directory.mkdir()
                other = paramiko.RSAKey.generate(2048)
                entry = f"[127.0.0.1]:{server.port} {other.get_name()} {other.get_base64()}\n"
                (ssh_directory / "known_hosts").write_text(entry, encoding="ascii")
                with patch("pi_desktop_bridge.gui_transport.Path.home", return_value=home):
                    with self.assertRaises(GUIConnectionError) as caught:
                        connect_gui(_details(server, expected=_fingerprint(server.key)))
                self.assertEqual(caught.exception.code, "host_key_changed")
                self.assertEqual(server.auth_count, 0)
                self.assertEqual((ssh_directory / "known_hosts").read_text(encoding="ascii"), entry)
        finally:
            server.close()

    def test_password_is_absent_from_errors_commands_environment_and_files(self) -> None:
        sentinel = "dummy " * 8
        server = _LocalSSHServer(accepted_password="different")
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                with self.assertRaises(GUIConnectionError) as caught:
                    connect_gui(_details(server, expected=_fingerprint(server.key), password=sentinel))
                self.assertEqual(caught.exception.code, "authentication_failed")
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertFalse(any(sentinel in value for value in os.environ.values()))
                self.assertFalse(any(sentinel in command for command in server.commands))
                self.assertFalse(any(sentinel.encode() in path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()))
        finally:
            server.close()

    def test_action_timeout_does_not_replay_input(self) -> None:
        server = _LocalSSHServer(click_delay=2.0)
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                bridge = connect_gui(_details(server, expected=_fingerprint(server.key)))
                try:
                    bridge.timeout = 0.15
                    started = time.monotonic()
                    with self.assertRaises(TransportError):
                        bridge.request("click", {"x": 1, "y": 1})
                    self.assertLess(time.monotonic() - started, 1.5)
                finally:
                    bridge.close()
                self.assertEqual(server.methods.count("click"), 1)
        finally:
            server.close()

    def test_remote_eof_after_input_is_ambiguous_and_cleans_pumps(self) -> None:
        server = _LocalSSHServer(close_on_click=True)
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                bridge = connect_gui(_details(server, expected=_fingerprint(server.key)))
                try:
                    with self.assertRaises(TransportError) as caught:
                        bridge.request("click", {"x": 1, "y": 1})
                    self.assertEqual(caught.exception.input_state, "may_have_executed")
                    self.assertIsNone(bridge._process)
                finally:
                    bridge.close()
                self.assertEqual(server.methods.count("click"), 1)
                self.assertFalse(any(thread.name.startswith("pi-gui-ssh-") for thread in threading.enumerate()))
        finally:
            server.close()

    def test_source_mismatch_without_deploy_has_deploy_hint(self) -> None:
        server = _LocalSSHServer(agent_hash="0" * 64)
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                with self.assertRaises(GUIConnectionError) as caught:
                    connect_gui(_details(server, expected=_fingerprint(server.key)))
                self.assertEqual(caught.exception.code, "agent_source_mismatch")
                self.assertIn("Deploy", caught.exception.message)
                self.assertEqual(server.methods, ["hello"])
        finally:
            server.close()

    def test_health_not_ready_reports_prerequisites_without_starting_session(self) -> None:
        server = _LocalSSHServer(desktop_ready=False)
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)
            ):
                with self.assertRaises(GUIConnectionError) as caught:
                    connect_gui(_details(server, expected=_fingerprint(server.key)))
                self.assertEqual(caught.exception.code, "desktop_not_ready")
                self.assertIn("Wayland", caught.exception.message)
                self.assertEqual(server.methods, ["hello", "health"])
        finally:
            server.close()

    def test_existing_alias_mode_uses_strict_cli_deploy_and_checked_transport(self) -> None:
        class FakeBridge:
            def __init__(self) -> None:
                self.methods: list[str] = []

            def request(self, method: str, *, deadline: float) -> dict:
                self.methods.append(method)
                if method == "hello":
                    return {"agent_sha256": EXPECTED_AGENT_SHA256}
                return {"desktop_ready": True}

            def close(self) -> None:
                pass

        bridge = FakeBridge()
        with patch("pi_desktop_bridge.gui_transport.cli.deploy", return_value="digest") as deploy, patch(
            "pi_desktop_bridge.gui_transport.SSHTransport", return_value=bridge
        ):
            self.assertIs(connect_gui({"mode": "existing", "host": "pi-desktop"}), bridge)
        deploy.assert_called_once_with("pi-desktop")
        self.assertEqual(bridge.methods, ["hello", "health"])

    def test_password_auth_flags_never_fall_back_to_keys(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.options: dict = {}

            def load_system_host_keys(self, filename) -> None:
                pass

            def set_missing_host_key_policy(self, policy) -> None:
                pass

            def connect(self, **kwargs) -> None:
                self.options = kwargs

            def close(self) -> None:
                pass

        for password, expected in (("secret", False), ("", True)):
            with self.subTest(password=bool(password)), tempfile.TemporaryDirectory() as directory:
                client = FakeClient()
                with patch("pi_desktop_bridge.gui_transport.Path.home", return_value=Path(directory)), patch(
                    "pi_desktop_bridge.gui_transport.paramiko.SSHClient", return_value=client
                ), patch("pi_desktop_bridge.gui_transport._checked_transport", return_value="checked"):
                    result = connect_gui({"mode": "password", "host": "127.0.0.1", "username": "pi",
                                          "password": password, "deploy": False})
                self.assertEqual(result, "checked")
                self.assertEqual(client.options["allow_agent"], expected)
                self.assertEqual(client.options["look_for_keys"], expected)
                self.assertEqual(client.options["password"], password or None)

    def test_fixed_deployment_streams_packaged_source_and_verifies_digest(self) -> None:
        class Channel:
            def __init__(self) -> None:
                self.command = ""
                self.data = bytearray()
                self.read = False
                self.written = False
                self.closed = False

            def settimeout(self, timeout: float) -> None:
                pass

            def exec_command(self, command: str) -> None:
                self.command = command

            def send(self, view) -> int:
                self.data.extend(view)
                return len(view)

            def shutdown_write(self) -> None:
                self.written = True

            def recv_ready(self) -> bool:
                return self.written and not self.read

            def recv(self, size: int) -> bytes:
                self.read = True
                return json.dumps({"sha256": hashlib.sha256(self.data).hexdigest()}).encode()

            def recv_stderr_ready(self) -> bool:
                return False

            def exit_status_ready(self) -> bool:
                return self.written

            def recv_exit_status(self) -> int:
                return 0

            def close(self) -> None:
                self.closed = True

        channel = Channel()

        class Transport:
            def is_active(self) -> bool:
                return True

            def open_session(self, timeout: float):
                return channel

        class Client:
            def get_transport(self):
                return Transport()

        _deploy_password(Client(), time.monotonic() + 5)
        self.assertEqual(channel.data, Path(__file__).parents[1].joinpath("src/pi_desktop_bridge/pi_agent.py").read_bytes())
        self.assertTrue(channel.command.startswith("python3 -c "))
        self.assertNotIn("password", channel.command.lower())
        self.assertTrue(channel.closed)

    def test_deployment_stderr_is_never_returned_to_gui(self) -> None:
        secret = "P1-stderr-secret-should-stay-private"

        class FailedChannel:
            def __init__(self) -> None:
                self.drained = False
                self.closed = False

            def settimeout(self, timeout: float) -> None:
                pass

            def exec_command(self, command: str) -> None:
                pass

            def send(self, data) -> int:
                return len(data)

            def shutdown_write(self) -> None:
                pass

            def recv_ready(self) -> bool:
                return False

            def recv_stderr_ready(self) -> bool:
                return not self.drained

            def recv_stderr(self, size: int) -> bytes:
                self.drained = True
                return secret.encode()

            def exit_status_ready(self) -> bool:
                return True

            def recv_exit_status(self) -> int:
                return 1

            def close(self) -> None:
                self.closed = True

        channel = FailedChannel()

        class Transport:
            def is_active(self) -> bool:
                return True

            def open_session(self, timeout: float):
                return channel

        class Client:
            def get_transport(self):
                return Transport()

        with self.assertRaises(GUIConnectionError) as caught:
            _deploy_password(Client(), time.monotonic() + 5)
        self.assertEqual(caught.exception.code, "deployment_failed")
        self.assertNotIn(secret, caught.exception.message)
        self.assertTrue(channel.drained and channel.closed)


if __name__ == "__main__":
    unittest.main()
