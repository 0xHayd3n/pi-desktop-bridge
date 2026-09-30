"""Authenticated streaming HTTP adapter for browser RFB channels."""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets

from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .dashboard import DashboardError, _complete, _integer, _join, _read_body

RESPONSE_SEND_TIMEOUT = 10.0


class HTTPStreamChannel:
    """A bounded duplex channel using an HTTP response and acknowledged POSTs."""

    def __init__(self, generation):
        self.generation = generation
        self.identity = secrets.token_urlsafe(32)
        self.output = asyncio.Queue(maxsize=2)
        self.input = asyncio.Queue(maxsize=2)
        self.closed = False
        self.deliveries = set()
        self.input_requests = 0

    async def send_bytes(self, data):
        if self.closed:
            raise DashboardError("stream_failed", 409)
        await self.output.put(data)

    async def receive(self):
        return await self.input.get()

    def submit(self, data):
        if self.closed:
            raise DashboardError("stale_session", 409)
        delivery = asyncio.get_running_loop().create_future()
        try:
            self.input.put_nowait({"type": "websocket.receive", "bytes": data,
                                   "_delivery": delivery})
        except asyncio.QueueFull:
            raise DashboardError("operation_pending", 409) from None
        self.deliveries.add(delivery)
        delivery.add_done_callback(self.deliveries.discard)
        return delivery

    async def close(self, code=1000):
        self.finish()

    def finish(self):
        if self.closed:
            return
        self.closed = True
        for delivery in tuple(self.deliveries):
            if not delivery.done():
                delivery.set_exception(DashboardError("stream_failed", 409))
        # Drop unsent input on disconnect; it must never be delivered later.
        for queue in (self.output, self.input):
            while not queue.empty():
                queue.get_nowait()
        self.output.put_nowait(None)
        self.input.put_nowait({"type": "websocket.disconnect"})


def _header(request, name):
    values = [value for key, value in request.scope["headers"] if key.lower() == name]
    if len(values) != 1:
        raise DashboardError("invalid_request")
    return values[0]


def _cancel_delivery(delivery):
    if delivery is not None:
        delivery.cancel()
        if not delivery.cancelled():
            delivery.exception()


async def _binary_body(request):
    if _header(request, b"content-type") != b"application/octet-stream":
        raise DashboardError("invalid_request")
    data = bytearray()
    try:
        async with asyncio.timeout(3.0):
            async for chunk in request.stream():
                if len(data) + len(chunk) > 65_536:
                    raise DashboardError("body_too_large", 413)
                data.extend(chunk)
    except TimeoutError:
        raise DashboardError("invalid_request", 408) from None
    if not data:
        raise DashboardError("invalid_request")
    return bytes(data)


def install_http_stream_routes(app, controller):
    def current(identity):
        channel = controller._stream_ws
        if (not isinstance(channel, HTTPStreamChannel) or channel.closed
                or controller.generation != channel.generation
                or not hmac.compare_digest(channel.identity.encode(), identity)):
            raise DashboardError("stale_session", 409)
        return channel

    def error_response(error):
        return JSONResponse({"error": error.public()}, status_code=error.status)

    async def open_stream(request):
        channel = None
        try:
            body = await _read_body(request)
            if (set(body) != {"generation", "ticket"}
                    or not _integer(body.get("generation"), 0, 2**53 - 1)
                    or not isinstance(body.get("ticket"), str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{43}", body["ticket"]) is None):
                raise DashboardError("invalid_request")
            if body["generation"] != controller.generation:
                raise DashboardError("stale_session", 409)
            channel = HTTPStreamChannel(body["generation"])
            await controller.claim_stream(channel, body["ticket"])
            relay = await controller.start_stream(channel)
            def completed(task):
                if not task.cancelled():
                    task.exception()
                channel.finish()
            relay.add_done_callback(completed)
        except BaseException as exc:
            if channel is not None:
                await _complete(controller.end_stream(channel))
            if isinstance(exc, asyncio.CancelledError):
                raise
            return error_response(exc if isinstance(exc, DashboardError)
                                  else DashboardError("stream_failed", 502))

        async def frames():
            try:
                while True:
                    data = await channel.output.get()
                    if data is None:
                        return
                    yield data
            finally:
                await _complete(controller.end_stream(channel))

        class OwnedResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                async def bounded_send(message):
                    async with asyncio.timeout(RESPONSE_SEND_TIMEOUT):
                        await send(message)

                async def disconnected():
                    while (await receive())["type"] != "http.disconnect":
                        pass

                tasks = [asyncio.create_task(self.stream_response(bounded_send)),
                         asyncio.create_task(disconnected())]
                async def cleanup():
                    for task in tasks:
                        task.cancel()
                    for task in tasks:
                        await _join(task)
                        if not task.cancelled():
                            task.exception()
                    await controller.end_stream(channel)
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        try:
                            task.result()
                        except (OSError, TimeoutError):
                            pass
                finally:
                    # Also covers a disconnect before the iterator starts, or
                    # a response-send failure outside the generator's body.
                    await _complete(cleanup())

        return OwnedResponse(frames(), media_type="application/octet-stream",
                             headers={"X-Pi-Stream-Id": channel.identity})

    async def input_stream(request):
        channel = None
        reserved = None
        delivery = None
        try:
            identity = _header(request, b"x-pi-stream-id")
            if re.fullmatch(rb"[A-Za-z0-9_-]{43}", identity) is None:
                raise DashboardError("invalid_request")
            # Reject stale input before reading its body, then check again before
            # enqueueing so a profile change during upload cannot retarget it.
            channel = current(identity)
            if channel.input_requests >= 3:
                raise DashboardError("operation_pending", 409)
            channel.input_requests += 1
            reserved = channel
            data = await _binary_body(request)
            channel = current(identity)
            delivery = channel.submit(data)
            disconnected = asyncio.create_task(request.receive())
            try:
                done, _ = await asyncio.wait({delivery, disconnected}, return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    _cancel_delivery(delivery)
                    await _complete(controller.end_stream(channel))
                    raise DashboardError("stream_failed", 409)
                delivery.result()
            except asyncio.CancelledError:
                _cancel_delivery(delivery)
                raise
            finally:
                disconnected.cancel()
                await _join(disconnected)
                if not disconnected.cancelled():
                    disconnected.exception()
            return Response(status_code=204)
        except asyncio.CancelledError:
            _cancel_delivery(delivery)
            if channel is not None:
                await _complete(controller.end_stream(channel))
            raise
        except DashboardError as exc:
            return error_response(exc)
        except Exception:
            return error_response(DashboardError("stream_failed", 502))
        finally:
            if reserved is not None:
                reserved.input_requests -= 1

    async def close_stream(request):
        try:
            body = await _read_body(request)
            if (set(body) != {"generation", "stream_id"}
                    or not _integer(body.get("generation"), 0, 2**53 - 1)
                    or not isinstance(body.get("stream_id"), str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{43}", body["stream_id"]) is None):
                raise DashboardError("invalid_request")
            channel = current(body["stream_id"].encode())
            if body["generation"] != channel.generation:
                raise DashboardError("stale_session", 409)
            await _complete(controller.end_stream(channel))
            return Response(status_code=204)
        except DashboardError as exc:
            return error_response(exc)

    app.routes.extend([Route("/api/stream-open", open_stream, methods=["POST"]),
                       Route("/api/stream-input", input_stream, methods=["POST"]),
                       Route("/api/stream-close", close_stream, methods=["POST"])])
