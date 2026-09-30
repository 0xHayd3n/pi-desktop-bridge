"""MCP tools for viewing and controlling a remote Pi desktop."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
import json
import re
import struct
import time
from typing import Any
import zlib

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import StrictInt

from .transport import MAX_RESPONSE_BYTES, RemoteAgentError, SSHTransport, TransportError


MAX_PIXELS = 16_777_216
MAX_PNG_BYTES = MAX_PIXELS * 4 + 1_048_576
_FRAME_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)


@dataclass(frozen=True)
class FrameView:
    view_id: str
    width: int
    height: int
    desktop_width: int
    desktop_height: int
    region_x: int
    region_y: int
    region_width: int
    region_height: int

    def map_point(self, x: int, y: int) -> tuple[int, int]:
        if type(x) is not int or type(y) is not int or not (0 <= x < self.width and 0 <= y < self.height):
            raise TransportError("Image coordinates are outside the current view", input_state="not_started")
        return (
            self.region_x + ((2 * x + 1) * self.region_width) // (2 * self.width),
            self.region_y + ((2 * y + 1) * self.region_height) // (2 * self.height),
        )


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _valid_png(data: bytes, width: int, height: int) -> bool:
    if len(data) < 57 or len(data) > MAX_PNG_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    first = True
    has_idat = False
    after_idat = False
    decoder = zlib.decompressobj()
    row_stride = 0
    expected_raw = 0
    raw_count = 0
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return False
        if zlib.crc32(data[offset + 4:end - 4]) != struct.unpack_from(">I", data, end - 4)[0]:
            return False
        if len(kind) != 4 or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind):
            return False
        if first:
            if kind != b"IHDR" or length != 13 or struct.unpack_from(">II", data, offset + 8) != (width, height):
                return False
            bit_depth, color_type, compression, filtering, interlace = data[offset + 16:offset + 21]
            if (bit_depth, compression, filtering, interlace) != (8, 0, 0, 0) or color_type not in (2, 6):
                return False
            row_stride = 1 + width * (3 if color_type == 2 else 4)
            expected_raw = row_stride * height
            first = False
        elif kind == b"IDAT":
            if after_idat or decoder.eof:
                return False
            has_idat = True
            compressed = data[offset + 8:end - 4]
            # Restrict decompressed output per call. This checks the raster
            # without retaining the full native framebuffer in memory.
            for start in range(0, len(compressed), 65_536):
                pending = compressed[start:start + 65_536]
                while pending:
                    try:
                        output = decoder.decompress(pending, min(65_536, expected_raw - raw_count + 1))
                    except zlib.error:
                        return False
                    pending = decoder.unconsumed_tail
                    if decoder.unused_data:
                        return False
                    position = 0
                    while position < len(output):
                        if raw_count >= expected_raw:
                            return False
                        in_row = raw_count % row_stride
                        if in_row == 0:
                            if output[position] > 4:
                                return False
                            position += 1
                            raw_count += 1
                        else:
                            take = min(len(output) - position, row_stride - in_row)
                            position += take
                            raw_count += take
                    if pending and not output:
                        return False
        elif kind == b"IEND":
            return (has_idat and length == 0 and end == len(data)
                    and decoder.eof and not decoder.unused_data and raw_count == expected_raw)
        elif kind == b"IHDR" or not kind[0] & 0x20:
            return False
        elif has_idat:
            after_idat = True
        offset = end
    return False


def _validated_frame(
    result: dict[str, Any], requested: dict[str, Any] | None = None,
) -> tuple[list[TextContent | ImageContent], FrameView]:
    encoded = result.get("image_base64")
    mime = result.get("mime_type")
    width = result.get("width")
    height = result.get("height")
    desktop_width = result.get("desktop_width")
    desktop_height = result.get("desktop_height")
    region = result.get("region")
    frame_id = result.get("frame_id")
    if (
        not isinstance(encoded, str) or mime != "image/png"
        or not _positive_int(width) or not _positive_int(height)
        or width * height > MAX_PIXELS
        or not _positive_int(desktop_width) or not _positive_int(desktop_height)
        or desktop_width > 65_535 or desktop_height > 65_535
        or desktop_width * desktop_height > MAX_PIXELS
        or not isinstance(region, dict)
        or set(region) != {"x", "y", "width", "height"}
        or any(type(region[key]) is not int for key in region)
        or region["x"] < 0 or region["y"] < 0
        or region["width"] <= 0 or region["height"] <= 0
        or region["width"] * region["height"] > MAX_PIXELS
        or region["x"] + region["width"] > desktop_width
        or region["y"] + region["height"] > desktop_height
        or width > region["width"]
        or height != max(1, region["height"] * width // region["width"])
        or not isinstance(frame_id, str) or _FRAME_ID.fullmatch(frame_id) is None
        or len(encoded) > min(MAX_RESPONSE_BYTES, 4 * ((MAX_PNG_BYTES + 2) // 3))
    ):
        raise TransportError("SSH agent returned invalid screenshot metadata")
    if requested is not None:
        expected_region = (
            {key: requested[key] for key in ("x", "y", "width", "height")}
            if "x" in requested else
            {"x": 0, "y": 0, "width": desktop_width, "height": desktop_height}
        )
        expected_width = min(region["width"], requested.get("max_width", region["width"]))
        if region != expected_region or width != expected_width:
            raise TransportError("SSH agent returned unexpected screenshot dimensions")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TransportError("SSH agent returned an invalid screenshot") from exc
    if not _valid_png(data, width, height):
        raise TransportError("SSH agent returned an invalid PNG screenshot")
    view = FrameView(frame_id, width, height, desktop_width, desktop_height,
                     region["x"], region["y"], region["width"], region["height"])
    text = {
        "description": f"Desktop screenshot: {width} × {height} pixels.",
        "view_id": frame_id,
        "width": width, "height": height,
        "desktop_width": desktop_width, "desktop_height": desktop_height,
        "region": region,
        "coordinate_instruction": (
            "Without view_id, mouse coordinates use original desktop pixels. "
            "With this view_id, mouse coordinates use pixels in this returned image; "
            "the bridge maps them to original desktop pixels."
        ),
    }
    return [TextContent(type="text", text=json.dumps(text, ensure_ascii=False)),
            ImageContent(type="image", data=encoded, mimeType="image/png")], view


def _frame_content(result: dict[str, Any]) -> list[TextContent | ImageContent]:
    return _validated_frame(result)[0]


def _screenshot_params(
    x: int | None, y: int | None, width: int | None, height: int | None,
    max_width: int | None,
) -> dict[str, int]:
    values = (x, y, width, height)
    if any(value is not None for value in values) and not all(value is not None for value in values):
        raise TransportError("Screenshot region requires x, y, width and height together", input_state="not_started")
    params: dict[str, int] = {}
    if all(value is not None for value in values):
        if (
            any(type(value) is not int for value in values)
            or x < 0 or y < 0 or width <= 0 or height <= 0
            or width * height > MAX_PIXELS
        ):
            raise TransportError("Invalid screenshot region", input_state="not_started")
        params.update(x=x, y=y, width=width, height=height)
    if max_width is not None:
        if type(max_width) is not int or not 1 <= max_width <= 65_535:
            raise TransportError("max_width must be an integer from 1 to 65535", input_state="not_started")
        params["max_width"] = max_width
    return params


def create_server(host: str, *, transport: SSHTransport | None = None) -> FastMCP:
    """Create the server; the SSH connection opens on the first tool call."""
    connection = transport or SSHTransport(host)
    action_lock = asyncio.Lock()
    observation_required = False
    latest_view: FrameView | None = None
    server = FastMCP(
        "Pi Desktop Bridge",
        instructions=(
            "Control the user's authorized Raspberry Pi desktop. Mouse coordinates "
            "use original desktop pixels unless a current screenshot view_id is supplied; "
            "then they use pixels in that returned image. After each "
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
        observation: bool = False, frame_result: bool = False,
        invalidate_view: bool = False,
    ) -> Any:
        nonlocal observation_required, latest_view
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
            if invalidate_view:
                latest_view = None
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
                if input_action or frame_result:
                    latest_view = None
                raise
            except TransportError:
                if frame_result and not input_action:
                    latest_view = None
                raise
            if frame_result:
                content, latest_view = result
                result = content
            if observation:
                # A cancelled screenshot is not an observation delivered to the
                # caller, even when the background capture itself succeeded.
                observation_required = False
            return result
        finally:
            action_lock.release()

    def mapped_params(method: str, params: dict[str, Any], view_id: str | None) -> dict[str, Any]:
        if view_id is None:
            return params
        if not isinstance(view_id, str) or latest_view is None or latest_view.view_id != view_id:
            raise TransportError("stale_view: capture a fresh desktop_screenshot before mapped input",
                                 code="stale_view", input_state="not_started")
        mapped = dict(params)
        if method == "drag":
            mapped["start_x"], mapped["start_y"] = latest_view.map_point(params["start_x"], params["start_y"])
            mapped["end_x"], mapped["end_y"] = latest_view.map_point(params["end_x"], params["end_y"])
        else:
            if "x" not in params or "y" not in params or params["x"] is None or params["y"] is None:
                raise TransportError("Mapped scroll requires both x and y", input_state="not_started")
            mapped["x"], mapped["y"] = latest_view.map_point(params["x"], params["y"])
        mapped["frame_id"] = view_id
        return mapped

    def action_and_frame(
        method: str, params: dict[str, Any], view_id: str | None, deadline: float,
    ) -> tuple[list[TextContent | ImageContent], FrameView]:
        nonlocal observation_required, latest_view
        params = mapped_params(method, params, view_id)
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
            latest_view = None
            raise TransportError(
                f"{exc} Input may have executed. Capture a successful fresh "
                "desktop_screenshot before deciding whether to repeat it.",
                code=exc.code,
            ) from exc
        latest_view = None
        if acknowledgement.get("ok") is not True:
            observation_required = True
            raise TransportError(
                f"{method}: remote agent did not confirm the input result. Capture a "
                "successful fresh desktop_screenshot before deciding whether to repeat it."
            )
        try:
            return _validated_frame(connection.request("screenshot", deadline=deadline), {})
        except TransportError as exc:
            observation_required = True
            raise TransportError(
                f"{method}: input was acknowledged by the remote agent, but the fresh "
                "screenshot is unavailable. Capture a successful fresh desktop_screenshot "
                "before taking further input."
            ) from exc

    async def input_and_frame(
        method: str, params: dict[str, Any], view_id: str | None = None,
    ) -> list[TextContent | ImageContent]:
        return await serialized(lambda deadline: action_and_frame(method, params, view_id, deadline),
                                input_action=True, frame_result=True)

    @server.tool(description="Show connection and desktop dimensions for the authorized Raspberry Pi.", annotations=read_only)
    async def desktop_status() -> str:
        return await serialized(lambda deadline: json.dumps(connection.request("status", deadline=deadline), ensure_ascii=False))

    @server.tool(description="Check Pi agent and desktop prerequisites without acquiring a session. This does not reserve the desktop or guarantee a later capture.", annotations=read_only)
    async def desktop_health() -> str:
        return await serialized(lambda deadline: json.dumps(connection.request("health", deadline=deadline), ensure_ascii=False))

    @server.tool(description="Capture the Pi desktop, optionally a source region in original pixels and/or a downscaled image. The returned view_id enables image-pixel mouse coordinates.", annotations=read_only, structured_output=False)
    async def desktop_screenshot(
        x: StrictInt | None = None, y: StrictInt | None = None,
        width: StrictInt | None = None, height: StrictInt | None = None,
        max_width: StrictInt | None = None,
    ) -> list[TextContent | ImageContent]:
        params = _screenshot_params(x, y, width, height, max_width)
        return await serialized(
            lambda deadline: _validated_frame(connection.request("screenshot", params, deadline=deadline), params),
            observation="x" not in params, frame_result=True,
        )

    @server.tool(description="Release this bridge's Pi desktop session. The MCP server can reconnect on the next tool call.", annotations=release_tool)
    async def desktop_disconnect() -> str:
        acknowledgement = await serialized(lambda deadline: connection.disconnect(deadline=deadline), invalidate_view=True)
        if acknowledgement.get("ok") is not True:
            raise TransportError("Remote agent did not confirm the desktop release")
        return "Desktop session released. A fresh screenshot is required before further input if an earlier action had an uncertain outcome."

    @server.tool(description="Move the pointer in original desktop pixels, or image pixels with the current view_id, then show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_move(x: StrictInt, y: StrictInt, view_id: str | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("move", {"x": x, "y": y}, view_id)

    @server.tool(description="Click in original desktop pixels, or image pixels with the current view_id, then show a fresh screenshot. Button is left, right, or middle.", annotations=input_tool, structured_output=False)
    async def desktop_click(x: StrictInt, y: StrictInt, button: str = "left", count: StrictInt = 1, view_id: str | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("click", {"x": x, "y": y, "button": button, "count": count}, view_id)

    @server.tool(description="Drag in original desktop pixels, or image pixels with the current view_id, then show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_drag(start_x: StrictInt, start_y: StrictInt, end_x: StrictInt, end_y: StrictInt, view_id: str | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("drag", {"start_x": start_x, "start_y": start_y, "end_x": end_x, "end_y": end_y}, view_id)

    @server.tool(description="Scroll at an explicit target in original pixels (x and y together), or image pixels with the current view_id, then show a fresh screenshot. Direction is up, down, left, or right.", annotations=input_tool, structured_output=False)
    async def desktop_scroll(direction: str, ticks: StrictInt = 1, x: StrictInt | None = None, y: StrictInt | None = None, view_id: str | None = None) -> list[TextContent | ImageContent]:
        params: dict[str, Any] = {"direction": direction, "ticks": ticks}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        return await input_and_frame("scroll", params, view_id)

    @server.tool(name="desktop_type", description="Type text into the focused desktop app and show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_type(text: str) -> list[TextContent | ImageContent]:
        return await input_and_frame("type_text", {"text": text})

    @server.tool(description="Press a key combination in the focused desktop app and show a fresh screenshot. Keys is a list such as ['Control_L','a'].", annotations=input_tool, structured_output=False)
    async def desktop_key(keys: list[str]) -> list[TextContent | ImageContent]:
        return await input_and_frame("key", {"keys": keys})

    return server
