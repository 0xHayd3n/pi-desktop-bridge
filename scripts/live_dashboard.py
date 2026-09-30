"""Opt-in live localhost dashboard check against an owned Pi test window."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import httpx

from live_smoke import start_fixture, stop_fixture, wait_for_state


ROOT = Path(__file__).resolve().parents[1]


def exercise(host: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    fixture = start_fixture(host)
    process = None
    client = None
    temporary = None
    report = {"checks": [], "fixture_removed": False}
    try:
        # Launch capabilities never enter the published report or console.
        temporary = tempfile.TemporaryDirectory(prefix="pi-dashboard-live-")
        launch = Path(temporary.name) / "launch.log"
        with launch.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "pi_desktop_bridge", "ui", "--host", host],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        deadline = time.monotonic() + 15
        match = None
        while time.monotonic() < deadline and process.poll() is None:
            match = re.search(r"(http://127\.0\.0\.1:\d+)/#token=([A-Za-z0-9_-]+)", launch.read_text(encoding="utf-8", errors="replace"))
            if match:
                break
            time.sleep(.1)
        if match is None:
            raise RuntimeError("The local dashboard did not start")
        origin, token = match.groups()
        client = httpx.Client(base_url=origin, timeout=70,
                             headers={"Authorization": "Bearer " + token, "Origin": origin},
                             trust_env=False)
        assert httpx.get(origin + "/api/state", trust_env=False).status_code == 401
        assert client.get("/api/state", headers={"Origin": "https://example.invalid"}).status_code == 403
        report["checks"].append("localhost_access_boundary")

        def call(path, body=None):
            response = client.get(path) if body is None else client.post(path, json=body)
            if response.status_code != 200:
                # No raw response/connection details in test failure output.
                raise RuntimeError(f"Dashboard operation failed: {path} ({response.status_code})")
            return response.json()

        state = call("/api/connect", {"mode": "existing", "host": host, "deploy": True})["state"]
        assert state["connected"]
        report["checks"].append("alias_login_and_agent_deployment")
        image = call("/api/frame")
        metadata = image["frame"]["metadata"]
        assert image["frame"]["mime_type"] == "image/png"
        png = base64.b64decode(image["frame"]["image_base64"], validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        (output / "before.png").write_bytes(png)
        report["preview_size"] = [metadata["width"], metadata["height"]]
        report["desktop_size"] = [metadata["desktop_width"], metadata["desktop_height"]]
        report["checks"].append("real_png_preview")

        def action(name, arguments):
            return call("/api/action", {"generation": state["generation"], "action": name,
                "args": arguments, "desktop_width": metadata["desktop_width"],
                "desktop_height": metadata["desktop_height"]})

        def received(expected):
            observed = wait_for_state(host, fixture, expected)
            assert all(observed.get(key) == value for key, value in expected.items())
            return observed

        geometry = fixture["state"]["geometry"]
        def center(name):
            widget = geometry[name]
            return {"x": widget["x"] + widget["width"] // 2,
                    "y": widget["y"] + widget["height"] // 2}

        action("move", center("button"))
        action("click", {**center("button"), "button": "left", "count": 1})
        received({"clicks": 1})
        report["checks"].append("pointer_and_click")
        action("move", center("entry"))
        action("click", {**center("entry"), "button": "left", "count": 1})
        action("type", {"text": "Dashboard — café"})
        received({"text": "Dashboard — café"})
        action("key", {"keys": ["Control_L", "a"]})
        action("type", {"text": "Live dashboard verified"})
        received({"text": "Live dashboard verified"})
        report["checks"].append("unicode_text_and_keyboard")
        canvas = geometry["canvas"]
        action("scroll", {**center("canvas"), "direction": "down", "ticks": 2})
        received({"scrolls": 2})
        report["checks"].append("targeted_scroll")
        result = action("drag", {"start_x": canvas["x"] + 60, "start_y": canvas["y"] + 65,
                                  "end_x": canvas["x"] + 400, "end_y": canvas["y"] + 115})
        received({"drag": [[60, 65], [400, 115]]})
        report["checks"].append("drag")
        (output / "after.png").write_bytes(base64.b64decode(result["frame"]["image_base64"], validate=True))
        call("/api/disconnect", {})
        rejected = client.post("/api/action", json={"generation": state["generation"],
            "action": "click", "args": center("button"),
            "desktop_width": metadata["desktop_width"], "desktop_height": metadata["desktop_height"]})
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "stale_session"
        report["checks"].append("disconnect_and_old_session_rejection")
    finally:
        if client is not None:
            try:
                client.post("/api/disconnect", json={})
            except httpx.HTTPError:
                pass
            finally:
                client.close()
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        finally:
            try:
                report["fixture_removed"] = stop_fixture(host, fixture)["fixture_removed"]
            finally:
                (output / "live-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                if temporary is not None:
                    temporary.cleanup()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="pi-desktop")
    parser.add_argument("--output", type=Path, default=ROOT / "_local/live-dashboard")
    args = parser.parse_args()
    print(json.dumps(exercise(args.host, args.output), indent=2))
