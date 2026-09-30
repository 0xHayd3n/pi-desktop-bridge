"""Exercise the real MCP server against an isolated, disposable Pi test window."""

import argparse
import asyncio
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from pi_desktop_bridge.transport import SSHTransport


ROOT = Path(__file__).resolve().parents[1]


def remote_python(host, source):
    result = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=8", host, "python3 -"],
        input=source, text=True, encoding="utf-8", capture_output=True, timeout=25,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Remote verification command failed")
    return json.loads(result.stdout)


def start_fixture(host):
    code = base64.b64encode((ROOT / "scripts/live_fixture.py").read_bytes()).decode("ascii")
    return remote_python(host, f'''
import base64, json, os, pathlib, subprocess, tempfile, time, shutil
base=pathlib.Path('/run/user/'+str(os.getuid()))
directory=pathlib.Path(tempfile.mkdtemp(prefix='pi-bridge-verification-', dir=base))
directory.chmod(0o700)
script=directory/'fixture.py'; script.write_bytes(base64.b64decode({code!r}))
script.chmod(0o600)
state=directory/'state.json'
env=os.environ.copy(); env['DISPLAY']=':0'; env['XDG_RUNTIME_DIR']=str(base)
with open(directory/'stderr.log','w') as log:
    process=subprocess.Popen(['python3',str(script),str(state)], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
try:
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        if state.exists():
            data=json.loads(state.read_text())
            if 'geometry' in data:
                print(json.dumps({{'directory':str(directory),'pid':process.pid,'state':data}}))
                break
        if process.poll() is not None:
            raise RuntimeError((directory/'stderr.log').read_text())
        time.sleep(0.05)
    else: raise RuntimeError('Test window did not become ready')
except BaseException:
    process.terminate()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired: process.kill(); process.wait()
    shutil.rmtree(directory)
    raise
''')


def fixture_state(host, fixture):
    path = fixture["directory"] + "/state.json"
    return remote_python(host, f"import json,pathlib\nprint(pathlib.Path({path!r}).read_text())\n")


def wait_for_state(host, fixture, expected):
    """Wait for the test app to process input without ever repeating input."""
    path = fixture["directory"] + "/state.json"
    return remote_python(host, f'''
import json,pathlib,time
path=pathlib.Path({path!r}); expected={expected!r}
deadline=time.monotonic()+5
while True:
    state=json.loads(path.read_text())
    if all(state.get(key)==value for key,value in expected.items()): break
    if time.monotonic()>=deadline: break
    time.sleep(0.05)
print(json.dumps(state))
''')


def stop_fixture(host, fixture):
    return remote_python(host, f'''
import json, os, pathlib, signal, shutil, time
directory=pathlib.Path({fixture['directory']!r})
expected=pathlib.Path('/run/user/'+str(os.getuid()))
if directory.parent != expected or not directory.name.startswith('pi-bridge-verification-'):
    raise RuntimeError('Unsafe fixture cleanup path')
pid={fixture['pid']!r}
try:
    args=pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0')
    if str(directory/'fixture.py').encode() not in args:
        raise RuntimeError('Fixture process identity changed')
    os.kill(pid,signal.SIGTERM)
except ProcessLookupError: pass
except FileNotFoundError: pass
shutil.rmtree(directory)
print(json.dumps({{'fixture_removed':not directory.exists()}}))
''')


async def exercise(host, output):
    output.mkdir(parents=True, exist_ok=True)
    fixture = await asyncio.to_thread(start_fixture, host)
    report = {"checks": {}, "tools": []}
    try:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pi_desktop_bridge", "serve", "--host", host],
            cwd=str(ROOT),
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                report["tools"] = [tool.name for tool in (await session.list_tools()).tools]
                assert len(report["tools"]) == 11, report["tools"]

                async def call(name, arguments=None, image_name=None):
                    result = await session.call_tool(name, arguments or {})
                    if result.isError:
                        raise RuntimeError(str(result.content))
                    images = [item for item in result.content if isinstance(item, types.ImageContent)]
                    if name not in {"desktop_status", "desktop_health", "desktop_disconnect"} and not images:
                        raise RuntimeError(f"{name} did not return a model-visible screenshot")
                    if image_name:
                        data = base64.b64decode(images[0].data, validate=True)
                        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise RuntimeError("Screenshot is not a PNG")
                        (output / image_name).write_bytes(data)
                    return result

                def json_content(result):
                    texts = [item.text for item in result.content if isinstance(item, types.TextContent)]
                    assert texts, result
                    return json.loads(texts[0])

                def another_client_can_acquire():
                    with SSHTransport(host) as transport:
                        return transport.request("status")

                health = json_content(await call("desktop_health"))
                assert health["desktop_ready"] and health["protocol_version"] == 4, health
                assert health["session_active"] is False, health
                report["checks"]["lease_free_health"] = True
                rejected = await session.call_tool("desktop_click", {"x": 0, "y": 0, "button": "invalid"})
                assert rejected.isError, rejected
                error = " ".join(item.text for item in rejected.content if isinstance(item, types.TextContent))
                assert "button" in error and "may have executed" not in error, error
                assert json_content(await call("desktop_health"))["session_active"] is False
                await asyncio.to_thread(another_client_can_acquire)
                report["checks"]["rejected_first_input_releases_desktop"] = True

                await call("desktop_status")
                report["checks"]["mcp_initialize_status"] = True
                await call("desktop_screenshot", image_name="before.png")
                report["checks"]["model_visible_image"] = True
                assert json_content(await call("desktop_health"))["session_active"] is True
                rejected = await session.call_tool("desktop_click", {"x": 0, "y": 0, "count": 9})
                assert rejected.isError
                assert json_content(await call("desktop_health"))["session_active"] is True
                report["checks"]["validation_error_keeps_session"] = True
                geometry = fixture["state"]["geometry"]

                def center(name):
                    box = geometry[name]
                    return {"x": box["x"] + box["width"] // 2,
                            "y": box["y"] + box["height"] // 2}

                # Begin away from the button so a click must reach its requested
                # coordinate rather than accidentally using the previous cursor.
                await call("desktop_move", center("canvas"))
                await call("desktop_click", {**center("button"), "button": "left", "count": 1})
                state = await asyncio.to_thread(wait_for_state, host, fixture, {"clicks": 1})
                assert state["clicks"] == 1, state
                report["checks"]["click"] = True
                await call("desktop_click", {**center("entry"), "button": "left", "count": 1})
                phrase = "Pi bridge verified — café ✓"
                await call("desktop_type", {"text": phrase})
                state = await asyncio.to_thread(wait_for_state, host, fixture, {"text": phrase})
                assert state["text"] == phrase, state
                report["checks"]["unicode_type"] = True
                await call("desktop_key", {"keys": ["Control_L", "a"]})
                await call("desktop_type", {"text": "Screenshot and input verified"})
                state = await asyncio.to_thread(wait_for_state, host, fixture,
                                                {"hotkeys": 1, "text": "Screenshot and input verified"})
                assert state["hotkeys"] == 1 and state["text"] == "Screenshot and input verified", state
                report["checks"]["hotkey"] = True
                await call("desktop_disconnect")
                await call("desktop_disconnect")  # Idempotent; should not start another agent.
                await asyncio.to_thread(another_client_can_acquire)
                report["checks"]["release_allows_another_client"] = True
                await call("desktop_scroll", {"direction": "down", "ticks": 2, **center("canvas")})
                state = await asyncio.to_thread(wait_for_state, host, fixture, {"scrolls": 2})
                assert state["scrolls"] == 2, state
                report["checks"]["scroll"] = True
                report["checks"]["targeted_first_scroll_after_reconnect"] = True
                canvas = geometry["canvas"]
                await call("desktop_drag", {
                    "start_x": canvas["x"] + 60, "start_y": canvas["y"] + 65,
                    "end_x": canvas["x"] + 400, "end_y": canvas["y"] + 115,
                }, image_name="after.png")
                state = await asyncio.to_thread(wait_for_state, host, fixture,
                                                {"drag": [[60, 65], [400, 115]]})
                assert len(state["drag"]) == 2, state
                assert state["drag"][0] == [60, 65] and state["drag"][1] == [400, 115], state
                report["checks"]["drag"] = True
    finally:
        report["checks"].update(await asyncio.to_thread(stop_fixture, host, fixture))
        (output / "live-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "_local/live")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.host):
        parser.error("Use a concrete SSH host alias")
    report = asyncio.run(exercise(args.host, args.output.resolve()))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
