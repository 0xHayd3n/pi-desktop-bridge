"""Exercise actual pipe framing and failure behavior without an SSH server."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pi_desktop_bridge import cli
from pi_desktop_bridge.transport import (
    REQUIRED_CAPABILITIES, RemoteAgentError, SSHTransport, TransportError,
    validate_host,
)


HELLO = {"protocol_version": 3, "agent_version": "0.3.0", "agent_sha256": "a" * 64,
         "capabilities": sorted(REQUIRED_CAPABILITIES)}


def child_argv(source: str) -> list[str]:
    return [sys.executable, "-u", "-c", source]


class TransportTests(unittest.TestCase):
    def test_fragmented_line_and_large_stderr_do_not_block(self) -> None:
        child = f"""
import json, os, sys, time
request = json.loads(sys.stdin.buffer.readline())
print(json.dumps({{'id': request['id'], 'result': {HELLO!r}}}), flush=True)
request = json.loads(sys.stdin.buffer.readline())
os.write(2, b'x' * 131072)
wire = json.dumps({{'id': request['id'], 'result': {{'ok': True}}}}).encode() + b'\\n'
os.write(1, wire[:7])
time.sleep(0.02)
os.write(1, wire[7:])
"""
        with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
            with SSHTransport("pi-desktop", timeout=3) as transport:
                self.assertEqual(transport.request("status"), {"ok": True})

    def test_timeout_does_not_retry_an_input_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "actions.txt"
            child = f"""
import json, pathlib, sys, time
request = json.loads(sys.stdin.buffer.readline())
print(json.dumps({{'id': request['id'], 'result': {HELLO!r}}}), flush=True)
request = json.loads(sys.stdin.buffer.readline())
with pathlib.Path({str(record)!r}).open('a') as stream:
    stream.write(request['method'] + '\\n')
time.sleep(5)
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    transport.request("hello")
                    transport.timeout = 0.15
                    with self.assertRaisesRegex(TransportError, "timed out"):
                        transport.request("click", {"x": 2, "y": 3})
            self.assertEqual(record.read_text().splitlines(), ["click"])

    def test_mismatched_response_id_is_rejected(self) -> None:
        child = f"""
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({{'id': request['id'], 'result': {HELLO!r}}}), flush=True)
request = json.loads(sys.stdin.readline())
print(json.dumps({{'id': request['id'] + 1, 'result': {{'ok': True}}}}), flush=True)
"""
        with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
            with SSHTransport("pi-desktop", timeout=2) as transport:
                with self.assertRaisesRegex(TransportError, "response id"):
                    transport.request("status")

    def test_host_alias_cannot_be_an_ssh_option_or_shell_fragment(self) -> None:
        for host in ("-oProxyCommand=bad", "user@pi", "pi;bad", "pi name", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_host(host)
        self.assertEqual(validate_host("raspberrypi-codex"), "raspberrypi-codex")

    @unittest.skipUnless(os.name == "nt", "Windows OpenSSH environment behavior")
    def test_filtered_mcp_environment_retains_windows_ssh_config_path(self) -> None:
        child = f"""
import json, os, sys
assert os.environ.get('PROGRAMDATA') == os.environ['SYSTEMDRIVE'] + '\\\\ProgramData'
request = json.loads(sys.stdin.readline())
print(json.dumps({{'id': request['id'], 'result': {HELLO!r}}}), flush=True)
request = json.loads(sys.stdin.readline())
print(json.dumps({{'id': request['id'], 'result': {{'ok': True}}}}), flush=True)
"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROGRAMDATA", None)
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    self.assertEqual(transport.request("status"), {"ok": True})

    def test_protocol_mismatch_rejects_before_input_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "methods.txt"
            child = f"""
import json, pathlib, sys
request = json.loads(sys.stdin.readline())
pathlib.Path({str(record)!r}).write_text(request['method'])
print(json.dumps({{'id': request['id'], 'result': {{**{HELLO!r}, 'protocol_version': 1}}}}), flush=True)
for line in sys.stdin:
    with pathlib.Path({str(record)!r}).open('a') as stream:
        stream.write(',' + json.loads(line)['method'])
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    with self.assertRaisesRegex(TransportError, "Deploy the latest agent") as caught:
                        transport.request("click", {"x": 1, "y": 1})
                    self.assertEqual(caught.exception.input_state, "not_started")
            self.assertEqual(record.read_text(), "hello")

    def test_structured_remote_rejection_keeps_healthy_connection(self) -> None:
        child = f"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'hello':
        result = {{'result': {HELLO!r}}}
    elif request['method'] == 'click':
        result = {{'error': {{'code': 'invalid_params', 'message': 'Outside visible desktop', 'input_state': 'not_started'}}}}
    else:
        result = {{'result': {{'ready': True}}}}
    print(json.dumps({{'id': request['id'], **result}}), flush=True)
"""
        with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
            with SSHTransport("pi-desktop", timeout=2) as transport:
                with self.assertRaises(RemoteAgentError) as caught:
                    transport.request("click", {"x": 9999, "y": 1})
                self.assertEqual((caught.exception.code, caught.exception.input_state), ("invalid_params", "not_started"))
                process = transport._process
                self.assertEqual(transport.request("health"), {"ready": True})
                self.assertIs(transport._process, process)

    def test_malformed_remote_error_discards_connection_conservatively(self) -> None:
        child = f"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    result = {{'result': {HELLO!r}}} if request['method'] == 'hello' else {{'error': {{'code': 'busy', 'message': 'Busy', 'input_state': ['not_started']}}}}
    print(json.dumps({{'id': request['id'], **result}}), flush=True)
"""
        with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
            with SSHTransport("pi-desktop", timeout=2) as transport:
                with self.assertRaisesRegex(TransportError, "malformed error") as caught:
                    transport.request("click", {"x": 1, "y": 1})
                self.assertEqual(caught.exception.input_state, "may_have_executed")
                self.assertIsNone(transport._process)

    def test_remote_error_message_limit_matches_agent_and_preserves_safe_rejection(self) -> None:
        for length in (1001, 1024, 1025):
            with self.subTest(length=length):
                child = f"""
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'hello':
        result = {{'result': {HELLO!r}}}
    elif request['method'] == 'click':
        result = {{'error': {{'code': 'invalid_params', 'message': 'x' * {length}, 'input_state': 'not_started'}}}}
    else:
        result = {{'result': {{'ready': True}}}}
    print(json.dumps({{'id': request['id'], **result}}), flush=True)
"""
                with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                    with SSHTransport("pi-desktop", timeout=2) as transport:
                        if length <= 1024:
                            with self.assertRaises(RemoteAgentError) as caught:
                                transport.request("click", {"x": 9999, "y": 1})
                            self.assertEqual(caught.exception.message, "x" * length)
                            self.assertEqual(caught.exception.input_state, "not_started")
                            process = transport._process
                            self.assertEqual(transport.request("health"), {"ready": True})
                            self.assertIs(transport._process, process)
                        else:
                            with self.assertRaisesRegex(TransportError, "malformed error") as caught:
                                transport.request("click", {"x": 9999, "y": 1})
                            self.assertEqual(caught.exception.input_state, "may_have_executed")
                            self.assertIsNone(transport._process)

    def test_disconnect_then_reconnect_negotiates_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "methods.txt"
            child = f"""
import json, pathlib, sys
for line in sys.stdin:
    request = json.loads(line)
    with pathlib.Path({str(record)!r}).open('a') as stream:
        stream.write(request['method'] + '\\n')
    result = {HELLO!r} if request['method'] == 'hello' else {{'ok': True}}
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
    if request['method'] == 'disconnect':
        break
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    self.assertEqual(transport.request("status"), {"ok": True})
                    self.assertEqual(transport.disconnect(), {"ok": True})
                    self.assertEqual(transport.request("status"), {"ok": True})
            self.assertEqual(record.read_text().splitlines(),
                             ["hello", "status", "disconnect", "hello", "status"])

    def test_next_explicit_request_reconnects_after_agent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "methods.txt"
            child = f"""
import json, pathlib, sys
for line in sys.stdin:
    request = json.loads(line)
    with pathlib.Path({str(record)!r}).open('a') as stream:
        stream.write(request['method'] + '\\n')
    result = {HELLO!r} if request['method'] == 'hello' else {{'ok': True}}
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
    if request['method'] == 'status':
        break
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    self.assertEqual(transport.request("status"), {"ok": True})
                    assert transport._process is not None
                    transport._process.wait(timeout=1)
                    self.assertEqual(transport.request("status"), {"ok": True})
            self.assertEqual(record.read_text().splitlines(), ["hello", "status", "hello", "status"])

    def test_lock_wait_timeout_does_not_cancel_active_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory) / "started"
            child = f"""
import json, pathlib, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'hello':
        result = {HELLO!r}
    else:
        pathlib.Path({str(started)!r}).write_text('started')
        time.sleep(0.2)
        result = {{'ready': True}}
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=1) as transport:
                    outcome: list[dict | BaseException] = []
                    thread = threading.Thread(target=lambda: outcome.append(transport.request("status")))
                    thread.start()
                    until = time.monotonic() + 1
                    while not started.exists() and time.monotonic() < until:
                        time.sleep(0.005)
                    self.assertTrue(started.exists())
                    process = transport._process
                    with self.assertRaises(TransportError) as caught:
                        transport.request("health", deadline=time.monotonic() + 0.03)
                    self.assertEqual(caught.exception.input_state, "not_started")
                    thread.join(timeout=2)
                    self.assertEqual(outcome, [{"ready": True}])
                    self.assertIs(transport._process, process)

    def test_handshake_and_requested_response_share_one_deadline(self) -> None:
        child = f"""
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    time.sleep(0.2 if request['method'] == 'hello' else 0.4)
    result = {HELLO!r} if request['method'] == 'hello' else {{'ready': True}}
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
"""
        with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
            with SSHTransport("pi-desktop", timeout=0.5) as transport:
                with self.assertRaisesRegex(TransportError, "timed out"):
                    transport.request("status")

    def test_write_and_response_share_one_deadline(self) -> None:
        child = f"""
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'status':
        time.sleep(0.4)
    result = {HELLO!r} if request['method'] == 'hello' else {{'ready': True}}
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
"""
        with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
            with SSHTransport("pi-desktop", timeout=2) as transport:
                transport.request("hello")
                transport.timeout = 0.5
                original_write = transport._write_request

                def slow_write(proc, wire: bytes, deadline: float) -> None:
                    if b'"method":"status"' in wire:
                        time.sleep(0.2)
                    original_write(proc, wire, deadline)

                with patch.object(transport, "_write_request", side_effect=slow_write):
                    with self.assertRaisesRegex(TransportError, "timed out"):
                        transport.request("status")


class DeployTests(unittest.TestCase):
    def _run_remote_code_locally(self, home: Path):
        real_run = subprocess.run

        def run(argv, **kwargs):
            command = shlex.split(argv[-1])
            self.assertEqual(command[:2], ["python3", "-c"])
            child_env = os.environ.copy()
            child_env["USERPROFILE"] = str(home)
            child_env["HOME"] = str(home)
            return real_run(
                [sys.executable, "-c", command[2]],
                input=kwargs["input"], capture_output=True,
                timeout=kwargs["timeout"], env=child_env, check=False,
            )

        return run

    def test_deploy_writes_packaged_agent_and_checks_remote_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "pi-home"
            home.mkdir()
            with patch("pi_desktop_bridge.cli.subprocess.run", side_effect=self._run_remote_code_locally(home)):
                digest = cli.deploy("pi-desktop")
            source = Path(cli.__file__).with_name("pi_agent.py").read_bytes()
            target = home / ".local" / "share" / "pi-desktop-bridge" / "pi_agent.py"
            self.assertEqual(target.read_bytes(), source)
            self.assertEqual(digest, hashlib.sha256(source).hexdigest())
            self.assertEqual(list(target.parent.glob(".pi_agent-*")), [])

    def test_deploy_rejects_a_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "pi-home"
            home.mkdir()
            outside = base / "outside"
            outside.mkdir()
            try:
                (home / ".local").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with patch("pi_desktop_bridge.cli.subprocess.run", side_effect=self._run_remote_code_locally(home)):
                with self.assertRaisesRegex(TransportError, "deployment failed"):
                    cli.deploy("pi-desktop")
            self.assertFalse((outside / "share").exists())


if __name__ == "__main__":
    unittest.main()
