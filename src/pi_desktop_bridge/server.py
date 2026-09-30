"""MCP tools for viewing and controlling a remote Pi desktop."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
import json
import struct
import time
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from .transport import MAX_RESPONSE_BYTES, RemoteAgentError, SSHTransport, TransportError


MAX_PIXELS = 16_777_216
MAX_PNG_BYTES = MAX_PIXELS * 4 + 1_048_576


def _frame_content(result: dict[str, Any]) -> list[TextContent | ImageContent]:
    encoded = result.get("image_base64")
    mime = result.get("mime_type")
    width = result.get("width")
    height = result.get("height")
    if (
        not isinstance(encoded, str) or mime != "image/png"
        or not isinstance(width, int) or isinstance(width, bool) or width <= 0
        or not isinstance(height, int) or isinstance(height, bool) or height <= 0
        or width * height > MAX_PIXELS
        or len(encoded) > min(MAX_RESPONSE_BYTES, 4 * ((MAX_PNG_BYTES + 2) // 3))
    ):
        raise TransportError("SSH agent returned an invalid screenshot")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TransportError("SSH agent returned an invalid screenshot") from exc
    if (
        len(data) < 33 or len(data) > MAX_PNG_BYTES
        or not data.startswith(b"\x89PNG\r\n\x1a\n")
        or data[8:16] != b"\x00\x00\x00\x0dIHDR"
        or struct.unpack(">II", data[16:24]) != (width, height)
    ):
        raise TransportError("SSH agent returned an invalid PNG screenshot")
    return [
        TextContent(type="text", text=f"Desktop screenshot: {width} × {height} pixels. Coordinates refer to these original image pixels."),
        ImageContent(type="image", data=encoded, mimeType="image/png"),
    ]


def create_server(host: str, *, transport: SSHTransport | None = None) -> FastMCP:
    """Create the server; the SSH connection opens on the first tool call."""
    connection = transport or SSHTransport(host)
    action_lock = asyncio.Lock()
    observation_required = False
    server = FastMCP(
        "Pi Desktop Bridge",
        instructions=(
            "Control the user's authorized Raspberry Pi desktop. Screenshot coordinates "
            "are pixels in the original image, starting at the top left. After each "
            "input action a fresh screenshot is returned. Keep actions within the "
            "scope the user authorized. Call desktop_disconnect when finished so "
            "another client can acquire the desktop."
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    input_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
    release_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    async def serialized(
        operation: Callable[[float], Any], *, input_action: bool = False,
        observation: bool = False,
    ) -> Any:
        nonlocal observation_required
        # Stamp the budget before queueing or offloading. FastMCP invokes sync
        # tools inline, so every tool must enter here from an async function.
        deadline = time.monotonic() + getattr(connection, "timeout", 60.0)
        try:
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                await action_lock.acquire()
        except TimeoutError as exc:
            raise TransportError("Desktop is busy; tool timed out before starting", input_state="not_started") from exc
        try:
            if time.monotonic() >= deadline:
                raise TransportError("Desktop tool timed out before starting", input_state="not_started")
            if input_action and observation_required:
                raise TransportError(
                    "A previous input outcome is uncertain. Capture a successful fresh "
                    "desktop_screenshot before taking further input.",
                    input_state="not_started",
                )
            # Only the lock owner starts a worker. Cancelled waiters therefore
            # cannot leave an input queued in the executor for later delivery.
            worker = asyncio.get_running_loop().run_in_executor(None, operation, deadline)
            try:
                # wait() leaves the underlying future alive on cancellation.
                # Unlike shield(), it does not log a later worker failure as an
                # abandoned exception before we can retrieve it (Python 3.14).
                await asyncio.wait({worker})
                result = worker.result()
            except asyncio.CancelledError:
                # Cancelling an await cannot stop a synchronous SSH operation.
                # Keep serialization through its deadline and owned cleanup;
                # never close a process from a different request. Shield both
                # MCP's AnyIO cancel scope and repeated native task cancellation.
                with anyio.CancelScope(shield=True):
                    while not worker.done():
                        try:
                            await asyncio.wait({worker})
                        except asyncio.CancelledError:
                            continue
                if not worker.cancelled():
                    worker.exception()  # Retrieve failures from cancelled calls.
                if input_action:
                    observation_required = True
                raise
            if observation:
                # A cancelled screenshot is not an observation delivered to the
                # caller, even when the background capture itself succeeded.
                observation_required = False
            return result
        finally:
            action_lock.release()

    def action_and_frame(method: str, params: dict[str, Any], deadline: float) -> list[TextContent | ImageContent]:
        nonlocal observation_required
        try:
            acknowledgement = connection.request(method, params, deadline=deadline)
        except TransportError as exc:
            if exc.input_state == "not_started":
                if isinstance(exc, RemoteAgentError):
                    raise TransportError(
                        f"{exc} No input was sent.", code=exc.code,
                        input_state="not_started",
                    ) from exc
                raise
            observation_required = True
            raise TransportError(
                f"{exc} Input may have executed. Capture a successful fresh "
                "desktop_screenshot before deciding whether to repeat it.",
                code=exc.code,
            ) from exc
        if acknowledgement.get("ok") is not True:
            observation_required = True
            raise TransportError(
                f"{method}: remote agent did not confirm the input result. Capture a "
                "successful fresh desktop_screenshot before deciding whether to repeat it."
            )
        try:
            return _frame_content(connection.request("screenshot", deadline=deadline))
        except TransportError as exc:
            observation_required = True
            raise TransportError(
                f"{method}: input was acknowledged by the remote agent, but the fresh "
                "screenshot is unavailable. Capture a successful fresh desktop_screenshot "
                "before taking further input."
            ) from exc

    async def input_and_frame(method: str, params: dict[str, Any]) -> list[TextContent | ImageContent]:
        return await serialized(lambda deadline: action_and_frame(method, params, deadline), input_action=True)

    @server.tool(description="Show connection and desktop dimensions for the authorized Raspberry Pi.", annotations=read_only)
    async def desktop_status() -> str:
        return await serialized(lambda deadline: json.dumps(connection.request("status", deadline=deadline), ensure_ascii=False))

    @server.tool(description="Check Pi agent and desktop prerequisites without acquiring a session. This does not reserve the desktop or guarantee a later capture.", annotations=read_only)
    async def desktop_health() -> str:
        return await serialized(lambda deadline: json.dumps(connection.request("health", deadline=deadline), ensure_ascii=False))

    @server.tool(description="Capture the current Raspberry Pi desktop. Image coordinates are original pixels from the top-left.", annotations=read_only, structured_output=False)
    async def desktop_screenshot() -> list[TextContent | ImageContent]:
        return await serialized(lambda deadline: _frame_content(connection.request("screenshot", deadline=deadline)), observation=True)

    @server.tool(description="Release this bridge's Pi desktop session. The MCP server can reconnect on the next tool call.", annotations=release_tool)
    async def desktop_disconnect() -> str:
        acknowledgement = await serialized(lambda deadline: connection.disconnect(deadline=deadline))
        if acknowledgement.get("ok") is not True:
            raise TransportError("Remote agent did not confirm the desktop release")
        return "Desktop session released. A fresh screenshot is required before further input if an earlier action had an uncertain outcome."

    @server.tool(description="Move the pointer to original screenshot pixel coordinates, then show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_move(x: int, y: int) -> list[TextContent | ImageContent]:
        return await input_and_frame("move", {"x": x, "y": y})

    @server.tool(description="Click at original screenshot pixel coordinates and show a fresh screenshot. Button is left, right, or middle.", annotations=input_tool, structured_output=False)
    async def desktop_click(x: int, y: int, button: str = "left", count: int = 1) -> list[TextContent | ImageContent]:
        return await input_and_frame("click", {"x": x, "y": y, "button": button, "count": count})

    @server.tool(description="Drag between original screenshot pixel coordinates and show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_drag(start_x: int, start_y: int, end_x: int, end_y: int) -> list[TextContent | ImageContent]:
        return await input_and_frame("drag", {"start_x": start_x, "start_y": start_y, "end_x": end_x, "end_y": end_y})

    @server.tool(description="Scroll at an explicit target in original screenshot pixels (x and y together), then show a fresh screenshot. Direction is up, down, left, or right.", annotations=input_tool, structured_output=False)
    async def desktop_scroll(direction: str, ticks: int = 1, x: int | None = None, y: int | None = None) -> list[TextContent | ImageContent]:
        params: dict[str, Any] = {"direction": direction, "ticks": ticks}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        return await input_and_frame("scroll", params)

    @server.tool(name="desktop_type", description="Type text into the focused desktop app and show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_type(text: str) -> list[TextContent | ImageContent]:
        return await input_and_frame("type_text", {"text": text})

    @server.tool(description="Press a key combination in the focused desktop app and show a fresh screenshot. Keys is a list such as ['Control_L','a'].", annotations=input_tool, structured_output=False)
    async def desktop_key(keys: list[str]) -> list[TextContent | ImageContent]:
        return await input_and_frame("key", {"keys": keys})

    return server
