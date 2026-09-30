"""Verify MCP exposes images and action results through the SDK."""

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from mcp import ClientSession, McpError, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ImageContent, TextContent

from pi_desktop_bridge.server import _frame_content, create_server
from pi_desktop_bridge.transport import RemoteAgentError, TransportError


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9P0xQAAAAASUVORK5CYII="
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.timeout = 60.0

    def request(self, method: str, params: dict | None = None, *, deadline: float | None = None) -> dict:
        self.calls.append((method, params or {}))
        if method == "screenshot":
            return {"image_base64": base64.b64encode(PNG).decode(), "mime_type": "image/png", "width": 1, "height": 1}
        if method == "status":
            return {"hostname": "pi", "width": 1, "height": 1}
        if method == "health":
            return {"desktop_ready": True, "session_active": False, "checks": []}
        return {"ok": True}

    def disconnect(self, *, deadline: float | None = None) -> dict:
        self.calls.append(("disconnect", {}))
        return {"ok": True}


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
            "desktop_health", "desktop_disconnect",
        })
        self.assertTrue(tools["desktop_screenshot"].annotations.readOnlyHint)
        self.assertTrue(tools["desktop_click"].annotations.destructiveHint)
        self.assertIn("['Control_L','a']", tools["desktop_key"].description)

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

    def test_targeted_scroll_forwards_original_pixel_coordinates(self) -> None:
        fake = FakeTransport()
        server = create_server("pi-desktop", transport=fake)
        asyncio.run(server.call_tool("desktop_scroll", {"direction": "down", "ticks": 2, "x": 13, "y": 24}))
        self.assertEqual(fake.calls[0], ("scroll", {"direction": "down", "ticks": 2, "x": 13, "y": 24}))

    def test_png_ihdr_must_match_declared_dimensions(self) -> None:
        frame = {"image_base64": base64.b64encode(PNG).decode(), "mime_type": "image/png", "width": 2, "height": 1}
        with self.assertRaisesRegex(TransportError, "invalid PNG"):
            _frame_content(frame)

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
