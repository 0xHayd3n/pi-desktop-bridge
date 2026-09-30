"""Authenticated loopback UI, using the same serialized MCP desktop tools."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import anyio
from mcp.types import ImageContent, TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from .server import create_server
from .transport import TransportError, validate_host


MAX_BODY_BYTES = 65_536
_MESSAGES = {
    "unauthorized": "Open the dashboard using its private launch link.",
    "forbidden": "This request did not come from this localhost dashboard.",
    "invalid_request": "The request contains invalid or unsupported fields.",
    "body_too_large": "The request exceeds the 64 KiB limit.",
    "operation_pending": "Another dashboard operation is still running.",
    "not_connected": "Connect to a Pi first.",
    "stale_session": "The connection changed. Capture a fresh desktop before input.",
    "stale_geometry": "The desktop size changed. Capture a fresh desktop before input.",
    "observation_required": "Capture a fresh desktop before further input. Review the result before repeating an action.",
    "busy": "Another desktop client holds the Pi session. Release that client before trying again.",
    "agent_source_mismatch": "The Pi agent differs from this bridge. Reconnect with agent deployment enabled.",
    "invalid_params": "The Pi rejected the input parameters before delivery.",
    "host_key_confirmation_required": "Verify the Pi host key fingerprint before connecting.",
    "host_key_changed": "The Pi host key changed. Verify the change before reconnecting.",
    "authentication_failed": "SSH authentication failed. Check the login details.",
    "connection_failed": "Could not connect to the Pi. Check its address and SSH settings.",
    "invalid_details": "The connection details are invalid.",
    "deployment_failed": "The Pi agent could not be deployed. Check SSH access and available storage.",
    "desktop_not_ready": "The Pi desktop is not ready. Check its graphical session.",
    "dependency_missing": "An SSH or Pi desktop dependency is missing.",
    "transport_error": "The desktop request failed. Capture a fresh desktop before further input.",
    "internal_error": "The dashboard could not complete this request.",
    "stream_failed": "The desktop stream stopped. Inspect the Pi before reconnecting.",
    "stream_active": "The live stream owns desktop input. Disconnect it before using snapshot tools.",
}


class DashboardError(Exception):
    def __init__(self, code: str, status: int = 400, *, host_key: dict | None = None):
        self.code = code if code in _MESSAGES else "internal_error"
        self.status = status
        self.host_key = host_key
        super().__init__(_MESSAGES[self.code])

    def public(self) -> dict:
        return {"code": self.code, "message": _MESSAGES[self.code]}


def _invalid() -> None:
    raise DashboardError("invalid_request")


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _connection_details(body: dict) -> dict:
    if set(body) - {"mode", "host", "username", "password", "port", "deploy", "expected_fingerprint"}:
        _invalid()
    mode = body.get("mode", "existing")
    host = body.get("host")
    if mode not in ("existing", "password") or not isinstance(host, str):
        _invalid()
    try:
        validate_host(host)
    except ValueError:
        _invalid()
    if type(body.get("deploy", True)) is not bool:
        _invalid()
    if mode == "existing":
        if set(body) - {"mode", "host", "deploy"}:
            _invalid()
    else:
        if (not isinstance(body.get("username"), str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", body["username"]) is None
                or not isinstance(body.get("password", ""), str)
                or len(body.get("password", "")) > 4096
                or not _integer(body.get("port", 22), 1, 65535)):
            _invalid()
        fingerprint = body.get("expected_fingerprint")
        if fingerprint is not None and (not isinstance(fingerprint, str)
                or re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", fingerprint) is None):
            _invalid()
    return {**body, "mode": mode, "deploy": body.get("deploy", True)}


def _action_details(body: dict) -> tuple[str, dict]:
    if set(body) != {"generation", "action", "args", "desktop_width", "desktop_height"}:
        _invalid()
    if (not _integer(body["generation"], 0, 2**53 - 1)
            or not _integer(body["desktop_width"], 1, 65535)
            or not _integer(body["desktop_height"], 1, 65535)
            or not isinstance(body["args"], dict) or not isinstance(body["action"], str)):
        _invalid()
    action, args = body["action"], body["args"]
    fields = {
        "move": ({"x", "y"}, set()),
        "click": ({"x", "y"}, {"button", "count"}),
        "drag": ({"start_x", "start_y", "end_x", "end_y"}, set()),
        "scroll": ({"direction"}, {"ticks", "x", "y"}),
        "type": ({"text"}, set()),
        "key": ({"keys"}, set()),
    }
    if action not in fields:
        _invalid()
    required, optional = fields[action]
    if not required <= args.keys() or args.keys() - required - optional:
        _invalid()
    for x, y in (("x", "y"), ("start_x", "start_y"), ("end_x", "end_y")):
        if (x in args) != (y in args):
            _invalid()
        if x in args and (not _integer(args[x], 0, body["desktop_width"] - 1)
                          or not _integer(args[y], 0, body["desktop_height"] - 1)):
            _invalid()
    if action == "click" and (args.get("button", "left") not in ("left", "middle", "right")
                              or not _integer(args.get("count", 1), 1, 2)):
        _invalid()
    if action == "scroll" and (args["direction"] not in ("up", "down", "left", "right")
                               or not _integer(args.get("ticks", 1), 1, 50)):
        _invalid()
    if action == "type" and (not isinstance(args["text"], str) or len(args["text"]) > 4096):
        _invalid()
    if action == "key" and (not isinstance(args["keys"], list) or not 1 <= len(args["keys"]) <= 8
                            or any(not isinstance(key, str) or not 1 <= len(key) <= 64 for key in args["keys"])):
        _invalid()
    return "desktop_" + action, args


async def _join(worker: asyncio.Future) -> None:
    """Retain ownership through executor work, even with repeated cancellation."""
    with anyio.CancelScope(shield=True):
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                continue


async def _complete(coroutine) -> Any:
    """Finish owned cleanup before propagating cancellation to its caller."""
    worker = asyncio.create_task(coroutine)
    cancelled = False
    with anyio.CancelScope(shield=True):
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            cancelled = True
            await _join(worker)
    try:
        return worker.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


def _transport_error(error: BaseException) -> TransportError | None:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TransportError):
            return error
        error = error.__cause__
    return None


class DashboardController:
    """One GUI-owned transport and lease; no unbounded pending operation queue."""

    def __init__(self, default_host, connection_factory, capture_max_width, stream_factory=None):
        self.host = default_host
        self.connection_factory = connection_factory
        self.capture_max_width = capture_max_width
        self.heartbeat_timeout = 30.0
        self.generation = 0
        self.observation_required = True
        self.error = None
        self._lock = asyncio.Lock()
        self._operation_active = False
        self._transport = None
        self._server = None
        self._context = None
        self._geometry = None
        self._lease_busy = False
        self._last_activity = time.monotonic()
        self._activity_changed = asyncio.Event()
        self._closing = False
        self.stream_factory = stream_factory
        self._ticket = None
        self._stream = None
        self._stream_ws = None
        self._stream_task = None

    def touch(self):
        self._last_activity = time.monotonic()
        self._activity_changed.set()

    def snapshot(self) -> dict:
        result = {"connected": self._server is not None, "host": self.host,
                  "generation": self.generation,
                  "busy": self._operation_active or self._lock.locked() or self._lease_busy,
                  "observation_required": self.observation_required}
        result["streaming"] = self._stream is not None
        if self.error:
            result["error"] = self.error
        return result

    @asynccontextmanager
    async def operation(self):
        # Reserve the request slot before awaiting the lock. A timer waiter can
        # already own the next turn even while Lock.locked() briefly is false.
        if self._operation_active or self._lock.locked():
            raise DashboardError("operation_pending", 409)
        if self._closing:
            raise DashboardError("not_connected", 409)
        self._operation_active = True
        try:
            async with self._lock:
                yield
        finally:
            self.touch()
            self._operation_active = False

    async def _close_candidate(self, candidate):
        close = getattr(candidate, "close", None)
        if close is not None:
            worker = asyncio.get_running_loop().run_in_executor(None, close)
            await _join(worker)
            try:
                worker.result()
            except Exception:
                pass

    async def _release(self):
        self._ticket = None
        await self._stop_stream()
        context, transport = self._context, self._transport
        self._context = self._transport = self._server = None
        self._geometry = None
        self.observation_required = True
        self._lease_busy = False
        try:
            if context is not None:
                await context.__aexit__(None, None, None)
        finally:
            if transport is not None:
                await self._close_candidate(transport)

    async def connect(self, body: dict):
        details = _connection_details(body)
        try:
            async with self.operation():
                self.generation += 1
                await _complete(self._release())
                self.host = details["host"]
                self.error = None
                candidate = None
                worker = asyncio.get_running_loop().run_in_executor(None, self.connection_factory, details)
                try:
                    try:
                        await asyncio.wait({worker})
                        candidate = worker.result()
                    except asyncio.CancelledError:
                        await _join(worker)
                        if not worker.cancelled() and worker.exception() is None:
                            candidate = worker.result()
                        raise
                    server = create_server(self.host, transport=candidate,
                                           capture_max_width=self.capture_max_width, idle_timeout=0)
                    context = server.settings.lifespan(server)
                    await context.__aenter__()
                    self._transport, self._server, self._context = candidate, server, context
                    candidate = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    code = getattr(exc, "code", "connection_failed")
                    key = None
                    if code == "host_key_confirmation_required":
                        fingerprint, algorithm = getattr(exc, "fingerprint", None), getattr(exc, "algorithm", None)
                        if (isinstance(fingerprint, str) and re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", fingerprint)
                                and isinstance(algorithm, str) and re.fullmatch(r"[A-Za-z0-9@._+-]{1,80}", algorithm)):
                            key = {"fingerprint": fingerprint, "algorithm": algorithm}
                        else:
                            code = "connection_failed"
                    if code not in _MESSAGES:
                        code = "connection_failed"
                    error = DashboardError(code, 409 if key else 502, host_key=key)
                    self.error = error.public()
                    raise error from None
                finally:
                    if candidate is not None:
                        await _complete(self._close_candidate(candidate))
        finally:
            # No connection form survives a completed/cancelled auth worker.
            details.clear()
            body.clear()

    async def disconnect(self):
        async with self.operation():
            self.generation += 1
            await _complete(self._release())
            self.error = None

    async def _call_frame(self, tool, args, *, action=False):
        try:
            content = await self._server.call_tool(tool, args)
            image = next(item for item in content if isinstance(item, ImageContent))
            metadata = json.loads(next(item.text for item in content if isinstance(item, TextContent)))
            self._geometry = (metadata["desktop_width"], metadata["desktop_height"])
            self._lease_busy = False
            self.error = None
            if not action:
                self.observation_required = False
            return {"image_base64": image.data, "mime_type": image.mimeType, "metadata": metadata}
        except asyncio.CancelledError:
            self._geometry = None
            self.observation_required = True
            raise
        except Exception as exc:
            transport_error = _transport_error(exc)
            code = transport_error.code if transport_error else "transport_error"
            if code not in {"busy", "invalid_params", "agent_source_mismatch"}:
                code = "transport_error"
            self._lease_busy = code == "busy"
            if not action or transport_error is None or transport_error.input_state != "not_started":
                self._geometry = None
                self.observation_required = True
            error = DashboardError(code, 409 if code == "busy" else 502)
            self.error = error.public()
            raise error from None

    async def frame(self):
        async with self.operation():
            if self._stream_ws is not None:
                raise DashboardError("stream_active", 409)
            if self._server is None:
                raise DashboardError("not_connected", 409)
            return await self._call_frame("desktop_screenshot", {"max_width": self.capture_max_width})

    def _check_action_context(self, body):
        if body["generation"] != self.generation:
            raise DashboardError("stale_session", 409)
        if self._server is None:
            raise DashboardError("not_connected", 409)
        if self.observation_required or self._geometry is None:
            raise DashboardError("observation_required", 409)
        if self._geometry != (body["desktop_width"], body["desktop_height"]):
            raise DashboardError("stale_geometry", 409)

    async def action(self, body):
        tool, args = _action_details(body)
        async with self.operation():
            if self._stream_ws is not None:
                raise DashboardError("stream_active", 409)
            self._check_action_context(body)
            if tool == "desktop_click":
                # WayVNC suppresses motion at unchanged coordinates. Approach
                # the original target from an adjacent pixel without replaying input.
                x, y = args["x"], args["y"]
                positions = []
                if body["desktop_width"] > 1:
                    positions.append({"x": x - 1 if x else 1, "y": y})
                elif body["desktop_height"] > 1:
                    positions.append({"x": x, "y": y - 1 if y else 1})
                positions.append({"x": x, "y": y})
                for position in positions:
                    await self._call_frame("desktop_move", position, action=True)
                    try:
                        self._check_action_context(body)
                    except DashboardError:
                        # The preparation frame was not delivered to the browser.
                        self._geometry = None
                        self.observation_required = True
                        raise
            return await self._call_frame(tool, args, action=True)

    async def stream_ticket(self, body):
        if set(body) != {"generation"} or not _integer(body["generation"], 0, 2**53 - 1):
            _invalid()
        async with self.operation():
            if body["generation"] != self.generation:
                raise DashboardError("stale_session", 409)
            if self._server is None:
                raise DashboardError("not_connected", 409)
            if self._stream_ws is not None:
                raise DashboardError("stream_active", 409)
            secret = secrets.token_urlsafe(32)
            self._ticket = (secret, self.generation, time.monotonic() + 15.0)
            return secret

    async def claim_stream(self, websocket, secret):
        if self._operation_active or self._lock.locked() or self._closing:
            raise DashboardError("operation_pending", 409)
        async with self._lock:
            ticket = self._ticket
            if (ticket is None or not isinstance(secret, str)
                    or not hmac.compare_digest(ticket[0], secret)
                    or ticket[1] != self.generation or time.monotonic() >= ticket[2]):
                raise DashboardError("unauthorized", 401)
            if self._server is None or self._stream_ws is not None:
                raise DashboardError("stream_active", 409)
            self._ticket = None
            self._stream_ws = websocket
            self.touch()

    async def start_stream(self, websocket):
        async with self.operation():
            if self._stream_ws is not websocket or self._transport is None:
                raise DashboardError("stale_session", 409)
            candidate = None
            worker = asyncio.get_running_loop().run_in_executor(None, self.stream_factory, self._transport)
            try:
                try:
                    await asyncio.wait({worker})
                    candidate = worker.result()
                except asyncio.CancelledError:
                    await _join(worker)
                    if not worker.cancelled() and worker.exception() is None:
                        candidate = worker.result()
                    raise
                self._stream, candidate = candidate, None
                self.observation_required = True
                self._geometry = None
                self._stream_task = asyncio.create_task(self._relay_stream(websocket, self._stream),
                                                        name="pi-dashboard-stream")
                return self._stream_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "stream_failed")
                if code not in {"busy", "agent_source_mismatch"}:
                    code = "stream_failed"
                self.error = DashboardError(code, 502).public()
                raise DashboardError(code, 502) from None
            finally:
                if candidate is not None:
                    await _complete(self._close_candidate(candidate))

    async def _relay_stream(self, websocket, stream):
        async def io(function, *args):
            worker = asyncio.get_running_loop().run_in_executor(None, function, *args)
            try:
                await asyncio.wait({worker})
                return worker.result()
            except asyncio.CancelledError:
                await _join(worker)
                if not worker.cancelled():
                    worker.exception()
                raise

        async def outgoing():
            while True:
                data = await io(stream.recv)
                if data is None:
                    continue
                if not data:
                    return
                if not isinstance(data, bytes) or len(data) > 65_536:
                    raise DashboardError("stream_failed", 502)
                async with asyncio.timeout(10.0):
                    await websocket.send_bytes(data)

        async def incoming():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if not isinstance(data, bytes) or not 0 < len(data) <= 65_536:
                    raise DashboardError("invalid_request")
                delivery = message.get("_delivery")
                if delivery is not None and delivery.done():
                    continue
                try:
                    await io(stream.send, data)
                except BaseException:
                    if delivery is not None and not delivery.done():
                        delivery.set_exception(DashboardError("stream_failed", 502))
                    raise
                else:
                    if delivery is not None and not delivery.done():
                        delivery.set_result(None)
                # Only the authenticated visible-tab heartbeat renews this lease;
                # RFB traffic alone must not keep a hidden/lost browser alive.

        tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
        async def cleanup():
            try:
                await self._close_candidate(stream)
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    await _join(task)
                    if not task.cancelled():
                        task.exception()
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            await _complete(cleanup())

    async def _stop_stream(self):
        stream, websocket, task = self._stream, self._stream_ws, self._stream_task
        self._stream = self._stream_ws = self._stream_task = None
        if stream is not None:
            await _complete(self._close_candidate(stream))
        if websocket is not None:
            try:
                async with asyncio.timeout(2.0):
                    await websocket.close(code=1000)
            except (WebSocketDisconnect, RuntimeError, TimeoutError):
                pass
        if task is not None:
            task.cancel()
            await _join(task)
        self._geometry = None
        self.observation_required = True

    async def end_stream(self, websocket):
        async with self._lock:
            if self._stream_ws is websocket:
                await _complete(self._stop_stream())

    async def heartbeat(self):
        while True:
            self._activity_changed.clear()
            if self._server is None:
                await self._activity_changed.wait()
                continue
            remaining = self._last_activity + self.heartbeat_timeout - time.monotonic()
            if remaining > 0:
                try:
                    async with asyncio.timeout(remaining):
                        await self._activity_changed.wait()
                    continue
                except TimeoutError:
                    pass
            async with self._lock:
                if self._server is not None and time.monotonic() >= self._last_activity + self.heartbeat_timeout:
                    self.generation += 1
                    await _complete(self._release())

    async def shutdown(self):
        self._closing = True
        async with self._lock:
            await _complete(self._release())


async def _read_body(request: Request) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY_BYTES:
            data.clear()
            raise DashboardError("body_too_large", 413)
        data.extend(chunk)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _invalid()
            result[key] = value
        return result
    try:
        body = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: _invalid())
    except (ValueError, UnicodeError, RecursionError):
        _invalid()
    finally:
        data.clear()
    if not isinstance(body, dict):
        _invalid()
    return body


class LocalBoundary:
    """Validate raw headers before routes, including static files and error responses."""

    def __init__(self, app, *, origin, token, controller):
        self.app, self.origin, self.token, self.controller = app, origin, token, controller
        self.host = urlsplit(origin).netloc

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' "
                     + self.origin.replace("http://", "ws://") + "; img-src 'self' blob: data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'").encode()),
                ]
            await send(message)
        values = {}
        for key, value in scope["headers"]:
            values.setdefault(key.lower(), []).append(value)
        host, origins = values.get(b"host", []), values.get(b"origin", [])
        if scope["type"] == "websocket":
            if (host != [self.host.encode()] or origins != [self.origin.encode()]
                    or scope["path"] != "/api/stream"
                    or len(values.get(b"sec-websocket-protocol", [])) != 1):
                return await send({"type": "websocket.close", "code": 1008})
            return await self.app(scope, receive, send)
        error = None
        if host != [self.host.encode()] or (origins and origins != [self.origin.encode()]):
            error = DashboardError("forbidden", 403)
        elif scope["path"].startswith("/api/"):
            auth = values.get(b"authorization", [])
            if len(auth) != 1 or not hmac.compare_digest(auth[0], b"Bearer " + self.token.encode()):
                error = DashboardError("unauthorized", 401)
            elif scope["method"] == "POST" and origins != [self.origin.encode()]:
                error = DashboardError("forbidden", 403)
            else:
                if scope["path"] not in {"/api/stream-open", "/api/stream-input", "/api/stream-close"}:
                    self.controller.touch()
        if error:
            return await JSONResponse({"error": error.public()}, status_code=error.status)(scope, receive, secured_send)
        await self.app(scope, receive, secured_send)


def create_dashboard(*, token: str, default_host: str = "pi-desktop", origin: str,
                     connection_factory=None, capture_max_width: int = 960, stream_factory=None) -> Starlette:
    """Build an app for a pre-bound 127.0.0.1 socket and its exact HTTP origin."""
    parsed = urlsplit(origin)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username is not None
            or parsed.password is not None or not parsed.port or parsed.path or parsed.query or parsed.fragment
            or origin != f"http://127.0.0.1:{parsed.port}"):
        raise ValueError("origin must be the exact http://127.0.0.1:<bound-port> origin")
    if not isinstance(token, str) or not token or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("token must be a nonempty ASCII bearer token")
    if not _integer(capture_max_width, 1, 65535):
        raise ValueError("capture_max_width must be an integer from 1 to 65535")
    validate_host(default_host)
    if connection_factory is None:
        from .gui_transport import connect_gui
        connection_factory = connect_gui
    if stream_factory is None:
        from .stream_transport import open_desktop_stream
        stream_factory = open_desktop_stream
    controller = DashboardController(default_host, connection_factory, capture_max_width, stream_factory)

    @asynccontextmanager
    async def lifespan(app):
        timer = asyncio.create_task(controller.heartbeat(), name="pi-dashboard-heartbeat")
        try:
            yield
        finally:
            timer.cancel()
            await _join(timer)
            if not timer.cancelled():
                timer.result()
            await _complete(controller.shutdown())

    async def api(request):
        body = None
        try:
            path = request.url.path
            if path == "/api/state":
                return JSONResponse(controller.snapshot())
            if request.method == "POST":
                body = await _read_body(request)
            frame = None
            ticket = None
            if path == "/api/connect":
                await controller.connect(body)
            elif path == "/api/disconnect":
                if body:
                    _invalid()
                await controller.disconnect()
            elif path == "/api/frame":
                frame = await controller.frame()
            elif path == "/api/action":
                frame = await controller.action(body)
            elif path == "/api/stream-ticket":
                ticket = await controller.stream_ticket(body)
            result = {"state": controller.snapshot()}
            if frame is not None:
                result["frame"] = frame
            if ticket is not None:
                result["ticket"] = ticket
            return JSONResponse(result)
        except DashboardError as exc:
            result = {"state": controller.snapshot(), "error": exc.public()}
            if exc.host_key:
                result["host_key"] = exc.host_key
            return JSONResponse(result, status_code=exc.status)
        except Exception:
            error = DashboardError("internal_error", 500)
            return JSONResponse({"state": controller.snapshot(), "error": error.public()}, status_code=500)
        finally:
            if body is not None:
                body.clear()

    async def static(request):
        filename = "index.html" if request.url.path == "/" else request.url.path[1:]
        return FileResponse(Path(__file__).with_name("web") / filename)

    async def stream_endpoint(websocket):
        protocols = websocket.scope.get("subprotocols", [])
        if (len(protocols) != 2 or protocols[0] != "binary"
                or not re.fullmatch(r"pi-ticket\.[A-Za-z0-9_-]{43}", protocols[1])):
            await websocket.close(code=1008)
            return
        try:
            await controller.claim_stream(websocket, protocols[1][10:])
        except DashboardError:
            await websocket.close(code=1008)
            return
        try:
            await websocket.accept(subprotocol="binary")
            task = await controller.start_stream(websocket)
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except (WebSocketDisconnect, DashboardError, RuntimeError):
            pass
        except Exception:
            if controller._stream_ws is websocket:
                controller.error = DashboardError("stream_failed", 502).public()
        finally:
            await _complete(controller.end_stream(websocket))

    app = Starlette(routes=[Route("/", static), Route("/app.js", static), Route("/style.css", static),
                           Mount("/vendor/novnc", app=StaticFiles(directory=Path(__file__).with_name("web") / "vendor/novnc")),
                           WebSocketRoute("/api/stream", stream_endpoint),
                           Route("/api/state", api), Route("/api/frame", api),
                           Route("/api/connect", api, methods=["POST"]),
                           Route("/api/action", api, methods=["POST"]),
                           Route("/api/stream-ticket", api, methods=["POST"]),
                           Route("/api/disconnect", api, methods=["POST"])], lifespan=lifespan)
    app.add_middleware(LocalBoundary, origin=origin, token=token, controller=controller)
    from .http_stream import install_http_stream_routes
    install_http_stream_routes(app, controller)
    app.state.controller = controller
    return app
