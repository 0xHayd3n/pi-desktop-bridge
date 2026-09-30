"""Command-line entry points for MCP service, deployment, and diagnostics."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from . import PROTOCOL_VERSION, __version__
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


def doctor(host: str) -> dict:
    """Check the deployed agent and desktop prerequisites without taking control."""
    validate_host(host)
    expected = hashlib.sha256(Path(__file__).with_name("pi_agent.py").read_bytes()).hexdigest()
    report = {
        "host": host, "client_version": __version__, "protocol_version": PROTOCOL_VERSION,
        "ready": False, "scope": "prerequisites", "checks": [], "next_steps": [],
    }
    try:
        with SSHTransport(host) as transport:
            health = transport.request("health")
    except (TransportError, OSError) as exc:
        # Do not dump SSH stderr, environment values or arbitrary child output.
        message = str(exc) if isinstance(exc, TransportError) else "The SSH client could not be started"
        report["checks"] = [{"name": "connection", "ok": False, "message": message[:1024]}]
        report["next_steps"] = [
            "Verify the SSH alias, trusted host key and password-free login in a normal terminal.",
            f"Deploy the current agent with pi-desktop-bridge deploy --host {host}, then reconnect.",
        ]
        return report
    if not isinstance(health, dict):
        health = {}
    checks = health.get("checks")
    digest = health.get("agent_sha256")
    if (type(health.get("desktop_ready")) is not bool
            or health.get("protocol_version") != PROTOCOL_VERSION
            or not isinstance(health.get("agent_version"), str) or len(health["agent_version"]) > 64
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(checks, list) or len(checks) > 32
            or any(not isinstance(check, dict) or not isinstance(check.get("name"), str)
                   or type(check.get("ok")) is not bool or not isinstance(check.get("message"), str)
                   for check in checks)):
        report["checks"] = [{"name": "agent_health", "ok": False,
                             "message": "The agent returned invalid health diagnostics"}]
        report["next_steps"] = [f"Deploy the current agent with pi-desktop-bridge deploy --host {host}, then reconnect."]
        return report
    matches = digest == expected
    report.update({
        "agent_version": health["agent_version"], "agent_sha256": digest,
        "agent_matches_client": matches, "desktop_ready": health["desktop_ready"],
        "session_active": health.get("session_active") is True,
        "checks": [{"name": check["name"][:128], "ok": check["ok"], "message": check["message"][:1024]}
                   for check in checks] + [{"name": "agent_source", "ok": matches,
                                           "message": "Deployed source matches this client" if matches else
                                           "Deployed source differs from this client; deploy and reconnect"}],
    })
    report["ready"] = matches and health["desktop_ready"] and all(check["ok"] for check in checks)
    if not matches:
        report["next_steps"].append(f"Deploy the current agent with pi-desktop-bridge deploy --host {host}, then reconnect.")
    if not health["desktop_ready"] or not all(check["ok"] for check in checks):
        report["next_steps"].append("Resolve the failed prerequisite checks under the Pi's desktop user, then rerun doctor.")
    return report


def _capture_width(value: str) -> int:
    try:
        width = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("capture width must be an integer from 1 to 65535") from exc
    if not 1 <= width <= 65_535:
        raise argparse.ArgumentTypeError("capture width must be an integer from 1 to 65535")
    return width


def _idle_timeout(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("idle timeout must be an integer from 0 to 3600 seconds") from exc
    if not 0 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("idle timeout must be an integer from 0 to 3600 seconds")
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Raspberry Pi desktop bridge over SSH")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "deploy", "doctor", "status", "screenshot"):
        sub = subparsers.add_parser(name)
        sub.add_argument(
            "--host", default=os.environ.get("PI_DESKTOP_SSH_HOST", "pi-desktop"),
            help="SSH hostname or alias (default: PI_DESKTOP_SSH_HOST or pi-desktop)",
        )
        if name == "screenshot":
            sub.add_argument("--output", required=True, type=Path, help="PNG output path")
        if name == "serve":
            sub.add_argument("--capture-max-width", type=_capture_width,
                             help="Maximum width of post-action images; explicit screenshots stay independent")
            sub.add_argument("--idle-timeout", type=_idle_timeout, default=300,
                             help="Release idle desktop sessions after this many seconds (default: 300; 0 disables)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_host(args.host)
        if args.command == "deploy":
            checksum = deploy(args.host)
            print(f"Deployed Pi agent to {args.host}; SHA-256 {checksum}")
        elif args.command == "doctor":
            report = doctor(args.host)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ready"] else 1
        elif args.command == "serve":
            with SSHTransport(args.host) as transport:
                create_server(args.host, transport=transport,
                              capture_max_width=args.capture_max_width,
                              idle_timeout=args.idle_timeout).run(transport="stdio")
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
