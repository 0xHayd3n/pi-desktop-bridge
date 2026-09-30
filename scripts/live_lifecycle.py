"""Opt-in acceptance test for idle release on a real Pi and MCP server.

Uses an owned disposable fixture; only mapped pointer movement sends input.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from pi_desktop_bridge.transport import RemoteAgentError, SSHTransport, validate_host

import live_smoke
from live_views import image_point


ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def mcp_client(host: str, idle_timeout: int):
    parameters = StdioServerParameters(command=sys.executable,
                                      args=["-m", "pi_desktop_bridge", "serve", "--host", host,
                                            "--idle-timeout", str(idle_timeout), "--capture-max-width", "960"])
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def call(session: ClientSession, name: str, arguments=None) -> tuple[dict, object]:
    result = await session.call_tool(name, arguments or {})
    assert not result.isError, result
    text = next(item.text for item in result.content if isinstance(item, types.TextContent))
    metadata = {"description": text} if name == "desktop_disconnect" else json.loads(text)
    return metadata, result


async def contender_busy(contender: SSHTransport):
    try:
        await asyncio.to_thread(contender.request, "status")
    except RemoteAgentError as error:
        assert error.code == "busy" and error.input_state == "not_started"
    else:
        raise AssertionError("The contender acquired an already-held desktop")


async def exercise(host: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report: dict = {"checks": {}}
    fixture = await asyncio.to_thread(live_smoke.start_fixture, host)
    contender = SSHTransport(host)
    try:
        async with mcp_client(host, 2) as session:
            health, _ = await call(session, "desktop_health")
            assert health["agent_version"] == "0.5.0" and not health["session_active"]
            assert health["session_policy"]["idle_timeout_seconds"] == 2
            view, image = await call(session, "desktop_screenshot", {"max_width": 960})
            assert any(isinstance(item, types.ImageContent) for item in image.content)
            await contender_busy(contender)
            report["checks"]["exclusive_lease_before_idle"] = True

            started = time.monotonic()
            polls = 0
            async with asyncio.timeout(8):
                while True:
                    health, _ = await call(session, "desktop_health")
                    polls += 1
                    if health["session_policy"]["auto_release_count"] == 1:
                        break
                    await asyncio.sleep(0.15)
            assert not health["session_active"] and not health["session_policy"]["local_session_active"]
            assert not health["observation_required"]
            report["idle_result"] = {"health_polls": polls, "seconds_after_contender_probe": time.monotonic() - started,
                                     "policy": health["session_policy"]}
            report["checks"]["health_polls_do_not_extend_idle"] = True

            canvas = fixture["state"]["geometry"]["canvas"]
            point = image_point(view, canvas["x"] + 5, canvas["y"] + 5)
            stale = await session.call_tool("desktop_move", {**point, "view_id": view["view_id"]})
            assert stale.isError and "stale_view" in str(stale.content), stale
            report["checks"]["idle_invalidates_view"] = True
            acquired = await asyncio.to_thread(contender.request, "status")
            assert acquired["width"] == view["desktop_width"]
            report["checks"]["another_client_acquires_after_idle"] = True
            blocked = await session.call_tool("desktop_screenshot", {"max_width": 960})
            assert blocked.isError and "controlled by another" in str(blocked.content), blocked
            health, _ = await call(session, "desktop_health")
            assert health["session_policy"]["auto_release_count"] == 1 and not health["observation_required"]
            await asyncio.to_thread(contender.disconnect)

            current, _ = await call(session, "desktop_screenshot", {"max_width": 960})
            point = image_point(current, canvas["x"] + 5, canvas["y"] + 5)
            moved, _ = await call(session, "desktop_move", {**point, "view_id": current["view_id"]})
            assert moved["width"] == 960
            report["checks"]["fresh_observation_reconnects_and_maps_input"] = True

            # This real operation lasts longer than the idle interval. Cleanup
            # must wait for it and re-check the renewed activity under the lock.
            region = {"x": canvas["x"] + 455, "y": canvas["y"] + 65, "width": 40, "height": 40}
            started = time.monotonic()
            waited, _ = await call(session, "desktop_wait_for_stable", {
                **region, "stable_ms": 2000, "timeout_ms": 3500, "poll_ms": 200, "max_width": 20,
            })
            elapsed = time.monotonic() - started
            assert waited["stability"]["stable"] and elapsed > 2, waited
            health, _ = await call(session, "desktop_health")
            assert health["session_active"] and health["session_policy"]["local_session_active"]
            assert health["session_policy"]["auto_release_count"] == 1
            await contender_busy(contender)
            report["active_operation"] = {"tool_seconds": elapsed, "stability": waited["stability"]}
            report["checks"]["active_wait_not_interrupted_by_idle_timer"] = True
            await call(session, "desktop_disconnect")

        async with mcp_client(host, 0) as session:
            await call(session, "desktop_screenshot", {"max_width": 960})
            started = time.monotonic()
            while time.monotonic() - started < 3:
                await asyncio.sleep(0.25)
                health, _ = await call(session, "desktop_health")
                assert health["session_active"] and health["session_policy"]["idle_timeout_seconds"] == 0
                assert health["session_policy"]["auto_release_count"] == 0
                assert health["session_policy"]["idle_remaining_seconds"] is None
            await contender_busy(contender)
            report["checks"]["zero_disables_idle_release"] = True
            # No disconnect tool here: normal stdio shutdown owns final cleanup.
        acquired = await asyncio.to_thread(contender.request, "status")
        assert acquired["width"] == view["desktop_width"]
        report["checks"]["stdio_shutdown_releases_desktop"] = True
    finally:
        await asyncio.to_thread(contender.close)
        report["cleanup"] = await asyncio.to_thread(live_smoke.stop_fixture, host, fixture)
        (output / "live-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="pi-desktop")
    parser.add_argument("--output", type=Path, default=ROOT / "_local" / "live-lifecycle")
    args = parser.parse_args()
    validate_host(args.host)
    print(json.dumps(asyncio.run(exercise(args.host, args.output)), indent=2))


if __name__ == "__main__":
    main()
