"""Verify MCP exposes images and action results through the SDK."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import time
import unittest

from mcp.types import ImageContent, TextContent

from pi_desktop_bridge.server import create_server
from pi_desktop_bridge.transport import TransportError


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9P0xQAAAAASUVORK5CYII="
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params or {}))
        if method == "screenshot":
            return {"image_base64": base64.b64encode(PNG).decode(), "mime_type": "image/png", "width": 1, "height": 1}
        if method == "status":
            return {"hostname": "pi", "width": 1, "height": 1}
        return {"ok": True}


class ServerTests(unittest.TestCase):
    def test_sdk_lists_all_tools_and_annotations(self) -> None:
        server = create_server("pi-desktop", transport=FakeTransport())
        listed = asyncio.run(server.list_tools())
        tools = {tool.name: tool for tool in listed}
        self.assertEqual(set(tools), {
            "desktop_status", "desktop_screenshot", "desktop_move", "desktop_click",
            "desktop_drag", "desktop_scroll", "desktop_type", "desktop_key",
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
            def request(self, method: str, params: dict | None = None) -> dict:
                result = super().request(method, params)
                if method in {"click", "move"}:
                    time.sleep(0.04)
                return result

        fake = SlowTransport()
        server = create_server("pi-desktop", transport=fake)

        def call(name: str, arguments: dict) -> None:
            asyncio.run(server.call_tool(name, arguments))

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, "desktop_click", {"x": 1, "y": 1})
            second = pool.submit(call, "desktop_move", {"x": 1, "y": 1})
            first.result()
            second.result()
        methods = [method for method, _ in fake.calls]
        self.assertEqual({methods[0], methods[2]}, {"click", "move"})
        self.assertEqual([methods[1], methods[3]], ["screenshot", "screenshot"])

    def test_input_acknowledged_before_capture_failure_is_visible_in_mcp_error(self) -> None:
        class CaptureFails(FakeTransport):
            def request(self, method: str, params: dict | None = None) -> dict:
                if method == "screenshot":
                    self.calls.append((method, params or {}))
                    raise TransportError("capture failed")
                return super().request(method, params)

        fake = CaptureFails()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "input was acknowledged.*fresh screenshot is unavailable"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertEqual([method for method, _ in fake.calls], ["click", "screenshot"])

    def test_missing_input_acknowledgement_warns_outcome_is_unknown(self) -> None:
        class ActionDropsConnection(FakeTransport):
            def request(self, method: str, params: dict | None = None) -> dict:
                self.calls.append((method, params or {}))
                raise TransportError("SSH agent closed its output")

        fake = ActionDropsConnection()
        server = create_server("pi-desktop", transport=fake)
        with self.assertRaisesRegex(Exception, "action may have executed.*fresh screenshot"):
            asyncio.run(server.call_tool("desktop_click", {"x": 1, "y": 1}))
        self.assertEqual([method for method, _ in fake.calls], ["click"])


if __name__ == "__main__":
    unittest.main()
