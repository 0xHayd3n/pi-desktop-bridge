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
    AGENT_BOOTSTRAP, EXPECTED_AGENT_SHA256, REQUIRED_CAPABILITIES, RemoteAgentError, SSHTransport, TransportError,
    validate_host,
)


HELLO = {"protocol_version": 4, "agent_version": "0.5.0", "agent_sha256": EXPECTED_AGENT_SHA256,
         "capabilities": sorted(REQUIRED_CAPABILITIES)}
MISMATCH_SHA256 = "0" * 64 if EXPECTED_AGENT_SHA256 != "0" * 64 else "1" * 64


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

    def test_source_mismatch_preserves_diagnostics_and_blocks_all_desktop_methods(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "methods.txt"
            stale_hello = {**HELLO, "agent_sha256": MISMATCH_SHA256}
            child = f"""
import json, pathlib, sys
for line in sys.stdin:
    request = json.loads(line)
    with pathlib.Path({str(record)!r}).open('a') as stream:
        stream.write(request['method'] + '\\n')
    result = {stale_hello!r} if request['method'] == 'hello' else {{'ready': True}}
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
    if request['method'] == 'disconnect':
        break
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    self.assertEqual(transport.request("hello"), stale_hello)
                    process = transport._process
                    self.assertEqual(transport.request("health"), {"ready": True})
                    for method in (
                        "status", "screenshot", "wait_for_stable", "move", "click",
                        "drag", "scroll", "type_text", "key",
                    ):
                        with self.subTest(method=method), self.assertRaises(TransportError) as caught:
                            transport.request(method)
                        self.assertEqual(caught.exception.code, "agent_source_mismatch")
                        self.assertEqual(caught.exception.input_state, "not_started")
                        self.assertIn("Deploy the latest agent", str(caught.exception))
                        self.assertIn("restart", str(caught.exception))
                    self.assertIs(transport._process, process)
                    self.assertEqual(transport.disconnect(), {"ready": True})
            self.assertEqual(record.read_text().splitlines(), ["hello", "health", "disconnect"])

    def test_malformed_hello_source_or_version_rejects_before_desktop_request(self) -> None:
        invalid_hellos = {
            "missing_hash": {key: value for key, value in HELLO.items() if key != "agent_sha256"},
            "uppercase_hash": {**HELLO, "agent_sha256": "A" * 64},
            "short_hash": {**HELLO, "agent_sha256": "a" * 63},
            "nonhex_hash": {**HELLO, "agent_sha256": "g" * 64},
            "nonstring_hash": {**HELLO, "agent_sha256": 123},
            "missing_version": {key: value for key, value in HELLO.items() if key != "agent_version"},
            "empty_version": {**HELLO, "agent_version": ""},
            "blank_version": {**HELLO, "agent_version": " "},
            "long_version": {**HELLO, "agent_version": "x" * 65},
        }
        for name, invalid_hello in invalid_hellos.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                record = Path(directory) / "methods.txt"
                child = f"""
import json, pathlib, sys
for line in sys.stdin:
    request = json.loads(line)
    with pathlib.Path({str(record)!r}).open('a') as stream:
        stream.write(request['method'] + '\\n')
    print(json.dumps({{'id': request['id'], 'result': {invalid_hello!r}}}), flush=True)
"""
                with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                    with SSHTransport("pi-desktop", timeout=2) as transport:
                        with self.assertRaises(TransportError) as caught:
                            transport.request("click", {"x": 1, "y": 1})
                        self.assertEqual(caught.exception.code, "incompatible_agent")
                        self.assertEqual(caught.exception.input_state, "not_started")
                        self.assertIsNone(transport._process)
                self.assertEqual(record.read_text().splitlines(), ["hello"])

    def test_each_new_ssh_session_rechecks_source_and_matching_actions_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launch_count = Path(directory) / "launch_count.txt"
            record = Path(directory) / "methods.txt"
            child = f"""
import json, pathlib, sys
counter = pathlib.Path({str(launch_count)!r})
launch = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(launch))
hello = {HELLO!r}
if launch == 2:
    hello['agent_sha256'] = {MISMATCH_SHA256!r}
for line in sys.stdin:
    request = json.loads(line)
    with pathlib.Path({str(record)!r}).open('a') as stream:
        stream.write(str(launch) + ':' + request['method'] + '\\n')
    result = hello if request['method'] == 'hello' else {{'ok': True}}
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
    if request['method'] == 'disconnect':
        break
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    self.assertEqual(transport.request("click", {"x": 1, "y": 1}), {"ok": True})
                    transport.disconnect()
                    with self.assertRaises(TransportError) as caught:
                        transport.request("status")
                    self.assertEqual((caught.exception.code, caught.exception.input_state),
                                     ("agent_source_mismatch", "not_started"))
                    self.assertEqual(transport.request("health"), {"ok": True})
                    transport.disconnect()
                    self.assertEqual(transport.request("click", {"x": 2, "y": 2}), {"ok": True})
            self.assertEqual(record.read_text().splitlines(), [
                "1:hello", "1:click", "1:disconnect", "2:hello", "2:health",
                "2:disconnect", "3:hello", "3:click",
            ])

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


class SnapshotStartupTests(unittest.TestCase):
    def test_atomic_replacement_during_startup_reports_executed_bytes_and_blocks_input(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".local/share/pi-desktop-bridge/pi_agent.py"
            target.parent.mkdir(parents=True)
            replacement = b"AGENT_VERSION = '0.5.0'\n# newer source with the same version\n"
            replacement_digest = hashlib.sha256(replacement).hexdigest()
            source = f'''
import hashlib,json,os,pathlib,sys
AGENT_VERSION='0.5.0'
path=pathlib.Path(__file__)
temporary=path.with_suffix('.new')
temporary.write_bytes({replacement!r})
os.replace(temporary,path)
# Reproduce the old self-report race: module imports see the replacement file.
AGENT_SHA256=hashlib.sha256(path.read_bytes()).hexdigest()
if __name__ == '__main__':
    raise AssertionError('Agent ran before its snapshot hash was bound')
def main():
    for line in sys.stdin:
        request=json.loads(line)
        with (path.parent/'methods.txt').open('a') as record:
            record.write(request['method']+'\\n')
        if request['method']=='hello':
            result={{**{HELLO!r},'agent_sha256':AGENT_SHA256,'agent_version':AGENT_VERSION}}
        elif request['method']=='health':
            result={{'agent_sha256':AGENT_SHA256}}
        else:
            result={{'ok':True}}
        print(json.dumps({{'id':request['id'],'result':result}}),flush=True)
        if request['method']=='disconnect': break
'''.encode()
            target.write_bytes(source)
            environment = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(AGENT_BOOTSTRAP)), \
                    patch("pi_desktop_bridge.transport._ssh_environment", return_value=environment), \
                    patch("pi_desktop_bridge.transport.EXPECTED_AGENT_SHA256", replacement_digest):
                with SSHTransport("pi-desktop", timeout=3) as connection:
                    hello = connection.request("hello")
                    self.assertEqual(hello["agent_version"], "0.5.0")
                    self.assertEqual(hello["agent_sha256"], hashlib.sha256(source).hexdigest())
                    self.assertEqual(target.read_bytes(), replacement)
                    for method in ("status", "screenshot", "click"):
                        with self.subTest(method=method), self.assertRaises(TransportError) as caught:
                            connection.request(method, {"x": 1, "y": 1} if method == "click" else {})
                        self.assertEqual((caught.exception.code, caught.exception.input_state),
                                         ("agent_source_mismatch", "not_started"))
                    self.assertEqual(connection.request("health")["agent_sha256"], hello["agent_sha256"])
                    self.assertEqual(connection.disconnect(), {"ok": True})
            self.assertEqual((target.parent / "methods.txt").read_text().splitlines(),
                             ["hello", "health", "disconnect"])

    def test_snapshot_size_limit_rejects_before_execution(self):
        for source in (b"", b"x" * 1_048_577):
            with self.subTest(size=len(source)), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                target = home / ".local/share/pi-desktop-bridge/pi_agent.py"
                target.parent.mkdir(parents=True)
                target.write_bytes(source)
                result = subprocess.run(child_argv(AGENT_BOOTSTRAP), capture_output=True,
                                        timeout=3, env={**os.environ, "USERPROFILE": str(home), "HOME": str(home)})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"source is empty or exceeds", result.stderr)


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
