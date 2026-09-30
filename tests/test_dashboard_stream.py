"""Exercise stream authorization, byte forwarding and exclusive lifecycle."""
import asyncio
import gc
import queue
import threading
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from test_dashboard import DashboardTransport, HEADERS, ORIGIN, TOKEN, click_body, connect
from pi_desktop_bridge.dashboard import DashboardError, create_dashboard
from pi_desktop_bridge.stream_transport import StreamError


class FakeStream:
    width, height, max_fps = 1920, 1080, 1000

    def __init__(self):
        self.received = []
        self.closed = threading.Event()
        self.data = queue.Queue(maxsize=2)
        self.data.put(b"RFB 003.008\n")

    def recv(self):
        if self.closed.is_set():
            return b""
        try:
            return self.data.get(timeout=0.05)
        except queue.Empty:
            return None

    def send(self, data):
        self.received.append(data)
        self.data.put(data, timeout=1)

    def close(self):
        self.closed.set()


class MemoryWebSocket:
    """The endpoint's async WebSocket boundary, with observable close and bytes."""

    def __init__(self, ticket):
        self.scope = {"subprotocols": ["binary", "pi-ticket." + ticket]}
        self.messages = asyncio.Queue()
        self.frames = []
        self.accepted = asyncio.Event()
        self.closed = asyncio.Event()

    async def accept(self, subprotocol):
        self.subprotocol = subprotocol
        self.accepted.set()

    async def receive(self):
        return await self.messages.get()

    async def send_bytes(self, data):
        if self.closed.is_set():
            raise RuntimeError("socket closed")
        self.frames.append(data)

    async def close(self, code=1000):
        if not self.closed.is_set():
            self.closed.set()
            self.messages.put_nowait({"type": "websocket.disconnect", "code": code})


class BlockedStream(FakeStream):
    """Both IO workers remain owned until closing the stream releases them."""

    def __init__(self, events):
        super().__init__()
        self.events = events
        self.recv_entered, self.recv_done = threading.Event(), threading.Event()
        self.send_entered, self.send_done = threading.Event(), threading.Event()

    def recv(self):
        self.recv_entered.set()
        try:
            if not self.closed.wait(5):
                raise AssertionError("blocked receive was not released by close")
            return b""
        finally:
            self.recv_done.set()

    def send(self, data):
        self.received.append(data)
        self.send_entered.set()
        try:
            if not self.closed.wait(5):
                raise AssertionError("blocked send was not released by close")
            raise StreamError()
        finally:
            self.send_done.set()

    def close(self):
        if not self.closed.is_set():
            self.events.append("stream.close")
        self.closed.set()


class StreamBoundaryTests(unittest.TestCase):
    def make_app(self):
        transport, streams = DashboardTransport(), []
        def factory(bridge):
            self.assertIs(bridge, transport)
            stream = FakeStream()
            streams.append(stream)
            return stream
        app = create_dashboard(token=TOKEN, origin=ORIGIN, connection_factory=lambda _: transport,
                               stream_factory=factory)
        return app, transport, streams

    def ticket(self, client, generation=1):
        response = client.post("/api/stream-ticket", headers=HEADERS, json={"generation": generation})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["ticket"]

    def websocket(self, client, ticket, headers=None):
        return client.websocket_connect(ORIGIN.replace("http", "ws") + "/api/stream",
            subprotocols=["binary", "pi-ticket." + ticket], headers={"Origin": ORIGIN} if headers is None else headers)

    def test_ticket_requires_authorization_current_connection_and_exact_body(self):
        app, _, streams = self.make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            self.assertEqual(client.post("/api/stream-ticket", json={"generation": 0}).status_code, 401)
            self.assertEqual(client.post("/api/stream-ticket", headers=HEADERS,
                                         json={"generation": 0}).status_code, 409)
            connect(client)
            for body in ({"generation": 0}, {"generation": True}, {"generation": 1, "host": "other"}):
                self.assertNotEqual(client.post("/api/stream-ticket", headers=HEADERS, json=body).status_code, 200)
            ticket = self.ticket(client)
            self.assertEqual(len(ticket), 43)
            self.assertNotIn(ticket, client.get("/api/state", headers=HEADERS).text)
            self.assertEqual(streams, [])

    def test_websocket_rejects_origin_host_missing_bad_and_expired_ticket_before_factory(self):
        app, _, streams = self.make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            ticket = self.ticket(client)
            activity = app.state.controller._last_activity
            for secret, headers in ((ticket, {}), (ticket, {"Origin": "https://foreign.invalid"}),
                    (ticket, {"Origin": ORIGIN, "Host": "foreign.invalid"}),
                    ("x" * 43, {"Origin": ORIGIN})):
                with self.subTest(headers=headers), self.assertRaises(WebSocketDisconnect):
                    with self.websocket(client, secret, headers):
                        pass
            self.assertEqual(app.state.controller._last_activity, activity)
            self.assertEqual(streams, [])
            secret, generation, _ = app.state.controller._ticket
            app.state.controller._ticket = (secret, generation, time.monotonic() - 1)
            with self.assertRaises(WebSocketDisconnect):
                with self.websocket(client, ticket):
                    pass
            self.assertEqual(streams, [])

    def test_binary_stream_forwards_immediately_without_capture_and_blocks_json_input(self):
        app, transport, streams = self.make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            ticket = self.ticket(client)
            with self.websocket(client, ticket) as websocket:
                self.assertEqual(websocket.accepted_subprotocol, "binary")
                self.assertEqual(websocket.receive_bytes(), b"RFB 003.008\n")
                payload = b"\x05\x01\x00\x20\x00\x40"
                websocket.send_bytes(payload)
                self.assertEqual(websocket.receive_bytes(), payload)
                self.assertEqual(streams[0].received, [payload])
                self.assertEqual(transport.calls, [])
                state = client.get("/api/state", headers=HEADERS).json()
                self.assertTrue(state["streaming"])
                self.assertEqual(client.get("/api/frame", headers=HEADERS).status_code, 409)
                self.assertEqual(client.post("/api/action", headers=HEADERS, json=click_body()).status_code, 409)
                self.assertEqual(client.post("/api/stream-ticket", headers=HEADERS,
                                             json={"generation": 1}).status_code, 409)
            self.assertTrue(streams[0].closed.wait(2))
            with self.assertRaises(WebSocketDisconnect):
                with self.websocket(client, ticket):
                    pass
            self.assertEqual(len(streams), 1)

    def test_disconnect_closes_stream_before_auth_transport_and_invalidates_ticket(self):
        app, transport, streams = self.make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            ticket = self.ticket(client)
            with self.websocket(client, ticket) as websocket:
                websocket.receive_bytes()
                response = client.post("/api/disconnect", headers=HEADERS, json={})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(streams[0].closed.is_set())
                self.assertTrue(transport.closed)
                self.assertFalse(response.json()["state"]["streaming"])
                self.assertEqual(response.json()["state"]["generation"], 2)
            with self.assertRaises(WebSocketDisconnect):
                with self.websocket(client, ticket):
                    pass
            self.assertEqual(len(streams), 1)

    def test_text_and_oversized_messages_close_without_forwarding_or_replay(self):
        for payload in ("private text must not enter RFB", b"x" * 65_537):
            with self.subTest(kind=type(payload).__name__):
                app, _, streams = self.make_app()
                with TestClient(app, base_url=ORIGIN) as client:
                    connect(client)
                    with self.websocket(client, self.ticket(client)) as websocket:
                        websocket.receive_bytes()
                        if isinstance(payload, str):
                            websocket.send_text(payload)
                        else:
                            websocket.send_bytes(payload)
                        with self.assertRaises(WebSocketDisconnect):
                            websocket.receive_bytes()
                    self.assertTrue(streams[0].closed.wait(2))
                    self.assertEqual(streams[0].received, [])

    def test_vendor_paths_load_as_modules_without_external_assets_or_traversal(self):
        app, _, _ = self.make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.get("/vendor/novnc/core/rfb.js")
            self.assertEqual(response.status_code, 200)
            self.assertIn("javascript", response.headers["content-type"])
            self.assertIn("export default", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotEqual(client.get("/vendor/novnc/%2e%2e/%2e%2e/dashboard.py").status_code, 200)


class StreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def open_endpoint(self, app):
        controller = app.state.controller
        ticket = await controller.stream_ticket({"generation": controller.generation})
        websocket = MemoryWebSocket(ticket)
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/stream")
        task = asyncio.create_task(endpoint(websocket))
        await asyncio.wait_for(websocket.accepted.wait(), 2)
        return websocket, task

    async def test_cancelled_blocked_startup_joins_factory_and_candidate_close_before_unlocking(self):
        entered, release = threading.Event(), threading.Event()
        close_entered, close_release = threading.Event(), threading.Event()
        transport = DashboardTransport()

        class Candidate(FakeStream):
            def close(self):
                close_entered.set()
                if not close_release.wait(5):
                    raise AssertionError("candidate cleanup gate was not released")
                super().close()

        candidate = Candidate()
        calls = []
        def factory(bridge):
            calls.append(bridge)
            entered.set()
            if not release.wait(5):
                raise AssertionError("startup gate was not released")
            return candidate

        app = create_dashboard(token=TOKEN, origin=ORIGIN, connection_factory=lambda _: transport,
                               stream_factory=factory)
        controller = app.state.controller
        async with app.router.lifespan_context(app):
            await controller.connect({"mode": "existing", "host": "first"})
            websocket, task = await self.open_endpoint(app)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0.01)
                task.cancel()  # Repeated cancellation must retain the same worker.
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                with self.assertRaises(DashboardError) as caught:
                    await controller.disconnect()
                self.assertEqual(caught.exception.code, "operation_pending")
                release.set()
                self.assertTrue(await asyncio.to_thread(close_entered.wait, 2))
                self.assertFalse(task.done())
                self.assertIsNone(controller._stream)
                self.assertIsNone(controller._stream_task)
                with self.assertRaises(DashboardError) as caught:
                    await controller.connect({"mode": "existing", "host": "second"})
                self.assertEqual(caught.exception.code, "operation_pending")
                close_release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertTrue(candidate.closed.is_set())
                self.assertTrue(websocket.closed.is_set())
                self.assertEqual(calls, [transport])
                self.assertEqual(candidate.received, [])
                self.assertFalse(controller._lock.locked())
                self.assertIsNone(controller._stream_ws)
                self.assertTrue(controller.snapshot()["connected"])
                await controller.disconnect()
                self.assertTrue(transport.closed)
            finally:
                release.set()
                close_release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def exercise_blocked_io_release(self, *, switch_profile, block_send):
        events = []
        class ProfileTransport(DashboardTransport):
            def __init__(self, name):
                super().__init__()
                self.name = name

            def close(self):
                if not self.closed:
                    events.append("transport.close:" + self.name)
                super().close()

        first, second = ProfileTransport("first"), ProfileTransport("second")
        stream = BlockedStream(events)
        def connect_profile(details):
            events.append("connect:" + details["host"])
            return first if details["host"] == "first" else second

        app = create_dashboard(token=TOKEN, origin=ORIGIN, connection_factory=connect_profile,
                               stream_factory=lambda _: stream)
        controller = app.state.controller
        async with app.router.lifespan_context(app):
            await controller.connect({"mode": "existing", "host": "first"})
            websocket, task = await self.open_endpoint(app)
            try:
                self.assertTrue(await asyncio.to_thread(stream.recv_entered.wait, 2))
                relay = controller._stream_task
                payload = b"\x05\x01\x00\x20\x00\x40"
                if block_send:
                    websocket.messages.put_nowait({"type": "websocket.receive", "bytes": payload})
                    self.assertTrue(await asyncio.to_thread(stream.send_entered.wait, 2))
                async with asyncio.timeout(2):
                    if switch_profile:
                        await controller.connect({"mode": "existing", "host": "second"})
                    else:
                        await controller.disconnect()
                    await task
                self.assertTrue(stream.closed.is_set())
                self.assertTrue(stream.recv_done.is_set())
                self.assertEqual(stream.send_done.is_set(), block_send)
                self.assertTrue(relay.done())
                self.assertTrue(websocket.closed.is_set())
                self.assertTrue(first.closed)
                self.assertLess(events.index("stream.close"), events.index("transport.close:first"))
                state = controller.snapshot()
                self.assertEqual(state["generation"], 2)
                self.assertFalse(state["streaming"])
                self.assertTrue(state["observation_required"])
                self.assertEqual(state["connected"], switch_profile)
                if switch_profile:
                    self.assertIs(controller._transport, second)
                    self.assertFalse(second.closed)
                    self.assertLess(events.index("transport.close:first"), events.index("connect:second"))
                websocket.messages.put_nowait({"type": "websocket.receive", "bytes": b"late-input"})
                await asyncio.sleep(0.01)
                self.assertEqual(stream.received, [payload] if block_send else [])
                self.assertEqual(first.calls, [("disconnect", {})])
                self.assertEqual(second.calls, [])
            finally:
                stream.close()
                await controller.end_stream(websocket)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_disconnect_joins_blocked_receive_and_send_before_releasing_transport(self):
        for block_send in (False, True):
            with self.subTest(block_send=block_send):
                await self.exercise_blocked_io_release(switch_profile=False, block_send=block_send)

    async def test_profile_switch_joins_old_receive_and_send_before_opening_new_transport(self):
        for block_send in (False, True):
            with self.subTest(block_send=block_send):
                await self.exercise_blocked_io_release(switch_profile=True, block_send=block_send)

    async def test_raw_rfb_traffic_does_not_renew_visible_tab_heartbeat(self):
        transport, stream = DashboardTransport(), FakeStream()
        app = create_dashboard(token=TOKEN, origin=ORIGIN, connection_factory=lambda _: transport,
                               stream_factory=lambda _: stream)
        controller = app.state.controller
        async with app.router.lifespan_context(app):
            await controller.connect({"mode": "existing", "host": "first"})
            websocket, endpoint = await self.open_endpoint(app)
            producer = None
            try:
                async with asyncio.timeout(2):
                    while not websocket.frames:
                        await asyncio.sleep(0.005)
                self.assertEqual(websocket.frames[0], b"RFB 003.008\n")
                # Arm the short heartbeat after startup to avoid scheduling races.
                controller.heartbeat_timeout = 0.15
                controller.touch()
                activity = controller._last_activity
                async def send_raw_traffic():
                    while not websocket.closed.is_set():
                        websocket.messages.put_nowait({"type": "websocket.receive", "bytes": b"\x03\x01rfb"})
                        await asyncio.sleep(0.01)
                producer = asyncio.create_task(send_raw_traffic())
                async with asyncio.timeout(2):
                    await websocket.closed.wait()
                    await endpoint
                    await producer
                self.assertGreater(len(stream.received), 0)
                self.assertGreater(len(websocket.frames), 1)
                self.assertEqual(controller._last_activity, activity)
                self.assertTrue(stream.closed.is_set())
                self.assertTrue(transport.closed)
                self.assertEqual(controller.generation, 2)
                self.assertFalse(controller.snapshot()["connected"])
                self.assertFalse(controller.snapshot()["streaming"])
            finally:
                if producer is not None:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                stream.close()
                await controller.end_stream(websocket)
                if not endpoint.done():
                    endpoint.cancel()
                await asyncio.gather(endpoint, return_exceptions=True)

    async def test_cancelled_send_failure_is_observed_after_worker_is_joined(self):
        events, unobserved = [], []
        send_release = threading.Event()
        class FailingSend(BlockedStream):
            def send(self, data):
                self.send_entered.set()
                try:
                    if not send_release.wait(5):
                        raise AssertionError("send cancellation gate was not released")
                    raise ValueError("private remote diagnostic must not leak")
                finally:
                    self.send_done.set()

        stream, transport = FailingSend(events), DashboardTransport()
        app = create_dashboard(token=TOKEN, origin=ORIGIN, connection_factory=lambda _: transport,
                               stream_factory=lambda _: stream)
        controller = app.state.controller
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unobserved.append(context))
        async with app.router.lifespan_context(app):
            await controller.connect({"mode": "existing", "host": "first"})
            websocket, endpoint = await self.open_endpoint(app)
            disconnect = None
            try:
                websocket.messages.put_nowait({"type": "websocket.receive", "bytes": b"input"})
                self.assertTrue(await asyncio.to_thread(stream.send_entered.wait, 2))
                disconnect = asyncio.create_task(controller.disconnect())
                try:
                    async with asyncio.timeout(2):
                        while not any(task.cancelling() and task.get_coro().__qualname__.endswith(".<locals>.incoming")
                                      for task in asyncio.all_tasks()):
                            await asyncio.sleep(0.005)
                except TimeoutError:
                    remaining = [(task.get_coro().__qualname__, task.cancelling()) for task in asyncio.all_tasks()
                                 if "_relay_stream" in task.get_coro().__qualname__]
                    self.fail(f"stream IO was not cancelled: disconnect_done={disconnect.done()}, "
                              f"endpoint_done={endpoint.done()}, remaining={remaining}")
                self.assertFalse(disconnect.done())
                self.assertFalse(stream.send_done.is_set())
                send_release.set()
                async with asyncio.timeout(2):
                    await disconnect
                    await endpoint
                self.assertTrue(stream.send_done.is_set())
                self.assertTrue(transport.closed)
                gc.collect()
                await asyncio.sleep(0)
                self.assertEqual(unobserved, [], "cancelled worker exception escaped cleanup")
            finally:
                send_release.set()
                stream.close()
                if disconnect is not None:
                    await asyncio.gather(disconnect, return_exceptions=True)
                await controller.end_stream(websocket)
                if not endpoint.done():
                    endpoint.cancel()
                await asyncio.gather(endpoint, return_exceptions=True)
                loop.set_exception_handler(previous_handler)

    async def test_delayed_old_endpoint_failure_cannot_mark_replacement_connection_failed(self):
        first, second = DashboardTransport(), DashboardTransport()
        class FailedStream(FakeStream):
            def recv(self):
                raise ValueError("private failure from the previous stream")

        stream = FailedStream()
        app = create_dashboard(token=TOKEN, origin=ORIGIN,
                               connection_factory=lambda details: first if details["host"] == "first" else second,
                               stream_factory=lambda _: stream)
        controller = app.state.controller
        failed_old, resume_old = asyncio.Event(), asyncio.Event()
        original_shield = asyncio.shield

        async def delayed_endpoint_result(task):
            try:
                return await original_shield(task)
            except ValueError:
                # The relay has failed and completed its own cleanup; delay only
                # the old endpoint receiving that failure until after replacement.
                failed_old.set()
                await resume_old.wait()
                raise

        async with app.router.lifespan_context(app):
            await controller.connect({"mode": "existing", "host": "first"})
            with patch("pi_desktop_bridge.dashboard.asyncio.shield", side_effect=delayed_endpoint_result):
                websocket, endpoint = await self.open_endpoint(app)
                try:
                    await asyncio.wait_for(failed_old.wait(), 2)
                    self.assertTrue(stream.closed.is_set())
                    await controller.connect({"mode": "existing", "host": "second"})
                    before = controller.snapshot()
                    self.assertEqual(before["generation"], 2)
                    self.assertNotIn("error", before)
                    self.assertIs(controller._transport, second)
                    resume_old.set()
                    await asyncio.wait_for(endpoint, 2)
                    self.assertEqual(controller.snapshot(), before)
                    self.assertFalse(second.closed)
                    self.assertTrue(first.closed)
                    self.assertTrue(websocket.closed.is_set())
                finally:
                    resume_old.set()
                    if not endpoint.done():
                        endpoint.cancel()
                    await asyncio.gather(endpoint, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
