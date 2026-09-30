"""Exercise authenticated HTTP RFB streams through an actual ASGI exchange."""

import asyncio
import gc
import json
import queue
import threading
import time
import unittest
from unittest.mock import patch

from test_dashboard import DashboardTransport, ORIGIN, TOKEN
from pi_desktop_bridge.dashboard import DashboardError, create_dashboard
from pi_desktop_bridge.http_stream import HTTPStreamChannel
from pi_desktop_bridge.stream_transport import StreamError


BANNER = b"RFB 003.008\n"
BASE_HEADERS = [(b"host", b"127.0.0.1:8765"), (b"origin", ORIGIN.encode()),
                (b"authorization", ("Bearer " + TOKEN).encode())]


class ByteStream:
    width, height, max_fps = 1920, 1080, 1000

    def __init__(self, *, block_send=False):
        self.output = queue.Queue(maxsize=2)
        self.output.put(BANNER)
        self.closed = threading.Event()
        self.send_entered, self.send_done = threading.Event(), threading.Event()
        self.send_release = threading.Event()
        if not block_send:
            self.send_release.set()
        self.attempted, self.sent = [], []

    def recv(self):
        if self.closed.is_set():
            return b""
        try:
            value = self.output.get(timeout=0.05)
        except queue.Empty:
            return None
        if isinstance(value, Exception):
            raise value
        return value

    def send(self, data):
        self.attempted.append(data)
        self.send_entered.set()
        try:
            deadline = time.monotonic() + 5
            while not self.send_release.is_set():
                if self.closed.wait(0.005):
                    raise StreamError()
                if time.monotonic() >= deadline:
                    raise AssertionError("test send gate was never released")
            if self.closed.is_set():
                raise StreamError()
            self.sent.append(data)
        finally:
            self.send_done.set()

    def close(self):
        self.closed.set()


class ASGIExchange:
    """Keep a response live while observing individual ASGI chunks and aborts."""

    def __init__(self, app, path, *, body=b"{}", headers=None, chunks=None, fail_send=None,
                 block_send=None):
        self.incoming = asyncio.Queue()
        self.response_started = asyncio.Event()
        self.body_messages = asyncio.Queue()
        self.messages = []
        self.receive_calls = 0
        self.status = None
        self.headers = {}
        self.fail_send = fail_send
        self.block_send = block_send
        self.send_entered, self.send_exited = asyncio.Event(), asyncio.Event()
        self.scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": list(BASE_HEADERS if headers is None else headers),
            "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 32123),
        }
        if chunks is None:
            self.incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})
        else:
            for message in chunks:
                self.incoming.put_nowait(message)
        self.task = asyncio.create_task(app(self.scope, self.receive, self.send))

    async def receive(self):
        self.receive_calls += 1
        return await self.incoming.get()

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = dict(message["headers"])
            self.response_started.set()
        if self.fail_send == message["type"]:
            raise OSError("disposable response connection closed")
        if self.block_send == message["type"]:
            self.send_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.send_exited.set()
        self.messages.append(message)
        if message["type"] == "http.response.body":
            self.body_messages.put_nowait(message)

    async def start(self):
        await asyncio.wait_for(self.response_started.wait(), 3)
        return self.status

    async def next_body(self):
        return await asyncio.wait_for(self.body_messages.get(), 3)

    async def finish(self):
        await asyncio.wait_for(asyncio.shield(self.task), 3)
        return self.status

    def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})

    def response_body(self):
        return b"".join(message.get("body", b"") for message in self.messages
                        if message["type"] == "http.response.body")


class HTTPStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = DashboardTransport()
        self.streams, self.exchanges = [], []
        self.block_send = False
        def factory(_transport):
            stream = ByteStream(block_send=self.block_send)
            self.streams.append(stream)
            return stream
        self.app = create_dashboard(token=TOKEN, origin=ORIGIN,
                                    connection_factory=lambda _: self.transport, stream_factory=factory)
        self.controller = self.app.state.controller
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        await self.controller.connect({"mode": "existing", "host": "first"})

    async def asyncTearDown(self):
        for exchange in self.exchanges:
            if not exchange.task.done():
                exchange.disconnect()
        for stream in self.streams:
            stream.close()
        await self.controller.shutdown()
        for exchange in self.exchanges:
            if not exchange.task.done():
                exchange.task.cancel()
        await asyncio.gather(*(exchange.task for exchange in self.exchanges), return_exceptions=True)
        await self.lifespan.__aexit__(None, None, None)

    def exchange(self, path, *, value=None, body=b"{}", **kwargs):
        if value is not None:
            body = json.dumps(value).encode()
        result = ASGIExchange(self.app, path, body=body, **kwargs)
        self.exchanges.append(result)
        return result

    async def request(self, path, **kwargs):
        result = self.exchange(path, **kwargs)
        await result.finish()
        return result

    async def open_stream(self):
        generation = self.controller.generation
        ticket = await self.controller.stream_ticket({"generation": generation})
        response = self.exchange("/api/stream-open", value={"generation": generation, "ticket": ticket})
        self.assertEqual(await response.start(), 200)
        first = await response.next_body()
        self.assertEqual(first["body"], BANNER)
        self.assertTrue(first["more_body"])
        return response, response.headers[b"x-pi-stream-id"], ticket

    def input(self, identity, data, *, headers=None, **kwargs):
        if headers is None:
            headers = BASE_HEADERS + [(b"x-pi-stream-id", identity), (b"content-type", b"application/octet-stream")]
        return self.exchange("/api/stream-input", body=data, headers=headers, **kwargs)

    async def close_stream(self, identity, generation=None):
        return await self.request("/api/stream-close", value={
            "generation": self.controller.generation if generation is None else generation,
            "stream_id": identity.decode()})

    async def test_auth_host_origin_reject_before_reading_body_or_starting_stream(self):
        cases = [
            ([item for item in BASE_HEADERS if item[0] != b"authorization"], 401),
            (BASE_HEADERS + [(b"authorization", b"Bearer duplicate")], 401),
            ([item for item in BASE_HEADERS if item[0] != b"origin"], 403),
            ([(key, b"https://foreign.invalid" if key == b"origin" else value) for key, value in BASE_HEADERS], 403),
            ([(key, b"foreign.invalid" if key == b"host" else value) for key, value in BASE_HEADERS], 403),
            (BASE_HEADERS + [(b"host", b"127.0.0.1:8765")], 403),
        ]
        for path in ("/api/stream-open", "/api/stream-input", "/api/stream-close"):
            for headers, status in cases:
                with self.subTest(path=path, status=status, headers=headers):
                    response = await self.request(path, headers=headers, body=b"not read")
                    self.assertEqual(response.status, status)
                    self.assertEqual(response.receive_calls, 0)
        self.assertEqual(self.streams, [])

    async def test_open_schema_stale_generation_and_single_use_ticket(self):
        ticket = await self.controller.stream_ticket({"generation": 1})
        for body, status in (({"generation": True, "ticket": ticket}, 400),
                             ({"generation": 0, "ticket": ticket}, 409),
                             ({"generation": 1, "ticket": "bad"}, 400),
                             ({"generation": 1, "ticket": ticket, "host": "other"}, 400)):
            response = await self.request("/api/stream-open", value=body)
            self.assertEqual(response.status, status)
            self.assertEqual(self.streams, [])
        opened = self.exchange("/api/stream-open", value={"generation": 1, "ticket": ticket})
        self.assertEqual(await opened.start(), 200)
        self.assertEqual((await opened.next_body())["body"], BANNER)
        identity = opened.headers[b"x-pi-stream-id"]
        self.assertEqual(len(identity), 43)
        self.assertEqual(opened.headers[b"content-type"], b"application/octet-stream")
        self.assertEqual(opened.headers[b"cache-control"], b"no-store")
        self.assertEqual((await self.close_stream(identity)).status, 204)
        await opened.finish()
        replay = await self.request("/api/stream-open", value={"generation": 1, "ticket": ticket})
        self.assertEqual(replay.status, 401)
        self.assertEqual(len(self.streams), 1)

    async def test_input_requires_one_id_binary_type_and_exact_64k_limit(self):
        opened, identity, _ = await self.open_stream()
        base = BASE_HEADERS + [(b"x-pi-stream-id", identity)]
        cases = [
            (base, b"x", 400),
            (base + [(b"content-type", b"text/plain")], b"x", 400),
            (base + [(b"content-type", b"application/octet-stream")] * 2, b"x", 400),
            (base + [(b"x-pi-stream-id", identity), (b"content-type", b"application/octet-stream")], b"x", 400),
            (base + [(b"content-type", b"application/octet-stream")], b"", 400),
            (base + [(b"content-type", b"application/octet-stream")], b"x" * 65537, 413),
        ]
        for headers, body, status in cases:
            with self.subTest(size=len(body), headers=headers):
                response = self.input(identity, body, headers=headers)
                await response.finish()
                self.assertEqual(response.status, status)
        chunked = self.input(identity, b"", chunks=[
            {"type": "http.request", "body": b"x" * 32768, "more_body": True},
            {"type": "http.request", "body": b"x" * 32769, "more_body": False},
        ])
        self.assertEqual(await chunked.finish(), 413)
        self.assertEqual(self.streams[0].attempted, [])
        payload = bytes(range(256)) * 256
        response = self.input(identity, payload)
        self.assertEqual(await response.finish(), 204)
        self.assertEqual(self.streams[0].sent, [payload])
        await self.close_stream(identity)
        await opened.finish()

    async def test_invalid_ids_and_close_schema_never_renew_heartbeat_or_close_current_stream(self):
        opened, identity, _ = await self.open_stream()
        activity = self.controller._last_activity
        for bad_identity, status in ((b"bad", 400), (b"!" * 43, 400), (b"0" * 43, 409)):
            response = self.input(bad_identity, b"unread")
            self.assertEqual(await response.finish(), status)
            self.assertEqual(response.receive_calls, 0)
        valid = {"generation": self.controller.generation, "stream_id": identity.decode()}
        cases = [({**valid, "generation": True}, 400), ({**valid, "generation": 0}, 409),
                 ({**valid, "stream_id": "bad"}, 400), ({**valid, "stream_id": "0" * 43}, 409),
                 ({**valid, "extra": "unused"}, 400)]
        for body, status in cases:
            response = await self.request("/api/stream-close", value=body)
            self.assertEqual(response.status, status)
        self.assertEqual(self.controller._last_activity, activity)
        self.assertFalse(self.streams[0].closed.is_set())
        self.assertEqual(self.streams[0].attempted, [])
        await self.close_stream(identity)
        await opened.finish()

    async def test_slow_body_readers_are_bounded_before_allocating_another_upload(self):
        opened, identity, _ = await self.open_stream()
        uploads = [self.input(identity, b"", chunks=[
            {"type": "http.request", "body": bytes([index]), "more_body": True},
        ]) for index in range(3)]
        channel = self.controller._stream_ws
        async with asyncio.timeout(2):
            while any(upload.receive_calls < 2 for upload in uploads):
                await asyncio.sleep(0.005)
        self.assertEqual(channel.input_requests, 3)
        self.assertEqual(self.streams[0].attempted, [])
        overflow = self.input(identity, b"unread")
        self.assertEqual(await overflow.finish(), 409)
        self.assertEqual(overflow.receive_calls, 0)
        for upload in uploads:
            upload.incoming.put_nowait({"type": "http.request", "body": b"end", "more_body": False})
            self.assertEqual(await upload.finish(), 204)
        self.assertEqual(channel.input_requests, 0)
        reused = self.input(identity, b"capacity-reused")
        self.assertEqual(await reused.finish(), 204)
        self.assertEqual(self.streams[0].sent, [bytes([index]) + b"end" for index in range(3)]
                         + [b"capacity-reused"])
        await self.close_stream(identity)
        await opened.finish()

    async def test_ack_waits_for_actual_send_without_replaying_input(self):
        self.block_send = True
        opened, identity, _ = await self.open_stream()
        stream = self.streams[0]
        response = self.input(identity, b"one-event")
        self.assertTrue(await asyncio.to_thread(stream.send_entered.wait, 2))
        self.assertIsNone(response.status)
        self.assertFalse(response.task.done())
        self.assertEqual(stream.sent, [])
        stream.send_release.set()
        self.assertEqual(await response.finish(), 204)
        self.assertTrue(stream.send_done.is_set())
        self.assertEqual(stream.attempted, [b"one-event"])
        self.assertEqual(stream.sent, [b"one-event"])
        await self.close_stream(identity)
        await opened.finish()

    async def test_bounded_input_queue_rejects_overflow_and_drops_unsent_events_on_close(self):
        self.block_send = True
        opened, identity, _ = await self.open_stream()
        stream = self.streams[0]
        first = self.input(identity, b"first")
        self.assertTrue(await asyncio.to_thread(stream.send_entered.wait, 2))
        second, third = self.input(identity, b"second"), self.input(identity, b"third")
        channel = self.controller._stream_ws
        async with asyncio.timeout(2):
            while channel.input.qsize() != 2:
                await asyncio.sleep(0.005)
        overflow = self.input(identity, b"overflow")
        self.assertEqual(await overflow.finish(), 409)
        self.assertEqual(channel.input.qsize(), 2)
        self.assertEqual((await self.close_stream(identity)).status, 204)
        for response in (first, second, third):
            self.assertNotEqual(await response.finish(), 204)
        await opened.finish()
        self.assertTrue(stream.send_done.is_set())
        self.assertEqual(stream.attempted, [b"first"])
        self.assertEqual(stream.sent, [])
        self.assertIsNone(self.controller._stream)

    async def test_output_queue_applies_backpressure_at_two_chunks(self):
        channel = HTTPStreamChannel(1)
        chunk = b"x" * 65536
        await channel.send_bytes(chunk)
        await channel.send_bytes(chunk)
        blocked = asyncio.create_task(channel.send_bytes(chunk))
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        self.assertEqual(channel.output.qsize(), 2)
        self.assertEqual(await channel.output.get(), chunk)
        await asyncio.wait_for(blocked, 1)
        self.assertEqual(channel.output.qsize(), 2)
        channel.finish()
        self.assertIsNone(await channel.output.get())

    async def test_stale_input_and_close_cannot_retarget_after_profile_switch_during_upload(self):
        opened, identity, _ = await self.open_stream()
        old_stream = self.streams[0]
        upload = self.input(identity, b"", chunks=[{"type": "http.request", "body": b"old-", "more_body": True}])
        async with asyncio.timeout(2):
            while upload.receive_calls < 2:
                await asyncio.sleep(0.005)
        await self.controller.connect({"mode": "existing", "host": "second"})
        await opened.finish()
        replacement, new_identity, _ = await self.open_stream()
        upload.incoming.put_nowait({"type": "http.request", "body": b"input", "more_body": False})
        self.assertEqual(await upload.finish(), 409)
        self.assertEqual((await self.close_stream(identity, generation=1)).status, 409)
        self.assertIsNotNone(self.controller._stream)
        self.assertEqual(old_stream.attempted, [])
        self.assertEqual(self.streams[1].attempted, [])
        stale = self.input(identity, b"unread")
        self.assertEqual(await stale.finish(), 409)
        self.assertEqual(stale.receive_calls, 0)
        await self.close_stream(new_identity)
        await replacement.finish()

    async def test_task_cancel_of_inflight_input_closes_and_joins_stream(self):
        self.block_send = True
        opened, identity, _ = await self.open_stream()
        stream = self.streams[0]
        response = self.input(identity, b"cancelled")
        self.assertTrue(await asyncio.to_thread(stream.send_entered.wait, 2))
        response.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await response.finish()
        await opened.finish()
        self.assertTrue(stream.closed.is_set())
        self.assertTrue(stream.send_done.is_set())
        self.assertEqual(stream.sent, [])
        self.assertIsNone(self.controller._stream)

    async def test_actual_input_disconnect_closes_channel_and_discards_queued_delivery(self):
        self.block_send = True
        opened, identity, _ = await self.open_stream()
        stream = self.streams[0]
        first = self.input(identity, b"in-flight")
        self.assertTrue(await asyncio.to_thread(stream.send_entered.wait, 2))
        cancelled = self.input(identity, b"queued-cancelled")
        async with asyncio.timeout(2):
            while self.controller._stream_ws.input.qsize() != 1:
                await asyncio.sleep(0.005)
        cancelled.disconnect()
        await cancelled.finish()
        await first.finish()
        await opened.finish()
        self.assertTrue(stream.closed.is_set())
        self.assertTrue(stream.send_done.is_set())
        self.assertEqual(stream.attempted, [b"in-flight"])
        self.assertEqual(stream.sent, [])
        self.assertIsNone(self.controller._stream)

    async def test_delivery_failure_racing_input_abort_is_consumed_and_joins_send(self):
        loop = asyncio.get_running_loop()
        unhandled = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            for abort in ("disconnect", "cancel"):
                with self.subTest(abort=abort):
                    self.block_send = True
                    opened, identity, _ = await self.open_stream()
                    stream = self.streams[-1]
                    response = self.input(identity, b"failing-input")
                    self.assertTrue(await asyncio.to_thread(stream.send_entered.wait, 2))
                    channel = self.controller._stream_ws
                    async with asyncio.timeout(2):
                        while response.receive_calls < 2:
                            await asyncio.sleep(0.005)
                    delivery, = channel.deliveries
                    # Complete the delivery and abort in one loop turn, before
                    # the waiting request can observe either result.
                    with (patch.object(delivery, "exception", wraps=delivery.exception) as exceptions,
                          patch.object(delivery, "result", wraps=delivery.result) as results):
                        delivery.set_exception(DashboardError("stream_failed", 502))
                        if abort == "cancel":
                            response.task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await response.finish()
                        else:
                            response.disconnect()
                            self.assertNotEqual(await response.finish(), 204)
                        # Tracebacks can retain a failed Future beyond GC, so
                        # also require its error to be observed before return.
                        self.assertGreater(exceptions.call_count + results.call_count, 0)
                    await opened.finish()
                    self.assertTrue(stream.closed.is_set())
                    self.assertTrue(stream.send_done.is_set())
                    self.assertEqual(stream.sent, [])
                    self.assertIsNone(self.controller._stream)
                    del delivery
                    await asyncio.sleep(0)
                    gc.collect()
                    self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(original_handler)

    async def test_response_abort_and_send_failures_release_stream_before_returning(self):
        opened, _identity, _ = await self.open_stream()
        stream = self.streams[-1]
        opened.disconnect()
        await opened.finish()
        self.assertTrue(stream.closed.is_set())
        self.assertIsNone(self.controller._stream)
        for failure in ("http.response.start", "http.response.body"):
            with self.subTest(failure=failure):
                ticket = await self.controller.stream_ticket({"generation": 1})
                response = self.exchange("/api/stream-open", value={"generation": 1, "ticket": ticket},
                                         fail_send=failure)
                await response.finish()
                self.assertTrue(self.streams[-1].closed.is_set())
                self.assertIsNone(self.controller._stream)
                self.assertIsNone(self.controller._stream_ws)

    async def test_blocked_asgi_body_send_times_out_without_browser_disconnect(self):
        ticket = await self.controller.stream_ticket({"generation": 1})
        existing_tasks = asyncio.all_tasks()
        with patch("pi_desktop_bridge.http_stream.RESPONSE_SEND_TIMEOUT", 0.05):
            response = self.exchange("/api/stream-open", value={"generation": 1, "ticket": ticket},
                                     block_send="http.response.body")
            self.assertEqual(await response.start(), 200)
            await asyncio.wait_for(response.send_entered.wait(), 2)
            await response.finish()
        # No http.disconnect was supplied and the send gate never opened.
        self.assertTrue(response.send_exited.is_set())
        self.assertEqual(response.response_body(), b"")
        self.assertTrue(self.streams[-1].closed.is_set())
        self.assertIsNone(self.controller._stream)
        self.assertIsNone(self.controller._stream_ws)
        await asyncio.sleep(0)
        self.assertEqual(asyncio.all_tasks() - existing_tasks, set())

    async def test_relay_eof_or_failure_ends_response_and_frees_lease(self):
        for terminal in (b"", ValueError("private backend failure")):
            with self.subTest(terminal=type(terminal).__name__):
                opened, _identity, _ = await self.open_stream()
                stream = self.streams[-1]
                stream.output.put_nowait(terminal)
                await opened.finish()
                self.assertTrue(stream.closed.is_set())
                self.assertIsNone(self.controller._stream)
                self.assertIsNone(self.controller._stream_ws)
                self.assertEqual(opened.response_body(), BANNER)
                self.assertFalse(opened.messages[-1].get("more_body", False))

    async def test_http_raw_traffic_cannot_renew_heartbeat(self):
        opened, identity, _ = await self.open_stream()
        stream = self.streams[0]
        self.controller.heartbeat_timeout = 0.15
        self.controller.touch()
        activity = self.controller._last_activity
        acknowledged = 0
        async with asyncio.timeout(3):
            while not opened.task.done():
                response = self.input(identity, b"raw-pointer")
                status = await response.finish()
                if status == 204:
                    acknowledged += 1
                    stream.output.put_nowait(b"raw-frame")
                else:
                    self.assertEqual(status, 409)
                await asyncio.sleep(0.01)
            await opened.finish()
        self.assertGreater(acknowledged, 0)
        self.assertEqual(self.controller._last_activity, activity)
        self.assertTrue(self.transport.closed)
        self.assertTrue(stream.closed.is_set())
        self.assertFalse(self.controller.snapshot()["connected"])


if __name__ == "__main__":
    unittest.main()
