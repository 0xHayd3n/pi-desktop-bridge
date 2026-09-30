"""MCP tools for viewing and controlling a remote Pi desktop."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
from contextlib import asynccontextmanager
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
    if not isinstance(result, dict):
        raise TransportError("SSH agent returned invalid screenshot metadata")
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


def _capture_params(max_width: int | None) -> dict[str, int]:
    """Validate a post-action width before there is any chance of input."""
    return _screenshot_params(None, None, None, None, max_width)


def _wait_params(
    x: int | None, y: int | None, width: int | None, height: int | None,
    max_width: int | None, stable_ms: int, timeout_ms: int, poll_ms: int,
) -> dict[str, int]:
    params = _screenshot_params(x, y, width, height, max_width)
    if (
        type(stable_ms) is not int or not 50 <= stable_ms <= 2000
        or type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000
        or type(poll_ms) is not int or not 50 <= poll_ms <= 1000
        or not poll_ms <= stable_ms <= timeout_ms
    ):
        raise TransportError("Invalid stability timing: require 50 <= poll_ms <= stable_ms <= timeout_ms, stable_ms <= 2000, poll_ms <= 1000, timeout_ms <= 10000", input_state="not_started")
    params.update(stable_ms=stable_ms, timeout_ms=timeout_ms, poll_ms=poll_ms)
    return params


def _validated_stability(result: dict[str, Any], params: dict[str, int]) -> dict[str, Any]:
    stability = result.get("stability")
    if not isinstance(stability, dict) or set(stability) != {
        "stable", "timed_out", "elapsed_ms", "samples", "stable_ms", "timeout_ms", "poll_ms",
    }:
        raise TransportError("SSH agent returned invalid stability metadata")
    stable = stability["stable"]
    timed_out = stability["timed_out"]
    elapsed = stability["elapsed_ms"]
    samples = stability["samples"]
    if (
        type(stable) is not bool or type(timed_out) is not bool or timed_out is stable
        or type(elapsed) is not int or not 0 <= elapsed <= params["timeout_ms"]
        or type(samples) is not int or not 1 <= samples <= 201
        or any(type(stability[key]) is not int or stability[key] != params[key]
               for key in ("stable_ms", "timeout_ms", "poll_ms"))
        or stable and (samples < 2 or elapsed < params["stable_ms"])
        or timed_out and elapsed != params["timeout_ms"]
    ):
        raise TransportError("SSH agent returned invalid stability metadata")
    return stability


def _validated_wait_frame(
    result: dict[str, Any], params: dict[str, int],
) -> tuple[list[TextContent | ImageContent], FrameView]:
    content, view = _validated_frame(result, params)
    stability = _validated_stability(result, params)
    metadata = json.loads(content[0].text)
    metadata["stability"] = stability
    content[0] = TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))
    return content, view


def create_server(
    host: str, *, transport: SSHTransport | None = None,
    capture_max_width: int | None = None, idle_timeout: int = 300,
) -> FastMCP:
    """Create the server; the SSH connection opens on the first tool call."""
    default_capture = _capture_params(capture_max_width)
    if type(idle_timeout) is not int or not 0 <= idle_timeout <= 3600:
        raise ValueError("idle_timeout must be an integer from 0 to 3600 seconds")
    connection = transport or SSHTransport(host)
    action_lock = asyncio.Lock()
    observation_required = False
    latest_view: FrameView | None = None
    last_activity = 0.0
    local_session_active = False
    release_pending = False
    auto_release_count = 0
    activity_changed = asyncio.Event()

    def record_activity() -> None:
        nonlocal last_activity, local_session_active, release_pending
        last_activity = time.monotonic()
        local_session_active = True
        release_pending = True
        activity_changed.set()

    def possibly_held(error: BaseException, input_action: bool) -> bool:
        if isinstance(error, TransportError):
            if input_action:
                return error.input_state != "not_started"
            if error.code in {"invalid_params", "stale_view", "busy", "agent_source_mismatch"}:
                return False
        # A failed/invalid capture may follow lease acquisition even when no
        # input was sent. It must age out just like an uncertain input result.
        return True

    async def finish_worker(worker: asyncio.Future) -> None:
        # Neither AnyIO cancellation nor repeated native cancellation may
        # abandon an executor worker while it owns the transport/action lock.
        with anyio.CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.wait({worker})
                except asyncio.CancelledError:
                    continue

    async def release_locked(*, automatic: bool) -> None:
        nonlocal latest_view, release_pending, local_session_active, auto_release_count
        latest_view = None
        release_pending = False  # One attempt per activity interval, including failures.
        deadline = time.monotonic() + getattr(connection, "timeout", 60.0)
        worker = asyncio.get_running_loop().run_in_executor(
            None, lambda: connection.disconnect(deadline=deadline),
        )
        cancelled = False
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            cancelled = True
            await finish_worker(worker)
        try:
            acknowledgement = worker.result()
            if isinstance(acknowledgement, dict) and acknowledgement.get("ok") is True:
                local_session_active = False
                if automatic:
                    auto_release_count += 1
        except Exception:
            # Keep the conservative local state and recovery guard. The next
            # explicit observation can reconnect; background cleanup stays quiet.
            pass
        if cancelled:
            raise asyncio.CancelledError

    async def idle_release() -> None:
        while True:
            activity_changed.clear()
            if not release_pending:
                await activity_changed.wait()
                continue
            remaining = last_activity + idle_timeout - time.monotonic()
            if remaining > 0:
                try:
                    async with asyncio.timeout(remaining):
                        await activity_changed.wait()
                    continue
                except TimeoutError:
                    pass
            async with action_lock:
                # An active request may have renewed the interval while the
                # timer waited. Never interrupt it or expire its fresh result.
                if release_pending and time.monotonic() >= last_activity + idle_timeout:
                    await release_locked(automatic=True)

    async def shutdown_release() -> None:
        async with action_lock:
            await release_locked(automatic=False)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        timer = (asyncio.create_task(idle_release(), name="pi-desktop-idle-release")
                 if idle_timeout else None)
        try:
            yield
        finally:
            # The SDK joins request handlers before exiting this lifespan.
            # Also join any timer-owned cleanup before beginning final cleanup.
            with anyio.CancelScope(shield=True):
                if timer is not None:
                    timer.cancel()
                    await finish_worker(timer)
                    if not timer.cancelled():
                        timer.result()
                cleanup = asyncio.create_task(shutdown_release())
                await finish_worker(cleanup)
                cleanup.result()

    server = FastMCP(
        "Pi Desktop Bridge",
        lifespan=lifespan,
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
        invalidate_view: bool = False, desktop_activity: bool = False,
        status_result: bool = False, release_session: bool = False,
    ) -> Any:
        nonlocal observation_required, latest_view, local_session_active, release_pending
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
            if release_session:
                release_pending = False
                activity_changed.set()
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
                await finish_worker(worker)
                if not worker.cancelled():
                    error = worker.exception()  # Retrieve failures from cancelled calls.
                    if desktop_activity and (error is None or possibly_held(error, input_action)):
                        record_activity()
                if input_action:
                    observation_required = True
                if input_action or frame_result:
                    latest_view = None
                raise
            except TransportError as exc:
                if desktop_activity and possibly_held(exc, input_action):
                    record_activity()
                if frame_result and not input_action:
                    latest_view = None
                raise
            if desktop_activity:
                record_activity()
            if release_session and isinstance(result, dict) and result.get("ok") is True:
                local_session_active = False
            if frame_result:
                content, latest_view = result
                result = content
            if observation:
                # A cancelled screenshot is not an observation delivered to the
                # caller, even when the background capture itself succeeded.
                observation_required = False
            if status_result:
                if result.get("session_active") is False:
                    latest_view = None
                    local_session_active = False
                result = json.dumps({
                    **result, "observation_required": observation_required,
                    "recovery_action": "desktop_screenshot" if observation_required else None,
                    "session_policy": {
                        "idle_timeout_seconds": idle_timeout,
                        "local_session_active": local_session_active,
                        "idle_remaining_seconds": (
                            max(0.0, last_activity + idle_timeout - time.monotonic())
                            if release_pending and idle_timeout else None
                        ),
                        "auto_release_count": auto_release_count,
                    },
                }, ensure_ascii=False)
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
        method: str, params: dict[str, Any], view_id: str | None,
        capture_params: dict[str, int], deadline: float,
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
            return _validated_frame(
                connection.request("screenshot", capture_params, deadline=deadline), capture_params,
            )
        except TransportError as exc:
            observation_required = True
            raise TransportError(
                f"{method}: input was acknowledged by the remote agent, but the fresh "
                "screenshot is unavailable. Capture a successful fresh desktop_screenshot "
                "before taking further input."
            ) from exc

    async def input_and_frame(
        method: str, params: dict[str, Any], view_id: str | None = None,
        capture_max_width: int | None = None,
    ) -> list[TextContent | ImageContent]:
        capture_params = (
            default_capture if capture_max_width is None else _capture_params(capture_max_width)
        )
        return await serialized(lambda deadline: action_and_frame(method, params, view_id, capture_params, deadline),
                                input_action=True, frame_result=True, desktop_activity=True)

    def status_with_recovery(method: str, deadline: float) -> dict[str, Any]:
        result = connection.request(method, deadline=deadline)
        if not isinstance(result, dict) or {"observation_required", "recovery_action", "session_policy"} & result.keys():
            raise TransportError("SSH agent returned conflicting status metadata")
        return result

    @server.tool(description="Show connection and desktop dimensions for the authorized Raspberry Pi.", annotations=read_only)
    async def desktop_status() -> str:
        return await serialized(lambda deadline: status_with_recovery("status", deadline),
                                desktop_activity=True, status_result=True)

    @server.tool(description="Check Pi agent and desktop prerequisites without acquiring a session. This does not reserve the desktop or guarantee a later capture.", annotations=read_only)
    async def desktop_health() -> str:
        return await serialized(lambda deadline: status_with_recovery("health", deadline), status_result=True)

    @server.tool(description="Capture the Pi desktop, optionally a source region in original pixels and/or a downscaled image. The returned view_id enables image-pixel mouse coordinates.", annotations=read_only, structured_output=False)
    async def desktop_screenshot(
        x: StrictInt | None = None, y: StrictInt | None = None,
        width: StrictInt | None = None, height: StrictInt | None = None,
        max_width: StrictInt | None = None,
    ) -> list[TextContent | ImageContent]:
        params = _screenshot_params(x, y, width, height, max_width)
        return await serialized(
            lambda deadline: _validated_frame(connection.request("screenshot", params, deadline=deadline), params),
            observation="x" not in params, frame_result=True, desktop_activity=True,
        )

    @server.tool(description="Observe sampled desktop pixels until unchanged for the requested duration, or sampling times out. Returns the last image and stability status; sampled equality does not prove the app is ready. A full desktop_screenshot is still required to recover from an uncertain input.", annotations=read_only, structured_output=False)
    async def desktop_wait_for_stable(
        x: StrictInt | None = None, y: StrictInt | None = None,
        width: StrictInt | None = None, height: StrictInt | None = None,
        max_width: StrictInt | None = None,
        stable_ms: StrictInt = 300, timeout_ms: StrictInt = 5000,
        poll_ms: StrictInt = 100,
    ) -> list[TextContent | ImageContent]:
        params = _wait_params(x, y, width, height, max_width, stable_ms, timeout_ms, poll_ms)
        return await serialized(
            lambda deadline: _validated_wait_frame(
                connection.request("wait_for_stable", params, deadline=deadline), params,
            ),
            frame_result=True, desktop_activity=True,
        )

    @server.tool(description="Release this bridge's Pi desktop session. The MCP server can reconnect on the next tool call.", annotations=release_tool)
    async def desktop_disconnect() -> str:
        acknowledgement = await serialized(lambda deadline: connection.disconnect(deadline=deadline),
                                           invalidate_view=True, release_session=True)
        if acknowledgement.get("ok") is not True:
            raise TransportError("Remote agent did not confirm the desktop release")
        return "Desktop session released. A fresh screenshot is required before further input if an earlier action had an uncertain outcome."

    @server.tool(description="Move the pointer in original desktop pixels, or image pixels with the current view_id, then show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_move(x: StrictInt, y: StrictInt, view_id: str | None = None, capture_max_width: StrictInt | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("move", {"x": x, "y": y}, view_id, capture_max_width)

    @server.tool(description="Click in original desktop pixels, or image pixels with the current view_id, then show a fresh screenshot. Button is left, right, or middle.", annotations=input_tool, structured_output=False)
    async def desktop_click(x: StrictInt, y: StrictInt, button: str = "left", count: StrictInt = 1, view_id: str | None = None, capture_max_width: StrictInt | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("click", {"x": x, "y": y, "button": button, "count": count}, view_id, capture_max_width)

    @server.tool(description="Drag in original desktop pixels, or image pixels with the current view_id, then show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_drag(start_x: StrictInt, start_y: StrictInt, end_x: StrictInt, end_y: StrictInt, view_id: str | None = None, capture_max_width: StrictInt | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("drag", {"start_x": start_x, "start_y": start_y, "end_x": end_x, "end_y": end_y}, view_id, capture_max_width)

    @server.tool(description="Scroll at an explicit target in original pixels (x and y together), or image pixels with the current view_id, then show a fresh screenshot. Direction is up, down, left, or right.", annotations=input_tool, structured_output=False)
    async def desktop_scroll(direction: str, ticks: StrictInt = 1, x: StrictInt | None = None, y: StrictInt | None = None, view_id: str | None = None, capture_max_width: StrictInt | None = None) -> list[TextContent | ImageContent]:
        params: dict[str, Any] = {"direction": direction, "ticks": ticks}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        return await input_and_frame("scroll", params, view_id, capture_max_width)

    @server.tool(name="desktop_type", description="Type text into the focused desktop app and show a fresh screenshot.", annotations=input_tool, structured_output=False)
    async def desktop_type(text: str, capture_max_width: StrictInt | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("type_text", {"text": text}, capture_max_width=capture_max_width)

    @server.tool(description="Press a key combination in the focused desktop app and show a fresh screenshot. Keys is a list such as ['Control_L','a'].", annotations=input_tool, structured_output=False)
    async def desktop_key(keys: list[str], capture_max_width: StrictInt | None = None) -> list[TextContent | ImageContent]:
        return await input_and_frame("key", {"keys": keys}, capture_max_width=capture_max_width)

    return server
