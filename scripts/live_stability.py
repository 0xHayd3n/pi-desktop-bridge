"""Opt-in Pi acceptance test for visual waits and resized action images.

Input and animation affect only an owned disposable Tk fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import secrets
import sys
import time

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from pi_desktop_bridge.transport import validate_host

import live_smoke
from live_views import image_point, png_rgb


ROOT = Path(__file__).resolve().parents[1]


def visual_command(host: str, fixture: dict, mode: str) -> dict:
    command_id = secrets.token_hex(8)
    return live_smoke.remote_python(host, f'''
import json,os,pathlib,time
directory=pathlib.Path({fixture['directory']!r})
if directory.parent != pathlib.Path('/run/user/'+str(os.getuid())) or not directory.name.startswith('pi-bridge-verification-'):
    raise RuntimeError('Unsafe fixture command path')
temporary=directory/'command.tmp'
temporary.write_text(json.dumps({{'id':{command_id!r},'mode':{mode!r}}}),encoding='utf-8')
os.replace(temporary,directory/'command.json')
deadline=time.monotonic()+3
while True:
    state=json.loads((directory/'state.json').read_text())
    if state['visual'].get('command_id')=={command_id!r}: break
    if time.monotonic()>=deadline: raise RuntimeError('Fixture command not observed')
    time.sleep(0.01)
print(json.dumps(state))
''')


async def exercise(host: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report: dict = {"checks": {}, "action_images": {}, "waits": {}}
    fixture = await asyncio.to_thread(live_smoke.start_fixture, host)
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "pi_desktop_bridge", "serve", "--host", host,
                                        "--capture-max-width", "960"])
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert len(tools) == 11 and "desktop_wait_for_stable" in tools
                for name in ("desktop_move", "desktop_click", "desktop_drag", "desktop_scroll", "desktop_type", "desktop_key"):
                    assert "capture_max_width" in tools[name].inputSchema["properties"]
                report["checks"]["mcp_schemas"] = True

                async def call(name: str, arguments=None, *, filename=None):
                    started = time.monotonic()
                    result = await session.call_tool(name, arguments or {})
                    assert not result.isError, result
                    text = next(item.text for item in result.content if isinstance(item, types.TextContent))
                    metadata = {"description": text} if name == "desktop_disconnect" else json.loads(text)
                    images = [item for item in result.content if isinstance(item, types.ImageContent)]
                    data = base64.b64decode(images[0].data, validate=True) if images else None
                    if data is not None:
                        width, height, _ = await asyncio.to_thread(png_rgb, data)
                        assert (width, height) == (metadata["width"], metadata["height"])
                    if filename:
                        (output / filename).write_bytes(data)
                    return metadata, data, time.monotonic() - started

                health, _, _ = await call("desktop_health")
                assert health["observation_required"] is False and health["recovery_action"] is None
                assert health["session_active"] is False and health["protocol_version"] == 4
                report["checks"]["recovery_state_visible"] = True
                canvas = fixture["state"]["geometry"]["canvas"]
                entry = fixture["state"]["geometry"]["entry"]

                async def action(name, arguments):
                    metadata, data, elapsed = await call(name, arguments)
                    assert metadata["width"] == min(960, metadata["desktop_width"])
                    assert metadata["region"] == {"x": 0, "y": 0, "width": metadata["desktop_width"],
                                                  "height": metadata["desktop_height"]}
                    report["action_images"][name] = {"width": metadata["width"], "bytes": len(data), "seconds": elapsed}
                    return metadata

                view = await action("desktop_move", {"x": canvas["x"] + 5, "y": canvas["y"] + 5})
                point = image_point(view, entry["x"] + entry["width"] // 2, entry["y"] + entry["height"] // 2)
                await action("desktop_click", {**point, "view_id": view["view_id"]})
                message = "v0.4 Pi Unicode — café"
                await action("desktop_type", {"text": message})
                state = await asyncio.to_thread(live_smoke.wait_for_state, host, fixture, {"text": message})
                assert state["text"] == message
                await action("desktop_key", {"keys": ["Control_L", "a"]})
                await action("desktop_drag", {"start_x": canvas["x"] + 70, "start_y": canvas["y"] + 80,
                                               "end_x": canvas["x"] + 250, "end_y": canvas["y"] + 110})
                await action("desktop_scroll", {"direction": "down", "ticks": 2,
                                                 "x": canvas["x"] + 250, "y": canvas["y"] + 110})
                state = await asyncio.to_thread(live_smoke.wait_for_state, host, fixture, {"scrolls": 2, "hotkeys": 1})
                assert state["hotkeys"] == 1 and state["scrolls"] == 2 and len(state["drag"]) == 2, state
                report["checks"]["all_six_resized_actions"] = True
                report["fixture_input_receipt"] = state

                override, _, _ = await call("desktop_move", {"x": canvas["x"] + 5, "y": canvas["y"] + 5,
                                                            "capture_max_width": 480})
                assert override["width"] == min(480, override["desktop_width"])
                native, native_png, _ = await call("desktop_screenshot")
                assert native["width"] == native["desktop_width"]
                report["native_png_bytes"] = len(native_png)
                report["checks"]["width_override_and_explicit_independence"] = True

                region = {"x": canvas["x"] + 455, "y": canvas["y"] + 65, "width": 50, "height": 50}
                await asyncio.to_thread(visual_command, host, fixture, "settle")
                settled, settled_png, elapsed = await call("desktop_wait_for_stable", {
                    **region, "stable_ms": 300, "timeout_ms": 3000, "poll_ms": 50, "max_width": 10,
                }, filename="settled.png")
                assert settled["stability"]["stable"] and not settled["stability"]["timed_out"], settled
                sw, sh, rgb = await asyncio.to_thread(png_rgb, settled_png)
                assert (sw, sh) == (10, 10) and rgb == b"\0\xff\0" * 100
                report["waits"]["settled"] = {**settled["stability"], "tool_seconds": elapsed, "bytes": len(settled_png)}
                report["checks"]["changed_to_stable_final_pixels"] = True
                mapped_move = await session.call_tool("desktop_move", {"x": 5, "y": 5, "view_id": settled["view_id"]})
                assert not mapped_move.isError, mapped_move
                stale = await session.call_tool("desktop_move", {"x": 5, "y": 5, "view_id": settled["view_id"]})
                assert stale.isError, stale
                report["checks"]["wait_view_mapping_and_stale_rejection"] = True
                # Keep the pointer out of the region while checking animation.
                await call("desktop_move", {"x": canvas["x"] + 5, "y": canvas["y"] + 5})
                await asyncio.to_thread(visual_command, host, fixture, "animate")
                moving, moving_png, elapsed = await call("desktop_wait_for_stable", {
                    **region, "stable_ms": 300, "timeout_ms": 1200, "poll_ms": 50, "max_width": 10,
                }, filename="timed-out.png")
                assert moving["stability"]["timed_out"] and not moving["stability"]["stable"], moving
                _, _, rgb = await asyncio.to_thread(png_rgb, moving_png)
                assert rgb in (b"\xff\0\0" * 100, b"\0\0\xff" * 100)
                report["waits"]["animated"] = {**moving["stability"], "tool_seconds": elapsed, "bytes": len(moving_png)}
                report["checks"]["animation_timeout_with_valid_image"] = True

                # A changing area outside the monitored ROI must not prevent settling.
                quiet_region = {"x": canvas["x"] + 80, "y": canvas["y"] + 130, "width": 60, "height": 10}
                quiet, _, _ = await call("desktop_wait_for_stable", {
                    **quiet_region, "stable_ms": 200, "timeout_ms": 2000, "poll_ms": 50,
                })
                assert quiet["stability"]["stable"], quiet
                report["checks"]["unrelated_animation_excluded"] = True
                await asyncio.to_thread(visual_command, host, fixture, "idle")
                await call("desktop_disconnect")
                health, _, _ = await call("desktop_health")
                assert health["session_active"] is False and health["observation_required"] is False
                report["checks"]["session_released"] = True
    finally:
        report["cleanup"] = await asyncio.to_thread(live_smoke.stop_fixture, host, fixture)
        (output / "live-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="pi-desktop")
    parser.add_argument("--output", type=Path, default=ROOT / "_local" / "live-stability")
    args = parser.parse_args()
    validate_host(args.host)
    print(json.dumps(asyncio.run(exercise(args.host, args.output)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
