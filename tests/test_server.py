"""Verify MCP exposes images and action results through the SDK."""

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib

from mcp import ClientSession, McpError, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ImageContent, TextContent

from pi_desktop_bridge.server import _frame_content, create_server
from pi_desktop_bridge.transport import RemoteAgentError, TransportError


FRAME_ID = "a" * 32


def png(width: int, height: int, *, idat_payload: bytes | None = None, raw_pixels: bytes | None = None) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    rows = raw_pixels if raw_pixels is not None else (b"\x00" + b"\x00\x00\x00" * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows) if idat_payload is None else idat_payload)
            + chunk(b"IEND", b""))


PNG = png(1, 1)


def frame(
    image_width: int = 1, image_height: int = 1, *,
    desktop_width: int = 1, desktop_height: int = 1,
    region: dict | None = None, frame_id: str = FRAME_ID,
    image: bytes | None = None,
) -> dict:
    return {
        "image_base64": base64.b64encode(image if image is not None else png(image_width, image_height)).decode(),
        "mime_type": "image/png", "width": image_width, "height": image_height,
        "desktop_width": desktop_width, "desktop_height": desktop_height,
        "region": region or {"x": 0, "y": 0, "width": desktop_width, "height": desktop_height},
        "frame_id": frame_id,
    }


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.timeout = 60.0

    def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
        self.calls.append((method, params or {}))
        if method == "screenshot":
            return frame(image=PNG)
        if method == "status":
            return {"hostname": "pi", "width": 1, "height": 1}
        if method == "health":
            return {"desktop_ready": True, "session_active": False, "checks": []}
        return {"ok": True}

    def disconnect(self, *, deadline: float | None = None) -> dict:
        self.calls.append(("disconnect", {}))
        return {"ok": True}


class ViewTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.next_frame = 0

    def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
        if method != "screenshot":
            return super().request(method, params, deadline=deadline)
        params = params or {}
        self.calls.append((method, params))
        self.next_frame += 1
        region = {key: params[key] for key in ("x", "y", "width", "height")} if "x" in params else {
            "x": 0, "y": 0, "width": 100, "height": 80,
        }
        width = min(region["width"], params.get("max_width", region["width"]))
        height = max(1, region["height"] * width // region["width"])
        return frame(width, height, desktop_width=100, desktop_height=80,
                     region=region, frame_id=f"{self.next_frame:032x}")


class WaitTransport(ViewTransport):
    def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
        if method != "wait_for_stable":
            return super().request(method, params, deadline=deadline)
        requested = params or {}
        result = super().request("screenshot", requested, deadline=deadline)
        self.calls[-1] = (method, requested)
        result["stability"] = {
            "stable": True, "timed_out": False, "elapsed_ms": requested["stable_ms"],
            "samples": 2, "stable_ms": requested["stable_ms"],
            "timeout_ms": requested["timeout_ms"], "poll_ms": requested["poll_ms"],
        }
        return result


class StdioFixtureTransport(FakeTransport):
    """Exercise the real MCP stdio path without SSH or desktop input."""

    def __init__(self, scenario: str, directory: str) -> None:
        super().__init__()
        self.scenario = scenario
        self.directory = Path(directory)
        self.timeout = 0.3 if scenario == "budget" else 3.0

    def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
        with (self.directory / "calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"method": method, "time": time.monotonic(), "deadline": deadline}) + "\n")
        if self.scenario == "budget" and method in {"health", "status"}:
            time.sleep(0.2)
            if deadline is not None and time.monotonic() >= deadline:
                raise TransportError("shared deadline expired", input_state="not_started")
        blocked_method = "screenshot" if self.scenario == "cancel_capture" else "click"
        if self.scenario != "budget" and method == blocked_method and not (self.directory / "release").exists():
            (self.directory / "started").touch()
            end = time.monotonic() + 3
            while not (self.directory / "release").exists() and time.monotonic() < end:
                time.sleep(0.005)
        if self.scenario == "cancel_capture" and method == "click":
            raise TransportError("input outcome unknown")
        return super().request(method, params, deadline=deadline)


@asynccontextmanager
async def stdio_fixture(scenario: str):
    with tempfile.TemporaryDirectory() as directory:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve()), "--stdio-fixture", scenario, directory],
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                await session.initialize()
                await session.list_tools()
                yield session, Path(directory)


async def wait_for_file(path: Path) -> None:
    async with asyncio.timeout(4):
        while not path.exists():
            await asyncio.sleep(0.005)


async def cancel_request(session: ClientSession, request_id: int) -> None:
    await session.send_notification(types.ClientNotification(types.CancelledNotification(
        method="notifications/cancelled",
        params=types.CancelledNotificationParams(requestId=request_id),
    )))


class ServerTests(unittest.TestCase):
    def test_sdk_lists_all_tools_and_annotations(self) -> None:
        server = create_server("pi-desktop", transport=FakeTransport())
        listed = asyncio.run(server.list_tools())
        tools = {tool.name: tool for tool in listed}
        self.assertEqual(set(tools), {
            "desktop_status", "desktop_screenshot", "desktop_move", "desktop_click",
            "desktop_drag", "desktop_scroll", "desktop_type", "desktop_key",
            "desktop_health", "desktop_disconnect", "desktop_wait_for_stable",
        })
        self.assertTrue(tools["desktop_screenshot"].annotations.readOnlyHint)
        self.assertTrue(tools["desktop_wait_for_stable"].annotations.readOnlyHint)
        self.assertTrue(tools["desktop_click"].annotations.destructiveHint)
        self.assertIn("['Control_L','a']", tools["desktop_key"].description)
        screenshot = tools["desktop_screenshot"].inputSchema["properties"]
        self.assertEqual(set(screenshot), {"x", "y", "width", "height", "max_width"})
        self.assertEqual(set(tools["desktop_wait_for_stable"].inputSchema["properties"]),
                         {"x", "y", "width", "height", "max_width", "stable_ms", "timeout_ms", "poll_ms"})
        for name in ("desktop_move", "desktop_click", "desktop_drag", "desktop_scroll",
                     "desktop_type", "desktop_key"):
            self.assertIn("capture_max_width", tools[name].inputSchema["properties"])
        for name in ("desktop_move", "desktop_click", "desktop_drag", "desktop_scroll"):
            self.assertIn("view_id", tools[name].inputSchema["properties"])
        for name in ("desktop_type", "desktop_key"):
            self.assertNotIn("view_id", tools[name].inputSchema["properties"])

    def test_click_returns_fresh_image_content(self) -> None:
        fake = FakeTransport()
        server = create_server("pi-desktop", transport=fake)
        result = asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertEqual(fake.calls, [
            ("click", {"x": 1, "y": 1, "button": "left", "count": 1}),
            ("screenshot", {}),
        ])
        self.assertTrue(any(isinstance(item, TextContent) and "1 × 1" in item.text for item in result))
        image = next(item for item in result if isinstance(item, ImageContent))
        self.assertEqual(image.mimeType, "image/png")
        self.assertEqual(base64.b64decode(image.data), PNG)

    def test_capture_width_default_override_and_all_six_action_schemas(self) -> None:
        fake = ViewTransport()
        server = create_server("pi-desktop", transport=fake, capture_max_width=20)
        actions = (
            ("desktop_move", {"x": 1, "y": 1}),
            ("desktop_click", {"x": 1, "y": 1}),
            ("desktop_drag", {"start_x": 1, "start_y": 1, "end_x": 2, "end_y": 2}),
            ("desktop_scroll", {"direction": "up"}),
            ("desktop_type", {"text": "hello"}),
            ("desktop_key", {"keys": ["Return"]}),
        )
        for tool, args in actions:
            result = asyncio.run(server.call_tool(tool, args))
            self.assertEqual(json.loads(result[0].text)["width"], 20, tool)
            self.assertEqual(fake.calls[-1], ("screenshot", {"max_width": 20}), tool)
            self.assertNotIn("max_width", fake.calls[-2][1], tool)
            result = asyncio.run(server.call_tool(tool, {**args, "capture_max_width": 10}))
            self.assertEqual(json.loads(result[0].text)["width"], 10, tool)
            self.assertEqual(fake.calls[-1], ("screenshot", {"max_width": 10}), tool)
        result = asyncio.run(server.call_tool("desktop_move", {"x": 1, "y": 1,
                                                               "capture_max_width": 65535}))
        self.assertEqual(json.loads(result[0].text)["width"], 100)
        self.assertEqual(fake.calls[-1], ("screenshot", {"max_width": 65535}))
        explicit = asyncio.run(server.call_tool("desktop_screenshot", {}))
        self.assertEqual(json.loads(explicit[0].text)["width"], 100)
        self.assertEqual(fake.calls[-1], ("screenshot", {}))

    def test_invalid_capture_policy_rejected_before_input(self) -> None:
        for invalid in (0, 65536, True, 1.5, "20"):
            with self.subTest(config=invalid), self.assertRaises(Exception):
                create_server("pi-desktop", transport=FakeTransport(), capture_max_width=invalid)
        fake = ViewTransport()
        server = create_server("pi-desktop", transport=fake, capture_max_width=20)
        actions = (
            ("desktop_move", {"x": 1, "y": 1}),
            ("desktop_click", {"x": 1, "y": 1}),
            ("desktop_drag", {"start_x": 1, "start_y": 1, "end_x": 2, "end_y": 2}),
            ("desktop_scroll", {"direction": "up"}),
            ("desktop_type", {"text": "hello"}),
            ("desktop_key", {"keys": ["Return"]}),
        )
        for tool, args in actions:
            for invalid in (0, 65536, True, "20", 20.0):
                with self.subTest(tool=tool, width=invalid), self.assertRaises(Exception):
                    asyncio.run(server.call_tool(tool, {**args, "capture_max_width": invalid}))
        self.assertEqual(fake.calls, [])

    def test_post_action_capture_must_match_requested_width_and_full_source(self) -> None:
        class WrongCapture(ViewTransport):
            def __init__(self, wrong_region: bool = False) -> None:
                super().__init__()
                self.wrong_region = wrong_region

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method != "screenshot":
                    return super().request(method, params, deadline=deadline)
                # Valid PNG and internally consistent metadata, but wrong output scope.
                returned = ({"x": 0, "y": 0, "width": 50, "height": 80, "max_width": 20}
                            if self.wrong_region else {"max_width": 21})
                return super().request(method, returned, deadline=deadline)

        for wrong_region in (False, True):
            with self.subTest(wrong_region=wrong_region):
                fake = WrongCapture(wrong_region)
                server = create_server("pi-desktop", transport=fake, capture_max_width=20)
                with self.assertRaisesRegex(Exception, "fresh screenshot is unavailable"):
                    asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
                with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
                    asyncio.run(server.call_tool("desktop_move", {"x": 1, "y": 1}))

    def test_concurrent_actions_keep_each_result_with_its_fresh_frame(self) -> None:
        class SlowTransport(FakeTransport):
            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                result = super().request(method, params, deadline=deadline)
                if method in {"click", "move"}:
                    time.sleep(0.04)
                return result

        fake = SlowTransport()
        server = create_server("pi-desktop", transport=fake)

        async def concurrent_calls() -> None:
            await asyncio.gather(
                server.call_tool("desktop_click", {"x": 1, "y": 1}),
                server.call_tool("desktop_move", {"x": 1, "y": 1}),
            )

        asyncio.run(concurrent_calls())
        methods = [method for method, _ in fake.calls]
        self.assertEqual({methods[0], methods[2]}, {"click", "move"})
        self.assertEqual([methods[1], methods[3]], ["screenshot", "screenshot"])

    def test_input_acknowledged_before_capture_failure_is_visible_in_mcp_error(self) -> None:
        class CaptureFails(FakeTransport):
            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "screenshot":
                    self.calls.append((method, params or {}))
                    raise TransportError("capture failed")
                return super().request(method, params, deadline=deadline)

        fake = CaptureFails()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "input was acknowledged.*fresh screenshot is unavailable"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertEqual([method for method, _ in fake.calls], ["click", "screenshot"])

    def test_missing_input_acknowledgement_warns_outcome_is_unknown(self) -> None:
        class ActionDropsConnection(FakeTransport):
            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                self.calls.append((method, params or {}))
                raise TransportError("SSH agent closed its output")

        fake = ActionDropsConnection()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "Input may have executed.*desktop_screenshot"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertEqual([method for method, _ in fake.calls], ["click"])

    def test_remote_no_input_rejection_does_not_block_next_action(self) -> None:
        class RejectOnce(FakeTransport):
            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click" and not any(item[0] == "click" for item in self.calls):
                    self.calls.append((method, params or {}))
                    raise RemoteAgentError("invalid_params", "Outside visible desktop", "not_started")
                return super().request(method, params, deadline=deadline)

        fake = RejectOnce()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "Outside visible desktop.*No input was sent"):
            asyncio.run(server.call_tool("desktop_click", {"x": 9999, "y": 1}))
        result = asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertTrue(any(isinstance(item, ImageContent) for item in result))

    def test_ambiguous_input_requires_observation_even_after_status_health_and_release(self) -> None:
        class AmbiguousOnce(FakeTransport):
            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click" and not any(item[0] == "click" for item in self.calls):
                    self.calls.append((method, params or {}))
                    raise RemoteAgentError("input_failed", "Pointer delivery interrupted", "may_have_executed")
                return super().request(method, params, deadline=deadline)

        fake = AmbiguousOnce()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "Pointer delivery interrupted.*Input may have executed"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        asyncio.run(server.call_tool("desktop_health", {}))
        asyncio.run(server.call_tool("desktop_status", {}))
        asyncio.run(server.call_tool("desktop_disconnect", {}))
        with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertEqual([name for name, _ in fake.calls].count("click"), 1)
        asyncio.run(server.call_tool("desktop_screenshot", {}))
        result = asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertTrue(any(isinstance(item, ImageContent) for item in result))

    def test_wait_schema_defaults_crop_timeout_and_view_mapping(self) -> None:
        fake = WaitTransport()
        server = create_server("pi-desktop", transport=fake, capture_max_width=20)
        first = asyncio.run(server.call_tool("desktop_wait_for_stable", {}))
        self.assertEqual(fake.calls[-1], ("wait_for_stable", {
            "stable_ms": 300, "timeout_ms": 5000, "poll_ms": 100,
        }))
        first_meta = json.loads(first[0].text)
        self.assertEqual(first_meta["width"], 100)
        self.assertEqual(first_meta["stability"], {
            "stable": True, "timed_out": False, "elapsed_ms": 300, "samples": 2,
            "stable_ms": 300, "timeout_ms": 5000, "poll_ms": 100,
        })
        crop = asyncio.run(server.call_tool("desktop_wait_for_stable", {
            "x": 10, "y": 20, "width": 8, "height": 4, "max_width": 4,
            "stable_ms": 200, "timeout_ms": 600, "poll_ms": 50,
        }))
        crop_meta = json.loads(crop[0].text)
        self.assertEqual((crop_meta["width"], crop_meta["height"]), (4, 2))
        self.assertEqual(crop_meta["region"], {"x": 10, "y": 20, "width": 8, "height": 4})
        self.assertEqual(fake.calls[-1][1], {
            "x": 10, "y": 20, "width": 8, "height": 4, "max_width": 4,
            "stable_ms": 200, "timeout_ms": 600, "poll_ms": 50,
        })
        before = len(fake.calls)
        with self.assertRaisesRegex(Exception, "stale_view"):
            asyncio.run(server.call_tool("desktop_click", {
                "x": 0, "y": 0, "view_id": first_meta["view_id"],
            }))
        self.assertEqual(len(fake.calls), before)
        asyncio.run(server.call_tool("desktop_click", {
            "x": 0, "y": 0, "view_id": crop_meta["view_id"],
        }))
        self.assertEqual(fake.calls[-2][1]["x"], 11)
        self.assertEqual(fake.calls[-2][1]["y"], 21)
        self.assertEqual(fake.calls[-2][1]["frame_id"], crop_meta["view_id"])
        self.assertEqual(fake.calls[-1], ("screenshot", {"max_width": 20}))

    def test_wait_invalid_parameters_rejected_before_transport(self) -> None:
        fake = WaitTransport()
        server = create_server("pi-desktop", transport=fake)
        cases = (
            {"x": 1}, {"max_width": 0}, {"stable_ms": 49},
            {"stable_ms": 2001}, {"timeout_ms": 99}, {"timeout_ms": 10001},
            {"poll_ms": 49}, {"poll_ms": 1001},
            {"stable_ms": 100, "poll_ms": 101},
            {"stable_ms": 101, "timeout_ms": 100},
            {"stable_ms": True}, {"poll_ms": "100"}, {"timeout_ms": 100.0},
        )
        for args in cases:
            with self.subTest(args=args), self.assertRaises(Exception):
                asyncio.run(server.call_tool("desktop_wait_for_stable", args))
        self.assertEqual(fake.calls, [])

    def test_wait_rejects_invalid_metadata_and_image_and_invalidates_old_view(self) -> None:
        class DamagedWait(WaitTransport):
            def __init__(self) -> None:
                super().__init__()
                self.change = lambda result: None

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                result = super().request(method, params, deadline=deadline)
                if method == "wait_for_stable":
                    self.change(result)
                return result

        fake = DamagedWait()
        server = create_server("pi-desktop", transport=fake)
        for change in (
            lambda result: result["stability"].update(stable=True, timed_out=True),
            lambda result: result["stability"].update(elapsed_ms=299),
            lambda result: result["stability"].update(samples=1),
            lambda result: result["stability"].update(samples=202),
            lambda result: result["stability"].update(elapsed_ms=11001),
            lambda result: result["stability"].update(elapsed_ms=5001),
            lambda result: result["stability"].update(stable=False, timed_out=True, elapsed_ms=4999),
            lambda result: result["stability"].update(stable_ms=301),
            lambda result: result["stability"].update(samples=True),
            lambda result: result["stability"].update(extra=1),
            lambda result: result.update(width=99),
        ):
            fake.change = lambda result: None
            current = json.loads(asyncio.run(server.call_tool("desktop_screenshot", {}))[0].text)["view_id"]
            fake.change = change
            with self.subTest(change=change), self.assertRaises(Exception):
                asyncio.run(server.call_tool("desktop_wait_for_stable", {}))
            before = len(fake.calls)
            with self.assertRaisesRegex(Exception, "stale_view"):
                asyncio.run(server.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": current}))
            self.assertEqual(len(fake.calls), before)

    def test_wait_timeout_preserves_guard_and_status_recovery_hints(self) -> None:
        class UncertainThenTimeout(WaitTransport):
            def __init__(self) -> None:
                super().__init__()
                self.timeout_result = True

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click":
                    self.calls.append((method, params or {}))
                    raise RemoteAgentError("input_failed", "outcome unknown", "may_have_executed")
                result = super().request(method, params, deadline=deadline)
                if method == "wait_for_stable" and self.timeout_result:
                    result["stability"].update(stable=False, timed_out=True, elapsed_ms=5000,
                                               samples=3)
                return result

        fake = UncertainThenTimeout()
        server = create_server("pi-desktop", transport=fake)
        def status_data(tool: str) -> dict:
            return json.loads(asyncio.run(server.call_tool(tool, {}))[0][0].text)

        for tool in ("desktop_status", "desktop_health"):
            initial = status_data(tool)
            self.assertIs(initial["observation_required"], False)
            self.assertIsNone(initial["recovery_action"])
        with self.assertRaisesRegex(Exception, "Input may have executed"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        for tool in ("desktop_status", "desktop_health"):
            state = status_data(tool)
            self.assertIs(state["observation_required"], True)
            self.assertEqual(state["recovery_action"], "desktop_screenshot")
            self.assertIn("hostname" if tool == "desktop_status" else "desktop_ready", state)
        waited = asyncio.run(server.call_tool("desktop_wait_for_stable", {}))
        metadata = json.loads(waited[0].text)
        self.assertEqual(metadata["stability"]["stable"], False)
        self.assertEqual(metadata["stability"]["timed_out"], True)
        self.assertTrue(any(isinstance(item, ImageContent) for item in waited))
        fake.timeout_result = False
        stable_wait = asyncio.run(server.call_tool("desktop_wait_for_stable", {}))
        self.assertIs(json.loads(stable_wait[0].text)["stability"]["stable"], True)
        asyncio.run(server.call_tool("desktop_disconnect", {}))
        with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
            asyncio.run(server.call_tool("desktop_move", {"x": 1, "y": 1,
                                                           "view_id": metadata["view_id"]}))
        self.assertEqual(status_data("desktop_health")["recovery_action"],
                         "desktop_screenshot")
        asyncio.run(server.call_tool("desktop_screenshot", {}))
        self.assertIsNone(status_data("desktop_status")["recovery_action"])

    def test_cancelled_wait_keeps_serialization_and_invalidates_old_view(self) -> None:
        class BlockingWait(WaitTransport):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "wait_for_stable":
                    self.started.set()
                    self.release.wait(timeout=2)
                return super().request(method, params, deadline=deadline)

        fake = BlockingWait()
        server = create_server("pi-desktop", transport=fake)

        async def cancelled_wait() -> None:
            view = json.loads((await server.call_tool("desktop_screenshot", {}))[0].text)["view_id"]
            active = asyncio.create_task(server.call_tool("desktop_wait_for_stable", {}))
            async with asyncio.timeout(1):
                while not fake.started.is_set():
                    await asyncio.sleep(0.005)
            active.cancel()
            health = asyncio.create_task(server.call_tool("desktop_health", {}))
            try:
                await asyncio.sleep(0.03)
                self.assertFalse(active.done())
                self.assertFalse(health.done())
            finally:
                fake.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await active
            await health
            with self.assertRaisesRegex(Exception, "stale_view"):
                await server.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": view})

        asyncio.run(cancelled_wait())
        self.assertEqual([method for method, _ in fake.calls],
                         ["screenshot", "wait_for_stable", "health"])

    def test_failed_wait_preserves_uncertain_input_recovery(self) -> None:
        class FailedWait(WaitTransport):
            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click":
                    raise RemoteAgentError("input_failed", "outcome unknown", "may_have_executed")
                if method == "wait_for_stable":
                    raise TransportError("sampling failed", input_state="not_started")
                return super().request(method, params, deadline=deadline)

        fake = FailedWait()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "Input may have executed"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        with self.assertRaisesRegex(Exception, "sampling failed"):
            asyncio.run(server.call_tool("desktop_wait_for_stable", {}))
        with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
            asyncio.run(server.call_tool("desktop_move", {"x": 1, "y": 1}))

    def test_targeted_scroll_forwards_original_pixel_coordinates(self) -> None:
        fake = FakeTransport()
        server = create_server("pi-desktop", transport=fake)
        asyncio.run(server.call_tool("desktop_scroll", {"direction": "down", "ticks": 2, "x": 13, "y": 24}))
        self.assertEqual(fake.calls[0], ("scroll", {"direction": "down", "ticks": 2, "x": 13, "y": 24}))

    def test_png_ihdr_must_match_declared_dimensions(self) -> None:
        bad = frame(2, 1, desktop_width=2, image=PNG)
        with self.assertRaisesRegex(TransportError, "invalid PNG"):
            _frame_content(bad)

    def test_screenshot_metadata_bounds_crc_and_iend(self) -> None:
        good = frame(3, 2, desktop_width=100, desktop_height=80,
                     region={"x": 10, "y": 20, "width": 7, "height": 5})
        metadata = json.loads(_frame_content(good)[0].text)
        self.assertEqual(metadata["view_id"], FRAME_ID)
        self.assertEqual(metadata["region"], good["region"])
        self.assertIn("image", metadata["coordinate_instruction"])
        bad_cases = [
            {**good, "desktop_width": 16},
            {**good, "desktop_width": 5000, "desktop_height": 5000},
            {**good, "frame_id": "A" * 32},
            {**good, "region": {**good["region"], "width": 2}},
            {**good, "height": 3},
            {**good, "desktop_width": True},
        ]
        image = base64.b64decode(good["image_base64"])
        for damaged in (image[:29] + bytes([image[29] ^ 1]) + image[30:],
                        image[:-1] + bytes([image[-1] ^ 1]), image[:-12], image + b"x",
                        png(3, 2, idat_payload=b"broken"),
                        png(3, 2, raw_pixels=(b"\x05" + b"\x00" * 9) * 2),
                        png(3, 2, raw_pixels=b"\x00" * 19),
                        png(3, 2, raw_pixels=b"\x00" * 21),
                        png(3, 2, idat_payload=zlib.compress(b"\x00" * 20) + b"trailing")):
            bad_cases.append({**good, "image_base64": base64.b64encode(damaged).decode()})
        for bad in bad_cases:
            with self.subTest(bad=bad), self.assertRaises(TransportError):
                _frame_content(bad)

    def test_region_shape_rejected_locally_and_full_resized_overview(self) -> None:
        fake = ViewTransport()
        server = create_server("pi-desktop", transport=fake)
        for args in ({"x": 1}, {"x": -1, "y": 0, "width": 2, "height": 2},
                     {"max_width": 0}, {"x": 0, "y": 0, "width": 5000, "height": 5000}):
            with self.subTest(args=args), self.assertRaises(Exception):
                asyncio.run(server.call_tool("desktop_screenshot", args))
        self.assertEqual(fake.calls, [])
        result = asyncio.run(server.call_tool("desktop_screenshot", {"max_width": 20}))
        metadata = json.loads(result[0].text)
        self.assertEqual((metadata["width"], metadata["height"]), (20, 16))
        self.assertEqual(metadata["region"], {"x": 0, "y": 0, "width": 100, "height": 80})
        self.assertEqual(fake.calls[0], ("screenshot", {"max_width": 20}))

    def test_sdk_rejects_coercible_non_integer_arguments_before_transport(self) -> None:
        fake = ViewTransport()
        server = create_server("pi-desktop", transport=fake)
        for tool, args in (
            ("desktop_screenshot", {"max_width": True}),
            ("desktop_screenshot", {"max_width": "20"}),
            ("desktop_screenshot", {"max_width": 20.0}),
            ("desktop_screenshot", {"x": True, "y": 0, "width": 2, "height": 2}),
            ("desktop_move", {"x": True, "y": 0}),
            ("desktop_click", {"x": 0, "y": 0, "count": "2"}),
            ("desktop_drag", {"start_x": 0, "start_y": 0, "end_x": 1.0, "end_y": 1}),
            ("desktop_scroll", {"direction": "down", "ticks": True}),
        ):
            with self.subTest(tool=tool, args=args), self.assertRaises(Exception):
                asyncio.run(server.call_tool(tool, args))
        self.assertEqual(fake.calls, [])

    def test_image_center_mapping_for_all_mouse_tools(self) -> None:
        fake = ViewTransport()
        server = create_server("pi-desktop", transport=fake)

        def current_view() -> str:
            result = asyncio.run(server.call_tool("desktop_screenshot", {
                "x": 10, "y": 20, "width": 7, "height": 5, "max_width": 3,
            }))
            self.assertEqual((json.loads(result[0].text)["width"], json.loads(result[0].text)["height"]), (3, 2))
            return json.loads(result[0].text)["view_id"]

        view_id = current_view()
        asyncio.run(server.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": view_id}))
        self.assertEqual(fake.calls[-2], ("move", {"x": 11, "y": 21, "frame_id": view_id}))
        view_id = current_view()
        asyncio.run(server.call_tool("desktop_click", {"x": 2, "y": 1, "view_id": view_id}))
        self.assertEqual(fake.calls[-2], ("click", {"x": 15, "y": 23, "button": "left", "count": 1,
                                                  "frame_id": view_id}))
        view_id = current_view()
        asyncio.run(server.call_tool("desktop_drag", {"start_x": 0, "start_y": 1, "end_x": 2, "end_y": 0,
                                                      "view_id": view_id}))
        self.assertEqual(fake.calls[-2], ("drag", {"start_x": 11, "start_y": 23, "end_x": 15, "end_y": 21,
                                                 "frame_id": view_id}))
        view_id = current_view()
        asyncio.run(server.call_tool("desktop_scroll", {"direction": "down", "x": 2, "y": 0,
                                                        "view_id": view_id}))
        self.assertEqual(fake.calls[-2], ("scroll", {"direction": "down", "ticks": 1, "x": 15, "y": 21,
                                                   "frame_id": view_id}))

    def test_stale_or_outside_view_never_reaches_transport(self) -> None:
        fake = ViewTransport()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "stale_view"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1, "view_id": "a" * 32}))
        self.assertEqual(fake.calls, [])
        view = json.loads(asyncio.run(server.call_tool("desktop_screenshot", {"max_width": 10}))[0].text)["view_id"]
        before = len(fake.calls)
        for args in ({"x": 10, "y": 0, "view_id": view},
                     {"x": 0, "y": 0, "view_id": "b" * 32}):
            with self.assertRaises(Exception):
                asyncio.run(server.call_tool("desktop_click", args))
        with self.assertRaisesRegex(Exception, "requires both x and y"):
            asyncio.run(server.call_tool("desktop_scroll", {"direction": "up", "view_id": view}))
        self.assertEqual(len(fake.calls), before)
        newer = json.loads(asyncio.run(server.call_tool("desktop_screenshot", {"max_width": 10}))[0].text)["view_id"]
        self.assertNotEqual(view, newer)
        before = len(fake.calls)
        with self.assertRaisesRegex(Exception, "stale_view"):
            asyncio.run(server.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": view}))
        self.assertEqual(len(fake.calls), before)
        asyncio.run(server.call_tool("desktop_disconnect", {}))
        with self.assertRaisesRegex(Exception, "stale_view"):
            asyncio.run(server.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": view}))
        self.assertEqual(fake.calls[-1][0], "disconnect")

    def test_known_remote_preflight_rejection_preserves_view(self) -> None:
        class RejectOnce(ViewTransport):
            def __init__(self) -> None:
                super().__init__()
                self.rejected = False

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click" and not self.rejected:
                    self.rejected = True
                    self.calls.append((method, params or {}))
                    raise RemoteAgentError("invalid_params", "button rejected", "not_started")
                return super().request(method, params, deadline=deadline)

        fake = RejectOnce()
        server = create_server("pi-desktop", transport=fake)
        view = json.loads(asyncio.run(server.call_tool("desktop_screenshot", {"max_width": 10}))[0].text)["view_id"]
        with self.assertRaisesRegex(Exception, "No input was sent"):
            asyncio.run(server.call_tool("desktop_click", {"x": 0, "y": 0, "view_id": view}))
        asyncio.run(server.call_tool("desktop_click", {"x": 0, "y": 0, "view_id": view}))
        clicks = [params for method, params in fake.calls if method == "click"]
        self.assertEqual(len(clicks), 2)
        self.assertTrue(all(params["frame_id"] == view for params in clicks))

    def test_invalid_full_metadata_neither_enables_view_nor_clears_guard(self) -> None:
        class BadFrame(ViewTransport):
            def __init__(self) -> None:
                super().__init__()
                self.bad = False

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click":
                    self.calls.append((method, params or {}))
                    raise RemoteAgentError("input_failed", "outcome unknown", "may_have_executed")
                result = super().request(method, params, deadline=deadline)
                if method == "screenshot" and self.bad:
                    result["image_base64"] = base64.b64encode(
                        png(result["width"], result["height"], idat_payload=b"broken")
                    ).decode()
                return result

        fake = BadFrame()
        server = create_server("pi-desktop", transport=fake)
        fake.bad = True
        with self.assertRaisesRegex(Exception, "invalid PNG"):
            asyncio.run(server.call_tool("desktop_screenshot", {}))
        before = len(fake.calls)
        with self.assertRaisesRegex(Exception, "stale_view"):
            asyncio.run(server.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": "a" * 32}))
        self.assertEqual(len(fake.calls), before)
        fake.bad = False
        with self.assertRaisesRegex(Exception, "Input may have executed"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        fake.bad = True
        with self.assertRaisesRegex(Exception, "invalid PNG"):
            asyncio.run(server.call_tool("desktop_screenshot", {}))
        with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
            asyncio.run(server.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": "a" * 32}))
        fake.bad = False
        view = json.loads(asyncio.run(server.call_tool("desktop_screenshot", {}))[0].text)["view_id"]
        self.assertEqual(len(view), 32)

    def test_partial_capture_cannot_clear_uncertain_input_guard(self) -> None:
        class Ambiguous(ViewTransport):
            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click":
                    self.calls.append((method, params or {}))
                    raise RemoteAgentError("input_failed", "outcome unknown", "may_have_executed")
                return super().request(method, params, deadline=deadline)

        fake = Ambiguous()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "Input may have executed"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        asyncio.run(server.call_tool("desktop_screenshot", {"x": 0, "y": 0, "width": 100, "height": 80}))
        with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
            asyncio.run(server.call_tool("desktop_move", {"x": 1, "y": 1}))
        asyncio.run(server.call_tool("desktop_screenshot", {"max_width": 20}))
        asyncio.run(server.call_tool("desktop_move", {"x": 1, "y": 1}))
        self.assertEqual([name for name, _ in fake.calls].count("move"), 1)

    def test_action_and_capture_share_mcp_deadline(self) -> None:
        class SlowCapture(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.timeout = 0.12
                self.deadlines: list[float | None] = []

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                self.deadlines.append(deadline)
                time.sleep(0.07)
                if deadline is not None and time.monotonic() >= deadline:
                    raise TransportError("shared deadline expired")
                return super().request(method, params, deadline=deadline)

        fake = SlowCapture()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "input was acknowledged.*fresh screenshot is unavailable"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertEqual(len(fake.deadlines), 2)
        self.assertEqual(fake.deadlines[0], fake.deadlines[1])

    def test_capture_failure_blocks_input_until_successful_screenshot(self) -> None:
        class CaptureFailsOnce(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.failed = False

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "screenshot" and not self.failed:
                    self.failed = True
                    self.calls.append((method, params or {}))
                    raise RemoteAgentError("capture_failed", "Screen capture failed", "not_started")
                return super().request(method, params, deadline=deadline)

        fake = CaptureFailsOnce()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "input was acknowledged.*fresh screenshot is unavailable"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
            asyncio.run(server.call_tool("desktop_move", {"x": 2, "y": 2}))
        self.assertEqual([name for name, _ in fake.calls].count("move"), 0)
        asyncio.run(server.call_tool("desktop_screenshot", {}))
        asyncio.run(server.call_tool("desktop_move", {"x": 2, "y": 2}))
        self.assertEqual([name for name, _ in fake.calls].count("move"), 1)

    def test_queued_mcp_tool_times_out_without_interrupting_active_action(self) -> None:
        class SlowAction(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.timeout = 0.08
                self.started = threading.Event()

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click":
                    self.started.set()
                    time.sleep(0.16)
                return super().request(method, params, deadline=deadline)

        fake = SlowAction()
        server = create_server("pi-desktop", transport=fake)
        async def concurrent_calls() -> None:
            active = asyncio.create_task(server.call_tool("desktop_click", {"x": 1, "y": 1}))
            async with asyncio.timeout(1):
                while not fake.started.is_set():
                    await asyncio.sleep(0.005)
            with self.assertRaisesRegex(Exception, "Desktop is busy; tool timed out before starting"):
                await server.call_tool("desktop_move", {"x": 2, "y": 2})
            self.assertTrue(any(isinstance(item, ImageContent) for item in await active))

        asyncio.run(concurrent_calls())
        self.assertEqual([name for name, _ in fake.calls].count("move"), 0)

    def test_repeated_native_cancellation_waits_for_failed_worker_cleanup(self) -> None:
        class CancelledAction(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()
                self.cleaned = threading.Event()

            def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
                if method == "click":
                    self.started.set()
                    try:
                        self.release.wait(timeout=2)
                        raise TransportError("response unavailable")
                    finally:
                        self.cleaned.set()
                return super().request(method, params, deadline=deadline)

        fake = CancelledAction()
        server = create_server("pi-desktop", transport=fake)

        async def cancel_active() -> None:
            loop_errors: list[dict] = []
            asyncio.get_running_loop().set_exception_handler(lambda loop, context: loop_errors.append(context))
            active = asyncio.create_task(server.call_tool("desktop_click", {"x": 1, "y": 1}))
            async with asyncio.timeout(1):
                while not fake.started.is_set():
                    await asyncio.sleep(0.005)
            active.cancel()
            await asyncio.sleep(0)
            active.cancel()
            health = asyncio.create_task(server.call_tool("desktop_health", {}))
            try:
                await asyncio.sleep(0.03)
                self.assertFalse(active.done())
                self.assertFalse(health.done())
                self.assertEqual(fake.calls, [])
            finally:
                fake.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await active
            self.assertTrue(fake.cleaned.is_set())
            await health
            with self.assertRaisesRegex(Exception, "previous input outcome is uncertain"):
                await server.call_tool("desktop_move", {"x": 2, "y": 2})
            await server.call_tool("desktop_screenshot", {})
            await server.call_tool("desktop_move", {"x": 2, "y": 2})
            self.assertEqual(loop_errors, [], "Cancelled worker left an unconsumed future exception")

        asyncio.run(cancel_active())


class StdioConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_stdio_tools_include_queue_time_in_deadline(self) -> None:
        async with stdio_fixture("budget") as (session, directory):
            first, second = await asyncio.gather(
                session.call_tool("desktop_health", {}),
                session.call_tool("desktop_status", {}),
            )
            self.assertFalse(first.isError)
            self.assertTrue(second.isError)
            self.assertIn("shared deadline expired", second.content[0].text)
            calls = [json.loads(line) for line in (directory / "calls.jsonl").read_text().splitlines()]
            self.assertLess(abs(calls[1]["deadline"] - calls[0]["deadline"]), 0.1)

    async def test_cancelled_queued_input_never_reaches_transport(self) -> None:
        async with stdio_fixture("cancel_queue") as (session, directory):
            active = asyncio.create_task(session.call_tool("desktop_click", {"x": 1, "y": 1}))
            await wait_for_file(directory / "started")
            # The SDK exposes no public request-id hook for cancellation tests.
            queued_id = session._request_id
            queued = asyncio.create_task(session.call_tool("desktop_move", {"x": 2, "y": 2}))
            await asyncio.sleep(0.03)
            await cancel_request(session, queued_id)
            try:
                with self.assertRaisesRegex(McpError, "Request cancelled"):
                    await asyncio.wait_for(asyncio.shield(queued), timeout=0.5)
            finally:
                (directory / "release").touch()
                await active
                await asyncio.gather(queued, return_exceptions=True)
            self.assertFalse((await session.call_tool("desktop_health", {})).isError)
            methods = [json.loads(line)["method"] for line in (directory / "calls.jsonl").read_text().splitlines()]
            self.assertEqual(methods, ["click", "screenshot", "health"])

    async def test_cancelled_active_input_holds_lock_and_requires_new_observation(self) -> None:
        async with stdio_fixture("cancel_active") as (session, directory):
            active_id = session._request_id
            active = asyncio.create_task(session.call_tool("desktop_click", {"x": 1, "y": 1}))
            await wait_for_file(directory / "started")
            await cancel_request(session, active_id)
            try:
                with self.assertRaisesRegex(McpError, "Request cancelled"):
                    await asyncio.wait_for(asyncio.shield(active), timeout=0.5)
                next_action = asyncio.create_task(session.call_tool("desktop_move", {"x": 2, "y": 2}))
                await asyncio.sleep(0.05)
                self.assertFalse(next_action.done(), "Cancellation released the active worker's lock")
            finally:
                (directory / "release").touch()
                await asyncio.gather(active, return_exceptions=True)
            rejected = await next_action
            self.assertTrue(rejected.isError)
            self.assertIn("previous input outcome is uncertain", rejected.content[0].text)
            self.assertFalse((await session.call_tool("desktop_screenshot", {})).isError)
            self.assertFalse((await session.call_tool("desktop_move", {"x": 2, "y": 2})).isError)
            methods = [json.loads(line)["method"] for line in (directory / "calls.jsonl").read_text().splitlines()]
            self.assertEqual(methods, ["click", "screenshot", "screenshot", "move", "screenshot"])

    async def test_cancelled_capture_does_not_clear_existing_observation_guard(self) -> None:
        async with stdio_fixture("cancel_capture") as (session, directory):
            self.assertTrue((await session.call_tool("desktop_click", {"x": 1, "y": 1})).isError)
            capture_id = session._request_id
            capture = asyncio.create_task(session.call_tool("desktop_screenshot", {}))
            await wait_for_file(directory / "started")
            await cancel_request(session, capture_id)
            try:
                with self.assertRaisesRegex(McpError, "Request cancelled"):
                    await asyncio.wait_for(asyncio.shield(capture), timeout=0.5)
            finally:
                (directory / "release").touch()
                await asyncio.gather(capture, return_exceptions=True)
            rejected = await session.call_tool("desktop_move", {"x": 2, "y": 2})
            self.assertTrue(rejected.isError)
            self.assertIn("previous input outcome is uncertain", rejected.content[0].text)
            self.assertFalse((await session.call_tool("desktop_screenshot", {})).isError)
            self.assertFalse((await session.call_tool("desktop_move", {"x": 2, "y": 2})).isError)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--stdio-fixture":
        create_server("pi-desktop", transport=StdioFixtureTransport(sys.argv[2], sys.argv[3])).run()
    else:
        unittest.main()
