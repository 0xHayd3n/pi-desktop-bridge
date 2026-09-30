"""Exercise actual pipe framing and failure behavior without an SSH server."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pi_desktop_bridge import cli
from pi_desktop_bridge.transport import SSHTransport, TransportError, validate_host


def child_argv(source: str) -> list[str]:
    return [sys.executable, "-u", "-c", source]


class TransportTests(unittest.TestCase):
    def test_fragmented_line_and_large_stderr_do_not_block(self) -> None:
        child = """
import json, os, sys, time
request = json.loads(sys.stdin.buffer.readline())
os.write(2, b'x' * 131072)
wire = json.dumps({'id': request['id'], 'result': {'ok': True}}).encode() + b'\\n'
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
with pathlib.Path({str(record)!r}).open('a') as stream:
    stream.write(request['method'] + '\\n')
time.sleep(5)
"""
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=0.15) as transport:
                    with self.assertRaisesRegex(TransportError, "timed out"):
                        transport.request("click", {"x": 2, "y": 3})
            self.assertEqual(record.read_text().splitlines(), ["click"])

    def test_mismatched_response_id_is_rejected(self) -> None:
        child = """
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({'id': request['id'] + 1, 'result': {'ok': True}}), flush=True)
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
        child = """
import json, os, sys
assert os.environ.get('PROGRAMDATA') == os.environ['SYSTEMDRIVE'] + '\\\\ProgramData'
request = json.loads(sys.stdin.readline())
print(json.dumps({'id': request['id'], 'result': {'ok': True}}), flush=True)
"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROGRAMDATA", None)
            with patch("pi_desktop_bridge.transport.ssh_argv", return_value=child_argv(child)):
                with SSHTransport("pi-desktop", timeout=2) as transport:
                    self.assertEqual(transport.request("status"), {"ok": True})


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
