"""Exercise the localhost dashboard boundary without opening SSH or sending Pi input."""

import asyncio
import logging
import threading
import unittest

import httpx
from starlette.testclient import TestClient

from pi_desktop_bridge.transport import RemoteAgentError, TransportError
from test_server import ViewTransport, frame


ORIGIN = "http://127.0.0.1:8765"
TOKEN = "dashboard-secret"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}


class DashboardTransport(ViewTransport):
    def __init__(self):
        super().__init__()
        self.closed = False
        self.fail_action = False
        self.fail_capture = False
        self.busy = False

    def request(self, method, params=None, *, deadline=None):
        if method == "screenshot" and self.busy:
            raise RemoteAgentError("busy", "another client's private details", "not_started")
        if method == "click" and self.fail_action:
            self.calls.append((method, params))
            raise TransportError("password=never-expose-this")
        if method == "screenshot" and self.fail_capture:
            raise TransportError("private SSH diagnostic")
        return super().request(method, params, deadline=deadline)

    def close(self):
        self.closed = True


def make_app(factory=None):
    from pi_desktop_bridge.dashboard import create_dashboard
    transport = DashboardTransport()
    app = create_dashboard(token=TOKEN, origin=ORIGIN,
                           connection_factory=factory or (lambda details: transport))
    return app, transport


def connect(client, **details):
    return client.post("/api/connect", headers=HEADERS,
                       json={"mode": "existing", "host": "pi-desktop", **details})


def click_body(generation=1, **changes):
    return {"generation": generation, "action": "click", "args": {"x": 30, "y": 20},
            "desktop_width": 100, "desktop_height": 80, **changes}


class DashboardHTTPTests(unittest.TestCase):
    def test_factory_exists(self):
        import pi_desktop_bridge
        from importlib.util import find_spec
        self.assertIsNotNone(find_spec(pi_desktop_bridge.__name__ + ".dashboard"),
                             "dashboard backend has not been implemented")

    def test_auth_origin_host_and_security_headers(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            self.assertEqual(client.get("/api/state").status_code, 401)
            self.assertEqual(client.get("/api/state", headers={"Authorization": "Bearer bad"}).status_code, 401)
            good = client.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(good.status_code, 200)
            self.assertEqual(good.json()["host"], "pi-desktop")
            self.assertNotIn(TOKEN, good.text)
            self.assertEqual(good.headers["cache-control"], "no-store")
            self.assertIn("frame-ancestors 'none'", good.headers["content-security-policy"])
            for origin in ("null", "https://evil.test", ORIGIN + ".evil.test"):
                response = client.get("/api/state", headers={**HEADERS, "Origin": origin})
                self.assertEqual(response.status_code, 403)
            for host in ("evil.test", "localhost:8765", "127.0.0.1:9999"):
                self.assertEqual(client.get("/", headers={"Host": host}).status_code, 403)
            self.assertEqual(client.post("/api/connect", headers={"Authorization": f"Bearer {TOKEN}"},
                                         json={}).status_code, 403)
            self.assertEqual(client.options("/api/connect", headers={"Origin": "https://evil.test"}).status_code, 403)
        self.assertEqual(fake.calls, [])

    def test_body_and_action_validation_happen_before_transport(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            for body in ('{"host":"a","host":"b"}', '[]', 'null', '{', '{"x":NaN}'):
                response = client.post("/api/connect", headers=HEADERS, content=body)
                self.assertEqual(response.status_code, 400, body)
            response = client.post("/api/connect", headers=HEADERS, content=b" " * 65537)
            self.assertEqual(response.status_code, 413)
            self.assertEqual(connect(client, surprise=True).status_code, 400)
            for details in ({"host": "::1"}, {"host": "bad host"},
                            {"mode": "password", "username": "pi", "port": True},
                            {"mode": "password", "username": "pi", "password": None},
                            {"mode": "password", "username": "pi", "expected_fingerprint": "bad"}):
                self.assertEqual(connect(client, **details).status_code, 400)
            self.assertEqual(connect(client).status_code, 200)
            self.assertEqual(client.get("/api/frame", headers=HEADERS).status_code, 200)
            before = list(fake.calls)
            for body in (click_body(args={"x": 1, "y": 1, "view_id": "x"}),
                         click_body(args={"x": True, "y": 1}),
                         click_body(args={"x": 100, "y": 1}),
                         click_body(args={"x": 1, "y": 1, "count": 3}),
                         click_body(action="disconnect"), click_body(extra=1)):
                self.assertEqual(client.post("/api/action", headers=HEADERS, json=body).status_code, 400)
            self.assertEqual(fake.calls, before)

    def test_frame_generation_geometry_and_original_desktop_coordinates(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            self.assertEqual(client.post("/api/action", headers=HEADERS,
                                         json=click_body(generation=0)).status_code, 409)
            self.assertEqual(fake.calls, [])
            state = connect(client).json()["state"]
            self.assertEqual(state["generation"], 1)
            self.assertTrue(state["observation_required"])
            self.assertEqual(client.post("/api/action", headers=HEADERS, json=click_body()).status_code, 409)
            frame = client.get("/api/frame", headers=HEADERS).json()
            self.assertEqual(frame["frame"]["metadata"]["desktop_width"], 100)
            self.assertFalse(frame["state"]["observation_required"])
            before = list(fake.calls)
            for body in (click_body(generation=0), click_body(desktop_width=99)):
                self.assertEqual(client.post("/api/action", headers=HEADERS, json=body).status_code, 409)
            self.assertEqual(fake.calls, before)
            response = client.post("/api/action", headers=HEADERS, json=click_body())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(fake.calls[-2], ("click", {"x": 30, "y": 20, "button": "left", "count": 1}))
            self.assertEqual(fake.calls[-1], ("screenshot", {"max_width": 960}))

    def test_click_prepares_pointer_and_captures_before_sending_button_once(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            client.get("/api/frame", headers=HEADERS)
            for button, count in (("left", 1), ("right", 1), ("left", 2)):
                with self.subTest(button=button, count=count):
                    before = len(fake.calls)
                    args = {"x": 30, "y": 20, "button": button, "count": count}
                    response = client.post("/api/action", headers=HEADERS,
                                           json=click_body(args=args))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(fake.calls[before:], [
                        ("move", {"x": 29, "y": 20}),
                        ("screenshot", {"max_width": 960}),
                        ("move", {"x": 30, "y": 20}),
                        ("screenshot", {"max_width": 960}),
                        ("click", args),
                        ("screenshot", {"max_width": 960}),
                    ])
                    self.assertFalse(response.json()["state"]["observation_required"])

    def test_click_preparation_stays_in_bounds_at_edges_and_single_pixel_dimensions(self):
        for width, height, target, adjacent in (
                (100, 80, (0, 0), (1, 0)),
                (100, 80, (99, 79), (98, 79)),
                (2, 1, (1, 0), (0, 0)),
                (1, 80, (0, 0), (0, 1)),
                (1, 80, (0, 79), (0, 78)),
                (1, 1, (0, 0), None)):
            with self.subTest(width=width, height=height, target=target):
                class SizedDesktop(DashboardTransport):
                    def request(self, method, params=None, *, deadline=None):
                        result = super().request(method, params, deadline=deadline)
                        if method == "screenshot":
                            return frame(width, height, desktop_width=width, desktop_height=height)
                        return result
                fake = SizedDesktop()
                app, _ = make_app(lambda details: fake)
                with TestClient(app, base_url=ORIGIN) as client:
                    connect(client)
                    client.get("/api/frame", headers=HEADERS)
                    args = {"x": target[0], "y": target[1]}
                    response = client.post("/api/action", headers=HEADERS,
                                           json=click_body(args=args, desktop_width=width, desktop_height=height))
                    self.assertEqual(response.status_code, 200, response.text)
                    moves = [params for method, params in fake.calls if method == "move"]
                    expected = [args] if adjacent is None else [{"x": adjacent[0], "y": adjacent[1]}, args]
                    self.assertEqual(moves, expected)
                    self.assertTrue(all(0 <= point["x"] < width and 0 <= point["y"] < height for point in moves))
                    self.assertEqual([params for method, params in fake.calls if method == "click"],
                                     [{**args, "button": "left", "count": 1}])

    def test_failed_pointer_preparation_never_sends_click(self):
        for failure, observation_required in (
                (TransportError("private movement diagnostic"), True),
                (RemoteAgentError("invalid_params", "private movement diagnostic", "not_started"), False)):
            with self.subTest(failure=failure, observation_required=observation_required):
                class FailedMove(DashboardTransport):
                    def request(self, method, params=None, *, deadline=None):
                        if method == "move":
                            self.calls.append((method, params))
                            raise failure
                        return super().request(method, params, deadline=deadline)
                fake = FailedMove()
                app, _ = make_app(lambda details: fake)
                with TestClient(app, base_url=ORIGIN) as client:
                    connect(client)
                    client.get("/api/frame", headers=HEADERS)
                    before = len(fake.calls)
                    response = client.post("/api/action", headers=HEADERS, json=click_body())
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(fake.calls[before:], [("move", {"x": 29, "y": 20})])
                    self.assertEqual(response.json()["state"]["observation_required"], observation_required)
                    self.assertNotIn("private movement", response.text)

    def test_failed_preparation_capture_never_sends_click(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            client.get("/api/frame", headers=HEADERS)
            fake.fail_capture = True
            response = client.post("/api/action", headers=HEADERS, json=click_body())
            self.assertEqual(response.status_code, 502)
            self.assertTrue(response.json()["state"]["observation_required"])
            self.assertEqual(sum(method == "move" for method, _ in fake.calls), 1)
            self.assertEqual(sum(method == "click" for method, _ in fake.calls), 0)
            fake.fail_capture = False
            self.assertEqual(client.post("/api/action", headers=HEADERS, json=click_body()).status_code, 409)
            self.assertEqual(client.get("/api/frame", headers=HEADERS).status_code, 200)

    def test_failed_second_preparation_move_or_capture_never_sends_click(self):
        for failed_method in ("move", "screenshot"):
            with self.subTest(failed_method=failed_method):
                class FailedTargetPreparation(DashboardTransport):
                    moves = 0
                    def request(self, method, params=None, *, deadline=None):
                        if method == "move":
                            self.moves += 1
                        if self.moves == 2 and method == failed_method:
                            self.calls.append((method, params))
                            raise TransportError("private target preparation diagnostic")
                        return super().request(method, params, deadline=deadline)
                fake = FailedTargetPreparation()
                app, _ = make_app(lambda details: fake)
                with TestClient(app, base_url=ORIGIN) as client:
                    connect(client)
                    client.get("/api/frame", headers=HEADERS)
                    response = client.post("/api/action", headers=HEADERS, json=click_body())
                    self.assertEqual(response.status_code, 502)
                    self.assertTrue(response.json()["state"]["observation_required"])
                    self.assertNotIn("private target", response.text)
                    self.assertEqual(sum(method == "move" for method, _ in fake.calls), 2)
                    self.assertEqual(sum(method == "click" for method, _ in fake.calls), 0)
                    before = list(fake.calls)
                    self.assertEqual(client.post("/api/action", headers=HEADERS, json=click_body()).status_code, 409)
                    self.assertEqual(fake.calls, before)

    def test_geometry_change_during_preparation_aborts_click_until_fresh_observation(self):
        for changed_stage in (1, 2):
            with self.subTest(changed_stage=changed_stage):
                class ChangedGeometry(DashboardTransport):
                    def request(self, method, params=None, *, deadline=None):
                        result = super().request(method, params, deadline=deadline)
                        moves = sum(name == "move" for name, _ in self.calls)
                        if method == "screenshot" and moves >= changed_stage:
                            return frame(120, 80, desktop_width=120, desktop_height=80)
                        return result
                fake = ChangedGeometry()
                app, _ = make_app(lambda details: fake)
                with TestClient(app, base_url=ORIGIN) as client:
                    connect(client)
                    client.get("/api/frame", headers=HEADERS)
                    response = client.post("/api/action", headers=HEADERS, json=click_body())
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()["error"]["code"], "stale_geometry")
                    self.assertTrue(response.json()["state"]["observation_required"])
                    self.assertEqual(sum(method == "move" for method, _ in fake.calls), changed_stage)
                    self.assertEqual(sum(method == "click" for method, _ in fake.calls), 0)
                    self.assertEqual(client.post("/api/action", headers=HEADERS,
                                                 json=click_body(desktop_width=120)).status_code, 409)
                    self.assertEqual(client.get("/api/frame", headers=HEADERS).status_code, 200)
                    self.assertEqual(client.post("/api/action", headers=HEADERS,
                                                 json=click_body(desktop_width=120)).status_code, 200)
                    self.assertEqual(sum(method == "click" for method, _ in fake.calls), 1)

    def test_uncertain_action_is_never_replayed_and_requires_observation(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            client.get("/api/frame", headers=HEADERS)
            fake.fail_action = True
            failure = client.post("/api/action", headers=HEADERS, json=click_body())
            self.assertEqual(failure.status_code, 502)
            self.assertTrue(failure.json()["state"]["observation_required"])
            self.assertNotIn("never-expose", failure.text)
            fake.fail_action = False
            blocked = client.post("/api/action", headers=HEADERS, json=click_body())
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(sum(method == "click" for method, _ in fake.calls), 1)
            self.assertEqual(client.get("/api/frame", headers=HEADERS).status_code, 200)
            self.assertEqual(client.post("/api/action", headers=HEADERS, json=click_body()).status_code, 200)

    def test_busy_lease_and_connection_errors_are_sanitized(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            fake.busy = True
            response = client.get("/api/frame", headers=HEADERS)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "busy")
            self.assertTrue(response.json()["state"]["busy"])
            self.assertNotIn("private", response.text)

    def test_host_key_confirmation_and_failed_connect_do_not_expose_credentials(self):
        class KeyConfirmation(Exception):
            code = "host_key_confirmation_required"
            fingerprint = "SHA256:" + "a" * 43
            algorithm = "ssh-ed25519"
        received = []
        def factory(details):
            received.append(details)
            raise KeyConfirmation("private-user private-password private diagnostic")
        app, _ = make_app(factory)
        with TestClient(app, base_url=ORIGIN) as client:
            response = connect(client, mode="password", username="private-user", password="private-password")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["host_key"],
                             {"fingerprint": KeyConfirmation.fingerprint, "algorithm": "ssh-ed25519"})
            self.assertNotIn("private-", response.text)
            self.assertEqual(received, [{}])
            self.assertFalse(client.get("/api/state", headers=HEADERS).json()["connected"])

    def test_duplicate_security_headers_and_streamed_body_are_rejected(self):
        app, _ = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            for headers in ([*HEADERS.items(), ("Host", "127.0.0.1:8765"), ("Host", "evil.test")],
                            [*HEADERS.items(), ("Origin", ORIGIN)],
                            [*HEADERS.items(), ("Authorization", f"Bearer {TOKEN}")]):
                response = client.get("/api/state", headers=headers)
                self.assertIn(response.status_code, (401, 403))
            response = client.post("/api/connect", headers=HEADERS,
                                   content=iter([b" " * 32000, b" " * 32000, b" " * 2000]))
            self.assertEqual(response.status_code, 413)

    def test_all_six_actions_call_existing_tools_and_return_frames(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            client.get("/api/frame", headers=HEADERS)
            actions = [("move", {"x": 1, "y": 2}, "move"),
                       ("click", {"x": 1, "y": 2, "count": 2}, "click"),
                       ("drag", {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4}, "drag"),
                       ("scroll", {"direction": "down", "ticks": 2, "x": 1, "y": 2}, "scroll"),
                       ("type", {"text": "example"}, "type_text"),
                       ("key", {"keys": ["Control_L", "a"]}, "key")]
            for action, args, method in actions:
                response = client.post("/api/action", headers=HEADERS,
                                       json=click_body(action=action, args=args))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(fake.calls[-2][0], method)
                self.assertEqual(response.json()["frame"]["mime_type"], "image/png")

    def test_failed_post_action_capture_requires_full_observation(self):
        class FailedClickCapture(DashboardTransport):
            def request(self, method, params=None, *, deadline=None):
                if method == "click":
                    self.fail_capture = True
                return super().request(method, params, deadline=deadline)
        fake = FailedClickCapture()
        app, _ = make_app(lambda details: fake)
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            client.get("/api/frame", headers=HEADERS)
            response = client.post("/api/action", headers=HEADERS, json=click_body())
            self.assertEqual(response.status_code, 502)
            self.assertTrue(response.json()["state"]["observation_required"])
            self.assertEqual(client.get("/api/frame", headers=HEADERS).status_code, 502)
            self.assertEqual(client.post("/api/action", headers=HEADERS, json=click_body()).status_code, 409)
            self.assertEqual(sum(method == "click" for method, _ in fake.calls), 1)

    def test_static_shell_does_not_contain_credentials_or_token(self):
        app, _ = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            for path in ("/", "/app.js", "/style.css"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(TOKEN, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_untrusted_diagnostics_are_not_logged(self):
        app, fake = make_app()
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            client.get("/api/frame", headers=HEADERS)
            fake.fail_action = True
            with self.assertLogs(level=logging.DEBUG) as logs:
                client.post("/api/action", headers=HEADERS, json=click_body())
            self.assertNotIn("never-expose", "\n".join(logs.output))

    def test_profile_switch_closes_old_transport_and_invalidates_generation(self):
        transports = []
        def factory(details):
            transport = DashboardTransport()
            transports.append(transport)
            return transport
        app, _ = make_app(factory)
        with TestClient(app, base_url=ORIGIN) as client:
            connect(client)
            client.get("/api/frame", headers=HEADERS)
            response = connect(client, mode="password", host="other-pi", username="private-user", password="private-password")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"]["generation"], 2)
            self.assertNotIn("private-", response.text)
            self.assertTrue(transports[0].closed)
            self.assertIn(("disconnect", {}), transports[0].calls)
            self.assertEqual(client.post("/api/action", headers=HEADERS, json=click_body()).status_code, 409)
            result = client.post("/api/disconnect", headers=HEADERS, json={})
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json()["state"]["connected"])
            self.assertTrue(transports[1].closed)


class DashboardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_click_preparation_keeps_lock_and_never_sends_click(self):
        for blocked_stage, blocked_method in ((1, "move"), (1, "screenshot"), (2, "move"), (2, "screenshot")):
            with self.subTest(blocked_stage=blocked_stage, blocked_method=blocked_method):
                started, release = threading.Event(), threading.Event()
                class BlockingPreparation(DashboardTransport):
                    moves = 0
                    def request(self, method, params=None, *, deadline=None):
                        if method == "move":
                            self.moves += 1
                        if self.moves == blocked_stage and method == blocked_method:
                            started.set()
                            release.wait(5)
                        return super().request(method, params, deadline=deadline)
                fake = BlockingPreparation()
                app, _ = make_app(lambda details: fake)
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                        await client.post("/api/connect", headers=HEADERS, json={"host": "pi"})
                        await client.get("/api/frame", headers=HEADERS)
                        action = asyncio.create_task(client.post("/api/action", headers=HEADERS, json=click_body()))
                        try:
                            self.assertTrue(await asyncio.to_thread(started.wait, 2))
                            action.cancel()
                            await asyncio.sleep(0.01)
                            action.cancel()
                            await asyncio.sleep(0.01)
                            self.assertFalse(action.done())
                            self.assertEqual((await client.get("/api/frame", headers=HEADERS)).status_code, 409)
                            for path, body in (("action", click_body()), ("disconnect", {}),
                                               ("connect", {"host": "other-pi"})):
                                self.assertEqual((await client.post("/api/" + path, headers=HEADERS,
                                                                    json=body)).status_code, 409)
                        finally:
                            release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await action
                        self.assertTrue(app.state.controller.snapshot()["observation_required"])
                        self.assertEqual(sum(method == "move" for method, _ in fake.calls), blocked_stage)
                        self.assertEqual(sum(method == "click" for method, _ in fake.calls), 0)
                        self.assertEqual((await client.post("/api/action", headers=HEADERS,
                                                            json=click_body())).status_code, 409)
                        self.assertEqual((await client.get("/api/frame", headers=HEADERS)).status_code, 200)

    async def test_heartbeat_waits_for_inflight_capture_then_releases(self):
        started, release = threading.Event(), threading.Event()
        class SlowCapture(DashboardTransport):
            def request(self, method, params=None, *, deadline=None):
                if method == "screenshot":
                    started.set()
                    release.wait(5)
                return super().request(method, params, deadline=deadline)
        fake = SlowCapture()
        app, _ = make_app(lambda details: fake)
        controller = app.state.controller
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                await client.post("/api/connect", headers=HEADERS, json={"host": "pi"})
                capture = asyncio.create_task(client.get("/api/frame", headers=HEADERS))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                # Arm expiry only once the capture owns the operation lock.
                controller.heartbeat_timeout = 0.03
                controller.touch()
                await asyncio.sleep(0.06)
                self.assertFalse(fake.closed)
                self.assertNotIn(("disconnect", {}), fake.calls)
                release.set()
                self.assertEqual((await capture).status_code, 200)
                async with asyncio.timeout(2):
                    while not fake.closed:
                        await asyncio.sleep(0.01)

    async def test_lifespan_shutdown_releases_transport_and_timer(self):
        app, fake = make_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                await client.post("/api/connect", headers=HEADERS, json={"host": "pi"})
                await client.get("/api/frame", headers=HEADERS)
                self.assertFalse(fake.closed)
        self.assertTrue(fake.closed)
        self.assertEqual(sum(method == "disconnect" for method, _ in fake.calls), 1)
        self.assertFalse(any(task.get_name() == "pi-dashboard-heartbeat" for task in asyncio.all_tasks()))

    async def test_cancelled_profile_release_finishes_before_lock_release_without_new_auth(self):
        started, release = threading.Event(), threading.Event()
        class BlockingDisconnect(DashboardTransport):
            def disconnect(self, *, deadline=None):
                started.set()
                release.wait(5)
                return super().disconnect(deadline=deadline)
        fake = BlockingDisconnect()
        created = []
        def factory(details):
            created.append(details["host"])
            return fake if len(created) == 1 else DashboardTransport()
        app, _ = make_app(factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                await client.post("/api/connect", headers=HEADERS, json={"host": "first"})
                switch = asyncio.create_task(client.post("/api/connect", headers=HEADERS, json={"host": "second"}))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                switch.cancel()
                await asyncio.sleep(0.01)
                switch.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(switch.done())
                self.assertEqual((await client.get("/api/frame", headers=HEADERS)).status_code, 409)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await switch
                self.assertEqual(created, ["first"])
                self.assertTrue(fake.closed)
                self.assertFalse(app.state.controller.snapshot()["connected"])

    async def test_cancelled_input_joins_worker_and_requires_explicit_new_capture(self):
        started, release = threading.Event(), threading.Event()
        class BlockingInput(DashboardTransport):
            def request(self, method, params=None, *, deadline=None):
                if method == "click":
                    started.set()
                    release.wait(5)
                return super().request(method, params, deadline=deadline)
        fake = BlockingInput()
        app, _ = make_app(lambda details: fake)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                await client.post("/api/connect", headers=HEADERS, json={"host": "pi"})
                await client.get("/api/frame", headers=HEADERS)
                action = asyncio.create_task(client.post("/api/action", headers=HEADERS, json=click_body()))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                action.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(action.done())
                self.assertEqual((await client.post("/api/disconnect", headers=HEADERS, json={})).status_code, 409)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await action
                self.assertTrue(app.state.controller.snapshot()["observation_required"])
                self.assertEqual((await client.post("/api/action", headers=HEADERS, json=click_body())).status_code, 409)
                self.assertEqual(sum(method == "click" for method, _ in fake.calls), 1)
                self.assertEqual((await client.get("/api/frame", headers=HEADERS)).status_code, 200)

    async def test_cancelled_auth_joins_worker_closes_candidate_and_preserves_lock(self):
        started, release = threading.Event(), threading.Event()
        candidate = DashboardTransport()
        def factory(details):
            started.set()
            release.wait(5)
            return candidate
        app, _ = make_app(factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                task = asyncio.create_task(client.post("/api/connect", headers=HEADERS,
                                                      json={"mode": "existing", "host": "pi"}))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                other = await client.post("/api/disconnect", headers=HEADERS, json={})
                self.assertEqual(other.status_code, 409)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(candidate.closed)
                self.assertFalse(app.state.controller.snapshot()["connected"])

    async def test_heartbeat_releases_transport_after_browser_disappears(self):
        app, fake = make_app()
        app.state.controller.heartbeat_timeout = 0.03
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                response = await client.post("/api/connect", headers=HEADERS,
                                             json={"mode": "existing", "host": "pi"})
                self.assertEqual(response.status_code, 200)
                await client.get("/api/frame", headers=HEADERS)
                async with asyncio.timeout(2):
                    while not fake.closed:
                        await asyncio.sleep(0.01)
                self.assertFalse(app.state.controller.snapshot()["connected"])
                self.assertIn(("disconnect", {}), fake.calls)


if __name__ == "__main__":
    unittest.main()
