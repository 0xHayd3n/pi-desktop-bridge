"""MCP tools for viewing and controlling a remote Pi desktop."""

from __future__ import annotations

import base64
import binascii
import json
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from .transport import SSHTransport, TransportError


def _frame_content(result: dict[str, Any]) -> list[TextContent | ImageContent]:
    encoded = result.get("image_base64")
    mime = result.get("mime_type")
    width = result.get("width")
    height = result.get("height")
    if (
        not isinstance(encoded, str) or mime != "image/png"
        or not isinstance(width, int) or isinstance(width, bool) or width <= 0
        or not isinstance(height, int) or isinstance(height, bool) or height <= 0
    ):
        raise TransportError("SSH agent returned an invalid screenshot")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TransportError("SSH agent returned an invalid screenshot") from exc
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise TransportError("SSH agent returned an invalid PNG screenshot")
    return [
        TextContent(type="text", text=f"Desktop screenshot: {width} × {height} pixels. Coordinates refer to these original image pixels."),
        ImageContent(type="image", data=encoded, mimeType="image/png"),
    ]


def create_server(host: str, *, transport: SSHTransport | None = None) -> FastMCP:
    """Create the server; the SSH connection opens on the first tool call."""
    connection = transport or SSHTransport(host)
    action_lock = threading.RLock()
    server = FastMCP(
        "Pi Desktop Bridge",
        instructions=(
            "Control the user's authorized Raspberry Pi desktop. Screenshot coordinates "
            "are pixels in the original image, starting at the top left. After each "
            "input action a fresh screenshot is returned. Keep actions within the "
            "scope the user authorized."
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    input_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

    def action_and_frame(method: str, params: dict[str, Any]) -> list[TextContent | ImageContent]:
        with action_lock:
            try:
                acknowledgement = connection.request(method, params)
            except TransportError as exc:
                raise TransportError(
                    f"{method}: input acknowledgement was not received; the action may have "
                    "executed. Capture a fresh screenshot before deciding whether to repeat it."
                ) from exc
            if acknowledgement.get("ok") is not True:
                raise TransportError(
                    f"{method}: remote agent did not confirm the input result. Capture a fresh "
                    "screenshot before deciding whether to repeat it."
                )
            try:
                return _frame_content(connection.request("screenshot"))
            except TransportError as exc:
                raise TransportError(
                    f"{method}: input was acknowledged by the remote agent, but the fresh "
                    "screenshot is unavailable. Capture a new screenshot before taking further input."
                ) from exc

    @server.tool(description="Show connection and desktop dimensions for the authorized Raspberry Pi.", annotations=read_only)
    def desktop_status() -> str:
        with action_lock:
            return json.dumps(connection.request("status"), ensure_ascii=False)

    @server.tool(description="Capture the current Raspberry Pi desktop. Image coordinates are original pixels from the top-left.", annotations=read_only, structured_output=False)
    def desktop_screenshot() -> list[TextContent | ImageContent]:
        with action_lock:
            return _frame_content(connection.request("screenshot"))

    @server.tool(description="Move the pointer to original screenshot pixel coordinates, then show a fresh screenshot.", annotations=input_tool, structured_output=False)
    def desktop_move(x: int, y: int) -> list[TextContent | ImageContent]:
        return action_and_frame("move", {"x": x, "y": y})

    @server.tool(description="Click at original screenshot pixel coordinates and show a fresh screenshot. Button is left, right, or middle.", annotations=input_tool, structured_output=False)
    def desktop_click(x: int, y: int, button: str = "left", count: int = 1) -> list[TextContent | ImageContent]:
        return action_and_frame("click", {"x": x, "y": y, "button": button, "count": count})

    @server.tool(description="Drag between original screenshot pixel coordinates and show a fresh screenshot.", annotations=input_tool, structured_output=False)
    def desktop_drag(start_x: int, start_y: int, end_x: int, end_y: int) -> list[TextContent | ImageContent]:
        return action_and_frame("drag", {"start_x": start_x, "start_y": start_y, "end_x": end_x, "end_y": end_y})

    @server.tool(description="Scroll the current desktop and show a fresh screenshot. Direction is up, down, left, or right.", annotations=input_tool, structured_output=False)
    def desktop_scroll(direction: str, ticks: int = 1) -> list[TextContent | ImageContent]:
        return action_and_frame("scroll", {"direction": direction, "ticks": ticks})

    @server.tool(name="desktop_type", description="Type text into the focused desktop app and show a fresh screenshot.", annotations=input_tool, structured_output=False)
    def desktop_type(text: str) -> list[TextContent | ImageContent]:
        return action_and_frame("type_text", {"text": text})

    @server.tool(description="Press a key combination in the focused desktop app and show a fresh screenshot. Keys is a list such as ['Control_L','a'].", annotations=input_tool, structured_output=False)
    def desktop_key(keys: list[str]) -> list[TextContent | ImageContent]:
        return action_and_frame("key", {"keys": keys})

    return server
