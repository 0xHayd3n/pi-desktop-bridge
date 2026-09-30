"""Command-line entry points for MCP service, deployment, and diagnostics."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from .server import create_server
from .transport import SSHTransport, TransportError, _ssh_environment, ssh_argv, validate_host


_DEPLOY_CODE = """
import hashlib, json, os, pathlib, sys, tempfile
source = sys.stdin.buffer.read()
home = pathlib.Path.home()
local = home / '.local'
share = local / 'share'
base = share / 'pi-desktop-bridge'
for directory in (local, share, base):
    if directory.is_symlink():
        raise RuntimeError('agent path must not contain a symlink')
    directory.mkdir(mode=0o700, exist_ok=True)
os.chmod(base, 0o700)
target = base / 'pi_agent.py'
fd, temporary = tempfile.mkstemp(prefix='.pi_agent-', dir=base)
try:
    with os.fdopen(fd, 'wb') as stream:
        stream.write(source)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(json.dumps({'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))
""".strip()


def deploy(host: str) -> str:
    """Copy the packaged stdlib agent to the SSH user's private directory."""
    validate_host(host)
    agent_path = Path(__file__).with_name("pi_agent.py")
    source = agent_path.read_bytes()
    if not source:
        raise RuntimeError("packaged Pi agent is empty")
    expected = hashlib.sha256(source).hexdigest()
    command = f"python3 -c {shlex.quote(_DEPLOY_CODE)}"
    try:
        result = subprocess.run(
            ssh_argv(host, command), input=source, capture_output=True,
            timeout=45, check=False, env=_ssh_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TransportError("Agent deployment timed out") from exc
    if result.returncode != 0:
        raise TransportError(f"Agent deployment failed (SSH exit {result.returncode})")
    try:
        verification = json.loads(result.stdout.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError("Agent deployment returned invalid verification") from exc
    if not isinstance(verification, dict) or verification.get("sha256") != expected:
        raise TransportError("Agent deployment checksum mismatch")
    return expected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Raspberry Pi desktop bridge over SSH")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "deploy", "status", "screenshot"):
        sub = subparsers.add_parser(name)
        sub.add_argument(
            "--host", default=os.environ.get("PI_DESKTOP_SSH_HOST", "pi-desktop"),
            help="SSH hostname or alias (default: PI_DESKTOP_SSH_HOST or pi-desktop)",
        )
        if name == "screenshot":
            sub.add_argument("--output", required=True, type=Path, help="PNG output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_host(args.host)
        if args.command == "deploy":
            checksum = deploy(args.host)
            print(f"Deployed Pi agent to {args.host}; SHA-256 {checksum}")
        elif args.command == "serve":
            with SSHTransport(args.host) as transport:
                create_server(args.host, transport=transport).run(transport="stdio")
        else:
            with SSHTransport(args.host) as transport:
                if args.command == "status":
                    print(json.dumps(transport.request("status"), ensure_ascii=False, indent=2))
                elif args.command == "screenshot":
                    frame = transport.request("screenshot")
                    if frame.get("mime_type") != "image/png":
                        raise TransportError("SSH agent returned an unexpected image type")
                    encoded = frame.get("image_base64")
                    if not isinstance(encoded, str):
                        raise TransportError("SSH agent returned an invalid screenshot")
                    data = base64.b64decode(encoded, validate=True)
                    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise TransportError("SSH agent returned an invalid PNG screenshot")
                    args.output.write_bytes(data)
                    print(f"Wrote {args.output}")
        return 0
    except (OSError, ValueError, TransportError, RuntimeError) as exc:
        print(f"pi-desktop-bridge: {exc}", file=sys.stderr)
        return 1
