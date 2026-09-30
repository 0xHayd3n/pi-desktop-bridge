"""Exercise RFB wire data and owned-session cleanup without a Pi desktop."""
import base64
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pi_desktop_bridge import pi_agent as agent


class FragmentedSocket:
    def __init__(self, incoming=b"", fragment=3):
        self.incoming = bytearray(incoming)
        self.sent = []
        self.fragment = fragment
        self.closed = False
        self.fail_send = None

    def recv(self, count):
        count = min(count, self.fragment, len(self.incoming))
        value = bytes(self.incoming[:count])
        del self.incoming[:count]
        return value

    def sendall(self, value):
        self.sent.append(bytes(value))
        if self.fail_send == len(self.sent):
            raise OSError("injected send failure")

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


def handshake(width=2, height=1, version=8, name=b"test desktop"):
    return (
        ("RFB 003.%03d\n" % version).encode()
        + b"\x01\x01"
        + (b"\0\0\0\0" if version == 8 else b"")
        + struct.pack("!HH", width, height)
        + bytes(16)
        + struct.pack("!I", len(name))
        + name
    )


def rectangle(x, y, width, height, data=b"", encoding=0):
    return struct.pack("!HHHHi", x, y, width, height, encoding) + data


def update(*rectangles):
    return struct.pack("!BBH", 0, 0, len(rectangles)) + b"".join(rectangles)


def decode_png(data):
    """Independently check PNG CRCs and inflate unfiltered RGB scanlines."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("invalid PNG signature")
    position, chunks, idat = 8, {}, bytearray()
    while position < len(data):
        size = struct.unpack_from("!I", data, position)[0]
        kind = data[position + 4:position + 8]
        value = data[position + 8:position + 8 + size]
        crc = struct.unpack_from("!I", data, position + 8 + size)[0]
        if zlib.crc32(kind + value) & 0xffffffff != crc:
            raise AssertionError("invalid PNG CRC")
        chunks[kind] = value
        if kind == b"IDAT":
            idat.extend(value)
        position += size + 12
    width, height, depth, color, *_ = struct.unpack("!IIBBBBB", chunks[b"IHDR"])
    if (depth, color) != (8, 2):
        raise AssertionError("expected RGB8 PNG")
    raw = zlib.decompress(idat)
    rows = []
    for y in range(height):
        start = y * (width * 3 + 1)
        if raw[start] != 0:
            raise AssertionError("unexpected scanline filter")
        rows.append(raw[start + 1:start + 1 + width * 3])
    return width, height, b"".join(rows)


class RFBTests(unittest.TestCase):
    def client(self, extra=b"", **kwargs):
        wire = FragmentedSocket(handshake(**kwargs) + extra)
        client = agent.RFBClient(wire)
        client.handshake()
        return client, wire

    def test_handshake_handles_fragmented_reads_and_none_auth_versions(self):
        for version in (7, 8):
            with self.subTest(version=version):
                client, wire = self.client(version=version)
                self.assertEqual((client.width, client.height, client.name), (2, 1, "test desktop"))
                self.assertEqual(wire.sent[:3], [("RFB 003.%03d\n" % version).encode(), b"\x01", b"\x01"])
                self.assertEqual(wire.sent[3], b"\0\0\0\0" + struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
                self.assertEqual(wire.sent[4], struct.pack("!BBHii", 2, 0, 2, 0, -223))

    def test_png_preserves_exact_rgb_pixels_across_multiple_rectangles(self):
        # The negotiated little-endian 0x00RRGGBB format is B,G,R,padding.
        frame = update(rectangle(1, 0, 1, 1, b"\xff\x00\x00\0"), rectangle(0, 0, 1, 1, b"\x00\x00\xff\0"))
        client, wire = self.client(frame)
        self.assertEqual(decode_png(client.screenshot_png()), (2, 1, b"\xff\0\0\0\0\xff"))
        self.assertEqual(wire.sent[-1], struct.pack("!BBHHHH", 3, 0, 0, 0, 2, 1))

    def test_partial_update_is_not_returned_as_complete_screenshot(self):
        client, _ = self.client(update(rectangle(0, 0, 1, 1, bytes(4))))
        with self.assertRaisesRegex(agent.AgentError, "closed"):
            client.screenshot_png()

    def test_resize_requests_new_frame_and_ignores_bounded_auxiliary_messages(self):
        colormap = b"\x01\0" + struct.pack("!HH", 0, 1) + bytes(6)
        clipboard = b"\x03\0\0\0" + struct.pack("!I", 4) + b"text"
        data = colormap + b"\x02" + clipboard
        data += update(rectangle(0, 0, 1, 2, encoding=-223))
        data += update(rectangle(0, 0, 1, 2, b"\x03\x02\x01\0\x06\x05\x04\0"))
        client, _ = self.client(data)
        self.assertEqual(decode_png(client.screenshot_png()), (1, 2, bytes(range(1, 7))))

    def test_rejects_oversized_frame_before_allocation(self):
        for width, height in ((0, 1), (65535, 65535), (4097, 4096)):
            with self.subTest(size=(width, height)):
                with self.assertRaisesRegex(agent.AgentError, "dimensions"):
                    self.client(width=width, height=height)

    def test_rejects_outside_rectangle_unsupported_encoding_and_long_clipboard(self):
        messages = [
            update(rectangle(1, 0, 2, 1)),
            update(rectangle(0, 0, 2, 1, encoding=5)),
            b"\x03\0\0\0" + struct.pack("!I", 2**31),
        ]
        for data in messages:
            with self.subTest(data=data):
                client, _ = self.client(data)
                with self.assertRaises(agent.AgentError):
                    client.screenshot_png()

    def test_security_failure_does_not_attempt_other_authentication(self):
        wire = FragmentedSocket(b"RFB 003.008\n\x01\x02")
        with self.assertRaisesRegex(agent.AgentError, "None"):
            agent.RFBClient(wire).handshake()
        self.assertEqual(wire.sent, [b"RFB 003.008\n"])

    def test_read_timeout_has_useful_error(self):
        wire = FragmentedSocket()
        with mock.patch.object(wire, "recv", side_effect=socket.timeout):
            with self.assertRaisesRegex(agent.AgentError, "timed out"):
                agent.RFBClient(wire).handshake()


class Session:
    def __init__(self, client):
        self.client = client
        self.wayland_display = "/run/user/1000/wayland-0"
        self.closed = False

    def ensure(self):
        return self.client

    def is_active(self):
        return not self.closed

    def check_geometry(self):
        pass

    def close(self):
        self.closed = True
        self.client.close()

    def screenshot_png(self, region=None, max_width=None):
        if region is not None or max_width is not None:
            raise AssertionError("Patch transformed capture in tests")
        return self.client.screenshot_png()


class InputTests(unittest.TestCase):
    def setUp(self):
        self.wire = FragmentedSocket(handshake(width=100, height=80))
        self.client = agent.RFBClient(self.wire)
        self.client.handshake()
        self.wire.sent.clear()
        self.session = Session(self.client)
        self.desktop = agent.DesktopAgent(self.session)

    def pointer(self, x, y, mask=0):
        return struct.pack("!BBHH", 5, mask, x, y)

    def key(self, value, down):
        return struct.pack("!BB2xI", 4, int(down), value)

    def test_click_and_double_click_release_button(self):
        self.assertEqual(self.desktop.dispatch("click", {"x": 4, "y": 6, "button": "right", "count": 2}), {"ok": True})
        self.assertEqual(self.wire.sent, [self.pointer(4, 6), self.pointer(4, 6, 4), self.pointer(4, 6), self.pointer(4, 6, 4), self.pointer(4, 6)])

    def test_drag_interpolates_and_releases_at_destination(self):
        self.desktop.dispatch("drag", {"start_x": 0, "start_y": 0, "end_x": 8, "end_y": 6, "steps": 2})
        self.assertEqual(self.wire.sent, [self.pointer(0, 0), self.pointer(0, 0, 1), self.pointer(4, 3, 1), self.pointer(8, 6, 1), self.pointer(8, 6)])

    def test_click_and_drag_settle_at_new_position_before_pressing(self):
        cases = [
            ("click", {"x": 4, "y": 6}),
            ("drag", {"start_x": 4, "start_y": 6, "end_x": 8, "end_y": 10, "steps": 1}),
        ]
        for method, params in cases:
            with self.subTest(method=method):
                self.desktop.dispatch("move", {"x": 50, "y": 40})
                self.wire.sent.clear()
                at_wait = []
                with mock.patch.object(agent.time, "sleep", side_effect=lambda duration: at_wait.append((duration, self.wire.sent[:]))):
                    self.desktop.dispatch(method, params)
                self.assertTrue(at_wait)
                self.assertGreater(at_wait[0][0], 0)
                self.assertLessEqual(at_wait[0][0], 0.1)
                self.assertEqual(at_wait[0][1], [self.pointer(4, 6)])
                self.assertEqual(self.wire.sent[:2], [self.pointer(4, 6), self.pointer(4, 6, 1)])

    def test_failed_prepress_movement_does_not_press_or_retry(self):
        self.wire.fail_send = 1
        with self.assertRaises(agent.AgentError):
            self.desktop.dispatch("click", {"x": 4, "y": 6})
        self.assertEqual(self.wire.sent, [self.pointer(4, 6)])
        self.assertTrue(self.session.closed)

    def test_scroll_uses_wheel_button_pulses_at_current_position(self):
        self.desktop.dispatch("move", {"x": 10, "y": 20})
        self.desktop.dispatch("scroll", {"direction": "left", "ticks": 2})
        self.assertEqual(self.wire.sent, [self.pointer(10, 20), self.pointer(10, 20, 32), self.pointer(10, 20), self.pointer(10, 20, 32), self.pointer(10, 20)])

    def test_first_scroll_requires_explicit_coordinates_without_starting_session(self):
        with mock.patch.object(self.session, "ensure") as ensure:
            with self.assertRaises(agent.AgentError) as error:
                self.desktop.dispatch("scroll", {"direction": "down"})
        self.assertEqual(error.exception.input_state, "not_started")
        self.assertIn("x and y", str(error.exception))
        ensure.assert_not_called()
        self.assertEqual(self.wire.sent, [])

    def test_first_targeted_scroll_moves_before_wheel_delivery(self):
        self.desktop.dispatch("scroll", {"direction": "down", "ticks": 1, "x": 10, "y": 20})
        self.assertEqual(self.wire.sent, [self.pointer(10, 20), self.pointer(10, 20, 16), self.pointer(10, 20)])

    def test_hotkey_releases_in_reverse_order(self):
        self.desktop.dispatch("key", {"keys": ["Control_L", "Alt_L", "F4"]})
        values = [0xffe3, 0xffe9, 0xffc1]
        self.assertEqual(self.wire.sent, [self.key(value, True) for value in values] + [self.key(value, False) for value in reversed(values)])

    def test_unicode_text_and_option_like_text_are_passed_only_on_stdin(self):
        text = "-M ctrl; $(ignored) — café ✓🙂\r\n\t"
        environment = {"WAYLAND_DISPLAY": self.session.wayland_display, "LC_ALL": "C"}
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        with mock.patch.object(agent.shutil, "which", return_value="/usr/bin/wtype"), \
             mock.patch.object(agent, "_wayland_environment", return_value=(environment, "/run/user/1000")), \
             mock.patch.object(agent.subprocess, "Popen", return_value=process) as spawn:
            self.assertEqual(self.desktop.dispatch("type_text", {"text": text}), {"ok": True})
        self.assertEqual(spawn.call_args.args[0], ["/usr/bin/wtype", "-d", "2", "-"])
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["env"]["LC_ALL"], "C.UTF-8")
        self.assertEqual(process.communicate.call_args.args, (text.replace("\r\n", "\n").encode("utf-8"),))
        self.assertGreater(process.communicate.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(process.communicate.call_args.kwargs["timeout"], 41)
        self.assertEqual(self.wire.sent, [])

    def test_missing_wtype_fails_before_text_events(self):
        with mock.patch.object(agent.shutil, "which", return_value=None), \
             mock.patch.object(agent.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(agent.AgentError, "Install.*wtype"):
                self.desktop.dispatch("type_text", {"text": "some text"})
        spawn.assert_not_called()
        self.assertEqual(self.wire.sent, [])

    def test_wtype_timeout_stops_helper_without_retrying_input(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.communicate.side_effect = subprocess.TimeoutExpired("wtype", 5)
        environment = {"WAYLAND_DISPLAY": self.session.wayland_display}
        with mock.patch.object(agent.shutil, "which", return_value="wtype"), \
             mock.patch.object(agent, "_wayland_environment", return_value=(environment, "/run/user/1000")), \
             mock.patch.object(agent.subprocess, "Popen", return_value=process) as spawn:
            with self.assertRaisesRegex(agent.AgentError, "timed out.*partially") as error:
                self.desktop.dispatch("type_text", {"text": "café"})
        self.assertEqual(error.exception.input_state, "may_have_executed")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(process.communicate.call_count, 1)
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(self.session.closed)

    def test_wtype_nonzero_exit_reports_possible_partial_input(self):
        process = mock.Mock(returncode=1)
        process.poll.return_value = 1
        environment = {"WAYLAND_DISPLAY": self.session.wayland_display}
        with mock.patch.object(agent.shutil, "which", return_value="wtype"), \
             mock.patch.object(agent, "_wayland_environment", return_value=(environment, "/run/user/1000")), \
             mock.patch.object(agent.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(agent.AgentError, "partially"):
                self.desktop.dispatch("type_text", {"text": "café"})
        self.assertTrue(self.session.closed)

    def test_invalid_inputs_emit_no_events(self):
        cases = [
            ("move", {"x": True, "y": 0}), ("move", {"x": 100, "y": 0}),
            ("move", {"x": 0.5, "y": 0}), ("move", {"x": -1, "y": 0}),
            ("click", {"x": 0, "y": 0, "button": "back"}),
            ("click", {"x": 0, "y": 0, "count": 3}),
            ("drag", {"start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1, "steps": 201}),
            ("scroll", {"direction": "down", "ticks": 51}),
            ("scroll", {"direction": "down", "x": 2}),
            ("type_text", {"text": "x" * 4097}), ("type_text", {"text": "x\0"}),
            ("type_text", {"text": "\ud800"}), ("key", {"keys": ["Control_L", "run command"]}),
            ("key", {"keys": ["a", "a"]}), ("key", {"keys": []}),
            ("move", {"x": 1, "y": 1, "extra": "ignored?"}), ("shell", {"command": "whoami"}),
        ]
        for method, params in cases:
            with self.subTest(method=method, params=repr(params)[:80]):
                with self.assertRaises(agent.AgentError):
                    self.desktop.dispatch(method, params)
                self.assertEqual(self.wire.sent, [])

    def test_mid_chord_failure_attempts_all_releases_and_closes_session(self):
        self.wire.fail_send = 2
        with self.assertRaises(agent.AgentError) as error:
            self.desktop.dispatch("key", {"keys": ["Control_L", "a"]})
        self.assertEqual(error.exception.input_state, "may_have_executed")
        self.assertEqual(self.wire.sent[-2:], [self.key(97, False), self.key(0xffe3, False)])
        self.assertTrue(self.session.closed)

    def test_failed_drag_releases_pointer_and_closes_session(self):
        self.wire.fail_send = 3
        with self.assertRaises(agent.AgentError):
            self.desktop.dispatch("drag", {"start_x": 0, "start_y": 0, "end_x": 8, "end_y": 6, "steps": 2})
        self.assertEqual(self.wire.sent[-1], self.pointer(4, 3))
        self.assertTrue(self.session.closed)

    def test_status_does_not_capture_and_disconnect_closes(self):
        result = self.desktop.dispatch("status", {})
        self.assertEqual((result["width"], result["height"]), (100, 80))
        self.assertEqual(self.wire.sent, [])
        self.assertEqual(self.desktop.dispatch("disconnect", {}), {"ok": True})
        self.assertTrue(self.wire.closed)

    def test_screenshot_response_contains_decodable_image(self):
        self.wire.incoming.extend(update(rectangle(0, 0, 100, 80, bytes(100 * 80 * 4))))
        result = self.desktop.dispatch("screenshot", {})
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[:2], (100, 80))
        self.assertEqual((result["width"], result["height"], result["desktop_width"], result["desktop_height"]),
                         (100, 80, 100, 80))
        self.assertEqual(result["region"], {"x": 0, "y": 0, "width": 100, "height": 80})
        self.assertRegex(result["frame_id"], r"^[0-9a-f]{32}$")

    def test_region_and_resized_overview_metadata_and_fresh_tokens(self):
        cropped = agent._png_rgb_rows(3, 2, [b"\x22" * 9, b"\x33" * 9])
        overview = agent._png_rgb_rows(25, 20, [b"\x44" * 75 for _ in range(20)])
        with mock.patch.object(self.session, "screenshot_png", side_effect=[cropped, overview]) as capture:
            first = self.desktop.dispatch("screenshot", {"x": 10, "y": 20, "width": 9, "height": 6,
                                                         "max_width": 3})
            second = self.desktop.dispatch("screenshot", {"max_width": 25})
        self.assertEqual(capture.call_args_list, [mock.call((10, 20, 9, 6), 3),
                                                  mock.call((0, 0, 100, 80), 25)])
        self.assertEqual((first["width"], first["height"]), (3, 2))
        self.assertEqual(first["region"], {"x": 10, "y": 20, "width": 9, "height": 6})
        self.assertEqual((second["width"], second["height"]), (25, 20))
        self.assertEqual(second["region"], {"x": 0, "y": 0, "width": 100, "height": 80})
        self.assertNotEqual(first["frame_id"], second["frame_id"])

    def test_invalid_capture_shape_rejects_before_ensure_and_bounds_after_ensure(self):
        for params in ({"x": 0}, {"x": 0, "y": 0, "width": 1},
                       {"x": True, "y": 0, "width": 1, "height": 1},
                       {"x": 0, "y": 0, "width": 0, "height": 1},
                       {"x": 0, "y": 0, "width": 5000, "height": 5000},
                       {"max_width": 0}, {"max_width": 1.5}, {"max_width": True}):
            with self.subTest(params=params), mock.patch.object(self.session, "ensure") as ensure:
                with self.assertRaises(agent.AgentError) as error:
                    self.desktop.dispatch("screenshot", params)
                self.assertEqual(error.exception.code, "invalid_params")
                ensure.assert_not_called()
        with mock.patch.object(self.session, "screenshot_png") as capture:
            with self.assertRaises(agent.AgentError) as error:
                self.desktop.dispatch("screenshot", {"x": 99, "y": 0, "width": 2, "height": 1})
        self.assertEqual(error.exception.code, "invalid_params")
        capture.assert_not_called()
        self.assertFalse(self.session.closed)

    def test_latest_frame_token_rejects_stale_and_is_consumed_by_input(self):
        png = agent._png(100, 80, bytes(100 * 80 * 4))
        with mock.patch.object(self.session, "screenshot_png", return_value=png):
            first = self.desktop.dispatch("screenshot", {})["frame_id"]
            current = self.desktop.dispatch("screenshot", {})["frame_id"]
        with self.assertRaises(agent.AgentError) as stale:
            self.desktop.dispatch("move", {"x": 2, "y": 3, "frame_id": first})
        self.assertEqual((stale.exception.code, stale.exception.input_state), ("stale_view", "not_started"))
        self.assertEqual(self.wire.sent, [])
        self.assertFalse(self.session.closed)
        self.assertEqual(self.desktop.dispatch("move", {"x": 2, "y": 3, "frame_id": current}), {"ok": True})
        self.assertEqual(self.wire.sent, [self.pointer(2, 3)])
        with self.assertRaises(agent.AgentError) as consumed:
            self.desktop.dispatch("move", {"x": 4, "y": 5, "frame_id": current})
        self.assertEqual((consumed.exception.code, consumed.exception.input_state), ("stale_view", "not_started"))
        self.assertEqual(self.wire.sent, [self.pointer(2, 3)])

    def test_token_before_capture_and_failed_capture_send_no_input(self):
        unknown = "a" * 32
        with self.assertRaises(agent.AgentError) as absent:
            self.desktop.dispatch("move", {"x": 1, "y": 1, "frame_id": unknown})
        self.assertEqual((absent.exception.code, absent.exception.input_state), ("stale_view", "not_started"))
        self.assertFalse(self.session.closed)
        self.assertEqual(self.wire.sent, [])
        png = agent._png(100, 80, bytes(100 * 80 * 4))
        with mock.patch.object(self.session, "screenshot_png", return_value=png):
            frame_id = self.desktop.dispatch("screenshot", {})["frame_id"]
        with mock.patch.object(self.session, "screenshot_png", side_effect=agent.AgentError("capture failed")):
            with self.assertRaises(agent.AgentError):
                self.desktop.dispatch("screenshot", {})
        self.assertTrue(self.session.closed)
        self.assertIsNone(self.desktop.latest_frame_id)
        self.assertNotEqual(frame_id, self.desktop.latest_frame_id)

    def test_known_rejections_preserve_token_but_native_keyboard_consumes_it(self):
        png = agent._png(100, 80, bytes(100 * 80 * 4))
        with mock.patch.object(self.session, "screenshot_png", return_value=png):
            frame_id = self.desktop.dispatch("screenshot", {})["frame_id"]
        for params in ({"x": -1, "y": 0, "frame_id": frame_id},
                       {"x": 101, "y": 0, "frame_id": frame_id},
                       {"x": 1, "y": 1, "frame_id": "F" * 32}):
            with self.subTest(params=params):
                with self.assertRaises(agent.AgentError) as error:
                    self.desktop.dispatch("move", params)
                self.assertEqual(error.exception.code, "invalid_params")
                self.assertEqual(self.wire.sent, [])
        self.desktop.dispatch("status", {})
        self.desktop.dispatch("key", {"keys": ["a"]})
        with self.assertRaises(agent.AgentError) as stale:
            self.desktop.dispatch("move", {"x": 1, "y": 1, "frame_id": frame_id})
        self.assertEqual(stale.exception.code, "stale_view")
        self.assertEqual(self.wire.sent, [self.key(97, True), self.key(97, False)])

    def test_frame_token_cannot_cross_reconnected_client(self):
        png = agent._png(100, 80, bytes(100 * 80 * 4))
        with mock.patch.object(self.session, "screenshot_png", return_value=png):
            frame_id = self.desktop.dispatch("screenshot", {})["frame_id"]
        fresh_wire = FragmentedSocket()
        fresh_client = agent.RFBClient(fresh_wire)
        fresh_client.width, fresh_client.height = 100, 80
        self.session.client = fresh_client
        with self.assertRaises(agent.AgentError) as stale:
            self.desktop.dispatch("move", {"x": 1, "y": 1, "frame_id": frame_id})
        self.assertEqual((stale.exception.code, stale.exception.input_state), ("stale_view", "not_started"))
        self.assertEqual(fresh_wire.sent, [])
        self.assertTrue(fresh_wire.closed)

    def test_screenshot_uses_fresh_compositor_capture_instead_of_rfb_placeholder(self):
        self.wire.incoming.extend(update(rectangle(0, 0, 100, 80, b"\x60\x60\x60\0" * 8000)))
        pixels = b"\0\0\xff\0" * 8000
        actual_png = agent._png(100, 80, pixels)
        with mock.patch.object(self.session, "screenshot_png", return_value=actual_png):
            result = self.desktop.dispatch("screenshot", {})
        self.assertEqual(base64.b64decode(result["image_base64"]), actual_png)
        self.assertEqual(self.wire.sent, [])


class ServingTests(unittest.TestCase):
    setUp = InputTests.setUp
    def test_newline_protocol_recovers_from_bad_request_and_cleans_up_at_eof(self):
        incoming = io.StringIO('not json\n{"id":true,"method":"status","params":{}}\n{"id":7,"method":"status","params":{}}\n')
        outgoing = io.StringIO()
        agent.serve(self.desktop, incoming, outgoing)
        messages = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([m["id"] for m in messages], [None, None, 7])
        self.assertIn("error", messages[0])
        self.assertIn("error", messages[1])
        self.assertEqual(messages[2]["result"]["width"], 100)
        self.assertTrue(self.session.closed)

    def test_disconnect_response_precedes_exit_and_ignores_later_requests(self):
        incoming = io.StringIO('{"id":1,"method":"disconnect","params":{}}\n{"id":2,"method":"status","params":{}}\n')
        outgoing = io.StringIO()
        agent.serve(self.desktop, incoming, outgoing)
        self.assertEqual(json.loads(outgoing.getvalue()), {"id": 1, "result": {"ok": True}})
        self.assertTrue(self.session.closed)

    def test_overlong_line_is_rejected_without_parsing(self):
        outgoing = io.StringIO()
        agent.serve(self.desktop, io.StringIO("x" * (agent.MAX_REQUEST_BYTES + 1)), outgoing)
        self.assertIn("limit", json.loads(outgoing.getvalue())["error"]["message"])
        self.assertTrue(self.session.closed)


class ProtocolV4Tests(unittest.TestCase):
    setUp = InputTests.setUp

    def test_hello_is_lease_free_and_advertises_protocol_and_capabilities(self):
        with mock.patch.object(self.session, "ensure") as ensure, \
             mock.patch.object(agent.subprocess, "Popen") as spawn:
            result = self.desktop.dispatch("hello", {})
        self.assertEqual(result["protocol_version"], 4)
        self.assertEqual(result["agent_version"], "0.6.0")
        self.assertRegex(result["agent_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(set(result["capabilities"]), {"hello", "health", "status", "screenshot", "wait_for_stable", "move", "click", "drag", "scroll", "type_text", "key", "disconnect"})
        ensure.assert_not_called()
        spawn.assert_not_called()

    def test_hash_identifies_loaded_source_even_after_on_disk_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(agent.__file__).read_bytes()
            path = Path(directory) / "snapshot.py"
            path.write_bytes(source)
            spec = importlib.util.spec_from_file_location("snapshot_agent", path)
            snapshot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(snapshot)
            path.write_bytes(source + b"\n# replaced after startup\n")
            result = snapshot.DesktopAgent().dispatch("hello", {})
            self.assertEqual(result["agent_sha256"], hashlib.sha256(source).hexdigest())
            self.assertNotEqual(result["agent_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_health_reports_missing_dependency_without_starting_or_leasing(self):
        with mock.patch.object(self.session, "ensure") as ensure, \
             mock.patch.object(agent.subprocess, "Popen") as spawn, \
             mock.patch.object(agent.shutil, "which", side_effect=lambda name: None if name == "wtype" else "/usr/bin/" + name), \
             mock.patch.object(agent, "_wayland_environment", return_value=({}, "/run/user/1000")):
            result = self.desktop.dispatch("health", {})
        self.assertFalse(result["desktop_ready"])
        self.assertTrue(result["session_active"])
        self.assertTrue(any(check["name"] == "wtype" and not check["ok"] for check in result["checks"]))
        ensure.assert_not_called()
        spawn.assert_not_called()

    def test_health_reports_environment_failure_as_check_not_exception(self):
        with mock.patch.object(agent.shutil, "which", return_value="present"), \
             mock.patch.object(agent, "_wayland_environment", side_effect=agent.AgentError("No owned Wayland socket")):
            result = self.desktop.dispatch("health", {})
        self.assertFalse(result["desktop_ready"])
        self.assertTrue(any(not check["ok"] and "Wayland" in check["message"] for check in result["checks"]))

    def test_healthy_fresh_agent_never_acquires_desktop_lease(self):
        desktop = agent.DesktopAgent()
        with mock.patch.object(agent.shutil, "which", return_value="present"), \
             mock.patch.object(agent, "_wayland_environment", return_value=({}, "/run/user/1000")), \
             mock.patch.object(agent, "DesktopLease") as lease, \
             mock.patch.object(agent.subprocess, "Popen") as spawn:
            result = desktop.dispatch("health", {})
            desktop.dispatch("hello", {})
        self.assertTrue(result["desktop_ready"])
        self.assertFalse(result["session_active"])
        lease.assert_not_called()
        spawn.assert_not_called()

    def test_invalid_fresh_action_does_not_acquire_session(self):
        session = mock.Mock()
        session.is_active.return_value = False
        desktop = agent.DesktopAgent(session)
        cases = [("move", {"x": True, "y": 0}), ("click", {"x": 1, "y": 2, "button": "bad"}),
                 ("key", {"keys": ["bad key"]}), ("type_text", {"text": "\0"}),
                 ("scroll", {"direction": "down", "x": 4})]
        for method, params in cases:
            with self.subTest(method=method):
                with self.assertRaises(agent.AgentError) as error:
                    desktop.dispatch(method, params)
                self.assertEqual(error.exception.code, "invalid_params")
                self.assertEqual(error.exception.input_state, "not_started")
        session.ensure.assert_not_called()

    def test_fresh_coordinate_rejection_releases_new_session(self):
        self.session.closed = True
        with self.assertRaises(agent.AgentError) as error:
            self.desktop.dispatch("move", {"x": 500, "y": 0})
        self.assertEqual(error.exception.input_state, "not_started")
        self.assertEqual(error.exception.code, "invalid_params")
        self.assertTrue(self.wire.closed)
        self.assertEqual(self.wire.sent, [])

    def test_existing_session_survives_known_coordinate_rejection(self):
        with self.assertRaises(agent.AgentError) as error:
            self.desktop.dispatch("move", {"x": 500, "y": 0})
        self.assertEqual(error.exception.input_state, "not_started")
        self.assertFalse(self.session.closed)
        self.assertFalse(self.wire.closed)

    def test_rejected_action_releases_session_replaced_during_ensure(self):
        self.desktop.client = self.client
        fresh_wire = FragmentedSocket()
        fresh_client = agent.RFBClient(fresh_wire)
        fresh_client.width, fresh_client.height = 20, 10
        def replace():
            self.session.client = fresh_client
            return fresh_client
        with mock.patch.object(self.session, "ensure", side_effect=replace):
            with self.assertRaises(agent.AgentError) as error:
                self.desktop.dispatch("move", {"x": 50, "y": 30})
        self.assertEqual(error.exception.input_state, "not_started")
        self.assertTrue(fresh_wire.closed)
        self.assertEqual(fresh_wire.sent, [])

    def test_missing_typing_dependency_rejects_before_fresh_session(self):
        with mock.patch.object(agent.shutil, "which", return_value=None), \
             mock.patch.object(self.session, "ensure") as ensure:
            with self.assertRaises(agent.AgentError) as error:
                self.desktop.dispatch("type_text", {"text": "café"})
        self.assertEqual((error.exception.code, error.exception.input_state), ("missing_dependency", "not_started"))
        ensure.assert_not_called()
        self.assertEqual(self.wire.sent, [])

    def test_geometry_change_rejects_input_then_later_capture_can_reconnect(self):
        failure = agent.AgentError("Desktop geometry changed", code="geometry_changed")
        with mock.patch.object(self.session, "check_geometry", side_effect=failure):
            with self.assertRaises(agent.AgentError) as error:
                self.desktop.dispatch("click", {"x": 4, "y": 6})
        self.assertEqual((error.exception.code, error.exception.input_state), ("geometry_changed", "not_started"))
        self.assertEqual(self.wire.sent, [])
        self.assertTrue(self.session.closed)
        fresh_wire = FragmentedSocket(update(rectangle(0, 0, 20, 10, b"\0\0\xff\0" * 200)))
        fresh_client = agent.RFBClient(fresh_wire)
        fresh_client.width, fresh_client.height = 20, 10
        fresh_client.frame = bytearray(20 * 10 * 4)
        def reconnect():
            self.session.client = fresh_client
            self.session.closed = False
            return fresh_client
        with mock.patch.object(self.session, "ensure", side_effect=reconnect) as ensure:
            result = self.desktop.dispatch("screenshot", {})
        ensure.assert_called_once()
        self.assertEqual((result["width"], result["height"]), (20, 10))
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"])), (20, 10, b"\xff\0\0" * 200))
        self.assertFalse(fresh_wire.closed)

    def test_each_input_checks_output_before_any_event_or_helper(self):
        cases = [("move", {"x": 4, "y": 6}), ("click", {"x": 4, "y": 6}),
                 ("drag", {"start_x": 4, "start_y": 6, "end_x": 8, "end_y": 9}),
                 ("scroll", {"direction": "down", "x": 4, "y": 6}),
                 ("key", {"keys": ["a"]}), ("type_text", {"text": "café"})]
        for method, params in cases:
            with self.subTest(method=method):
                session = Session(self.client)
                desktop = agent.DesktopAgent(session)
                with mock.patch.object(session, "check_geometry", side_effect=agent.AgentError("changed", code="geometry_changed")), \
                     mock.patch.object(agent.shutil, "which", return_value="wtype"), \
                     mock.patch.object(agent.subprocess, "Popen") as spawn:
                    with self.assertRaises(agent.AgentError) as error:
                        desktop.dispatch(method, params)
                self.assertEqual((error.exception.code, error.exception.input_state), ("geometry_changed", "not_started"))
                spawn.assert_not_called()
                self.assertEqual(self.wire.sent, [])

    def test_helper_spawn_failure_is_not_started_and_does_not_retry(self):
        environment = {"WAYLAND_DISPLAY": self.session.wayland_display}
        with mock.patch.object(agent.shutil, "which", return_value="wtype"), \
             mock.patch.object(agent, "_wayland_environment", return_value=(environment, "/run/user/1000")), \
             mock.patch.object(agent.subprocess, "Popen", side_effect=FileNotFoundError("wtype disappeared")) as spawn:
            with self.assertRaises(agent.AgentError) as error:
                self.desktop.dispatch("type_text", {"text": "café"})
        self.assertEqual(error.exception.input_state, "not_started")
        spawn.assert_called_once()
        self.assertTrue(self.session.closed)

    def test_unexpected_key_failure_cleans_up_and_returns_conservative_safe_error(self):
        with mock.patch.object(self.client, "key_event", side_effect=RuntimeError("private debug details")):
            with self.assertRaises(agent.AgentError) as error:
                self.desktop.dispatch("key", {"keys": ["Control_L", "a"]})
        self.assertEqual((error.exception.code, error.exception.input_state), ("internal_error", "may_have_executed"))
        self.assertNotIn("private", str(error.exception))
        self.assertTrue(self.session.closed)
        self.assertIsNone(self.desktop.client)
        self.assertEqual(self.desktop.held_keys, [])

    def test_failure_after_first_pointer_write_is_ambiguous_and_not_retried(self):
        self.wire.fail_send = 1
        with self.assertRaises(agent.AgentError) as error:
            self.desktop.dispatch("move", {"x": 4, "y": 6})
        self.assertEqual(error.exception.input_state, "may_have_executed")
        self.assertEqual(len(self.wire.sent), 1)
        self.assertTrue(self.session.closed)

    def test_structured_error_envelope_recovers_for_following_hello(self):
        requests = io.StringIO('{"id":1,"method":"move","params":{"x":true,"y":0}}\n{"id":2,"method":"hello","params":{}}\n')
        output = io.StringIO()
        agent.serve(self.desktop, requests, output)
        rejection, success = map(json.loads, output.getvalue().splitlines())
        self.assertEqual(set(rejection["error"]), {"code", "message", "input_state"})
        self.assertEqual(rejection["error"]["input_state"], "not_started")
        self.assertEqual(success["result"]["protocol_version"], 4)


class LifecycleTests(unittest.TestCase):
    def test_stream_start_requests_prompt_capture_without_compositor_screenshot(self):
        with tempfile.TemporaryDirectory() as runtime:
            wire = FragmentedSocket(handshake())
            wire.connect = mock.Mock()
            process = mock.Mock()
            process.poll.return_value = None
            with mock.patch.object(agent, "_wayland_environment", return_value=({"WAYLAND_DISPLAY": "wayland-0"}, runtime)), \
                 mock.patch.object(agent, "DesktopLease") as lease, \
                 mock.patch.object(agent.OwnedWayVNC, "_select_output_name", return_value="HDMI-A-1") as select_output, \
                 mock.patch.object(agent.OwnedWayVNC, "screenshot_png") as capture, \
                 mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
                 mock.patch.object(agent.shutil, "which", return_value="wayvnc"), \
                 mock.patch.object(agent.subprocess, "Popen", return_value=process) as spawn, \
                 mock.patch.object(agent.socket, "socket", return_value=wire):
                session = agent.OwnedWayVNC()
                try:
                    client = session.ensure(max_fps=agent.STREAM_MAX_FPS, prime_capture=False)
                    self.assertEqual((client.width, client.height), (2, 1))
                    select_output.assert_called_once()
                    capture.assert_not_called()
                    argv = spawn.call_args.args[0]
                    self.assertEqual(argv[argv.index("-f") + 1], "1000")
                    self.assertEqual(argv[-1], str(session.directory / "rfb.sock"))
                finally:
                    session.close()
                self.assertTrue(wire.closed)
                process.terminate.assert_called_once()
                lease.return_value.close.assert_called_once()

    def test_contended_lease_prevents_starting_wayvnc(self):
        with mock.patch.object(agent, "_wayland_environment", return_value=({}, "/run/user/1000")), \
             mock.patch.object(agent.shutil, "which", return_value="wayvnc"), \
             mock.patch.object(agent, "DesktopLease", side_effect=agent.AgentError("already controlled"), create=True), \
             mock.patch.object(agent.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(agent.AgentError, "already controlled"):
                agent.OwnedWayVNC().ensure()
        spawn.assert_not_called()

    def test_start_is_lazy_private_unix_only_and_close_removes_owned_directory(self):
        with tempfile.TemporaryDirectory() as runtime:
            wire = FragmentedSocket(handshake())
            wire.connect = mock.Mock()
            process = mock.Mock()
            process.poll.return_value = None
            environment = {"XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": "/run/user/1000/wayland-0"}
            with mock.patch.object(agent, "_wayland_environment", return_value=(environment, runtime)), \
                 mock.patch.object(agent, "DesktopLease", create=True) as lease_class, \
                 mock.patch.object(agent.OwnedWayVNC, "_select_output_name", return_value="HDMI-A-1", create=True), \
                 mock.patch.object(agent.OwnedWayVNC, "screenshot_png", return_value=b"ready", create=True) as capture, \
                 mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
                 mock.patch.object(agent.shutil, "which", return_value="/usr/bin/wayvnc"), \
                 mock.patch.object(agent.subprocess, "Popen", return_value=process) as spawn, \
                 mock.patch.object(agent.socket, "socket", return_value=wire) as socket_factory:
                session = agent.OwnedWayVNC()
                spawn.assert_not_called()
                client = session.ensure()
                capture.assert_called_once()
                self.assertIs(session.ensure(), client)
                spawn.assert_called_once()
                argv = spawn.call_args.args[0]
                directory = Path(argv[-1]).parent
                self.assertTrue(directory.is_dir())
                self.assertEqual(argv[:6], ["/usr/bin/wayvnc", "-C", os.devnull, "-u", "-S", str(directory / "control.sock")])
                self.assertIn("-r", argv)
                self.assertIn("-R", argv)
                self.assertEqual(spawn.call_args.kwargs["umask"], 0o077)
                self.assertEqual(spawn.call_args.kwargs["stdout"], agent.subprocess.DEVNULL)
                self.assertEqual(spawn.call_args.kwargs["env"], environment)
                socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
                session.close()
                self.assertTrue(wire.closed)
                process.terminate.assert_called_once()
                self.assertFalse(directory.exists())
                lease_class.assert_called_once_with(runtime)
                lease_class.return_value.close.assert_called_once()
                session.close()

    def test_handshake_failure_stops_process_and_removes_directory(self):
        with tempfile.TemporaryDirectory() as runtime:
            wire = FragmentedSocket(b"invalid data")
            wire.connect = mock.Mock()
            process = mock.Mock()
            process.poll.return_value = None
            with mock.patch.object(agent, "_wayland_environment", return_value=({"WAYLAND_DISPLAY": "wayland-0"}, runtime)), \
                 mock.patch.object(agent, "DesktopLease", create=True) as lease_class, \
                 mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
                 mock.patch.object(agent.shutil, "which", return_value="wayvnc"), \
                 mock.patch.object(agent.subprocess, "Popen", return_value=process), \
                 mock.patch.object(agent.socket, "socket", return_value=wire):
                session = agent.OwnedWayVNC()
                with self.assertRaises(agent.AgentError):
                    session.ensure()
                self.assertEqual(list(Path(runtime).iterdir()), [])
                self.assertTrue(wire.closed)
                process.terminate.assert_called_once()
                lease_class.return_value.close.assert_called_once()

    def test_close_escalates_only_owned_unresponsive_process(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [agent.subprocess.TimeoutExpired("wayvnc", 2), 0]
        session = agent.OwnedWayVNC()
        session.process = process
        session.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_wayland_discovery_rejects_foreign_or_non_socket_paths(self):
        with tempfile.TemporaryDirectory() as runtime:
            path = Path(runtime) / "wayland-0"
            path.write_text("not a socket")
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": str(path)}), \
                 mock.patch.object(agent.os, "getuid", return_value=1000, create=True):
                with self.assertRaises(agent.AgentError):
                    agent._wayland_environment()

    def test_main_signal_handler_exits_and_finally_closes(self):
        desktop = mock.Mock()
        handlers = {}
        def register(signum, handler):
            handlers[signum] = handler
        def run(*args):
            handlers[agent.signal.SIGTERM](agent.signal.SIGTERM, None)
        with mock.patch.object(agent, "DesktopAgent", return_value=desktop), \
             mock.patch.object(agent.signal, "signal", side_effect=register), \
             mock.patch.object(agent, "serve", side_effect=run):
            with self.assertRaises(SystemExit):
                agent.main()
        desktop.close.assert_called()


class CompositorCaptureTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        self.session = agent.OwnedWayVNC()
        self.session.directory = Path(self.runtime.name)
        self.session.environment = {"WAYLAND_DISPLAY": "/run/user/1000/wayland-0"}
        self.session.output_name = "HDMI-A-1"
        self.session.client = SimpleNamespace(width=2, height=1)
        self.capture_geometry = mock.Mock()
        self.png = agent._png(2, 1, b"\0\0\xff\0\xff\0\0\0")

    def capture_with_output(self, png, process=None, region=None, max_width=None):
        process = process or mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        def start(argv, **kwargs):
            kwargs["stdout"].write(png)
            return process
        with mock.patch.object(agent.shutil, "which", return_value="/usr/bin/grim"), \
             mock.patch.object(agent.subprocess, "Popen", side_effect=start) as spawn, \
             mock.patch.object(self.session, "check_geometry", self.capture_geometry):
            result = self.session.screenshot_png(region, max_width)
        return result, spawn, process

    def test_capture_selects_same_output_and_preserves_exact_pixels(self):
        result, spawn, process = self.capture_with_output(self.png)
        self.assertEqual(decode_png(result), (2, 1, b"\xff\0\0\0\0\xff"))
        self.assertEqual(spawn.call_args.args[0], ["/usr/bin/grim", "-c", "-t", "png", "-l", "3", "-o", "HDMI-A-1", "-"])
        self.assertEqual(spawn.call_args.kwargs["env"], self.session.environment)
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(process.wait.call_args.kwargs["timeout"], agent.IO_TIMEOUT)
        self.assertEqual(self.capture_geometry.call_count, 2)

    def test_ppm_crop_preserves_exact_edge_pixels_without_native_geometry_flags(self):
        self.session.client = SimpleNamespace(width=4, height=3)
        rgb = bytes(channel for y in range(3) for x in range(4)
                    for channel in (x + 10 * y, 100 + x, 200 + y))
        ppm = b"P6\n4 3\n255\n" + rgb
        actual, spawn, _ = self.capture_with_output(ppm, region=(1, 1, 3, 2))
        expected = bytes(channel for y in (1, 2) for x in (1, 2, 3)
                         for channel in (x + 10 * y, 100 + x, 200 + y))
        self.assertEqual(decode_png(actual), (3, 2, expected))
        self.assertEqual(spawn.call_args.args[0], ["/usr/bin/grim", "-c", "-t", "ppm", "-o", "HDMI-A-1", "-"])
        self.assertEqual(self.capture_geometry.call_count, 2)

    def test_ppm_nearest_neighbor_samples_pixel_centers_for_noninteger_ratio(self):
        self.session.client = SimpleNamespace(width=5, height=4)
        rgb = bytes(channel for y in range(4) for x in range(5)
                    for channel in (x, y, x + 5 * y))
        actual, _, _ = self.capture_with_output(b"P6\n5 4\n255\n" + rgb,
                                                region=(0, 0, 5, 4), max_width=3)
        expected = bytes(channel for y in (1, 3) for x in (0, 2, 4)
                         for channel in (x, y, x + 5 * y))
        self.assertEqual(decode_png(actual), (3, 2, expected))

    def test_ppm_rejects_malformed_size_and_dimension_payloads(self):
        good = b"P6\n2 1\n255\n" + b"\x01\x02\x03\x04\x05\x06"
        cases = (b"P3\n2 1\n255\n" + good[-6:],
                 b"P6\n2 1\n65535\n" + good[-6:],
                 b"P6\n1 1\n255\n" + good[-6:],
                 good[:-1], good + b"\0",
                 b"P6\n" + b" " * 257 + b"2 1\n255\n" + good[-6:])
        for ppm in cases:
            with self.subTest(ppm=ppm[:25]):
                with self.assertRaises(agent.AgentError):
                    self.capture_with_output(ppm, region=(0, 0, 2, 1))
        with mock.patch.object(agent, "MAX_PPM_BYTES", len(good) - 1):
            with self.assertRaisesRegex(agent.AgentError, "byte limit"):
                self.capture_with_output(good, region=(0, 0, 2, 1))

    def test_geometry_is_checked_before_and_after_both_capture_paths(self):
        for region in (None, (0, 0, 2, 1)):
            with self.subTest(region=region):
                self.capture_geometry.reset_mock()
                data = self.png if region is None else b"P6\n2 1\n255\n" + b"\xff\0\0\0\0\xff"
                changed = agent.AgentError("output changed", code="geometry_changed")
                self.capture_geometry.side_effect = [None, changed]
                with self.assertRaises(agent.AgentError) as error:
                    self.capture_with_output(data, region=region)
                self.assertEqual(error.exception.code, "geometry_changed")
                self.assertEqual(self.capture_geometry.call_count, 2)
                self.capture_geometry.side_effect = None

    def test_valid_solid_gray_compositor_image_is_accepted(self):
        gray = agent._png(2, 1, b"\x60\x60\x60\0" * 2)
        self.assertEqual(self.capture_with_output(gray)[0], gray)

    def test_invalid_oversized_and_different_dimensions_are_rejected(self):
        oversized_header = bytearray(self.png)
        oversized_header[16:24] = struct.pack("!II", 65535, 65535)
        oversized_header[29:33] = struct.pack("!I", zlib.crc32(oversized_header[12:29]) & 0xffffffff)
        for png in (b"not a PNG", self.png[:-12], agent._png(1, 1, bytes(4)), bytes(oversized_header)):
            with self.subTest(size=len(png)):
                with self.assertRaises(agent.AgentError):
                    self.capture_with_output(png)

    def test_capture_byte_limit_is_checked_before_loading_image(self):
        with mock.patch.object(agent, "MAX_PNG_BYTES", len(self.png) - 1):
            with self.assertRaisesRegex(agent.AgentError, "byte limit"):
                self.capture_with_output(self.png)

    def test_timeout_terminates_capture_helper(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("grim", 10), 0]
        with mock.patch.object(agent.shutil, "which", return_value="grim"), \
             mock.patch.object(agent.subprocess, "Popen", return_value=process), \
             mock.patch.object(self.session, "check_geometry", self.capture_geometry):
            with self.assertRaisesRegex(agent.AgentError, "timed out"):
                self.session.screenshot_png()
        process.terminate.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_private_control_response_selects_captured_output_without_newline(self):
        response = {"id": 1, "code": 0, "data": [
            {"name": "HDMI-A-2", "captured": False},
            {"name": "HDMI-A-1", "captured": True, "width": 2, "height": 1, "power": "ON"},
        ]}
        wire = FragmentedSocket(json.dumps(response).encode(), fragment=7)
        wire.connect = mock.Mock()
        with mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
             mock.patch.object(agent.socket, "socket", return_value=wire):
            self.assertEqual(self.session._select_output_name(), "HDMI-A-1")
        self.assertEqual(json.loads(wire.sent[0]), {"id": 1, "method": "output-list"})
        self.assertTrue(wire.closed)

    def test_startup_waits_for_initial_unknown_power_metadata_before_selection(self):
        initial = {"name": "HDMI-A-1", "width": 2, "height": 1, "captured": True}
        wires = []
        for power in ("UNKNOWN", "ON"):
            wire = FragmentedSocket(json.dumps({"id": 1, "code": 0, "data": [{**initial, "power": power}]}).encode(), fragment=7)
            wire.connect = mock.Mock()
            wires.append(wire)
        with mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
             mock.patch.object(agent.socket, "socket", side_effect=wires), \
             mock.patch.object(agent.time, "sleep") as sleep:
            self.assertEqual(self.session._select_output_name(), "HDMI-A-1")
        self.assertEqual(self.session.output_geometry["power"], "ON")
        self.assertTrue(all(wire.closed for wire in wires))
        self.assertTrue(all(len(wire.sent) == 1 and json.loads(wire.sent[0])["method"] == "output-list" for wire in wires))
        sleep.assert_called_once()

    def test_startup_unknown_power_has_one_bounded_readiness_deadline(self):
        unknown = {"name": "HDMI-A-1", "width": 2, "height": 1, "power": "UNKNOWN", "captured": True}
        now = [0.0]
        def advance(delay):
            now[0] += delay
        with mock.patch.object(self.session, "_query_selected_output", return_value=unknown) as query, \
             mock.patch.object(agent, "IO_TIMEOUT", 0.05), \
             mock.patch.object(agent.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(agent.time, "sleep", side_effect=advance):
            with self.assertRaisesRegex(agent.AgentError, "power.*ready") as error:
                self.session._select_output_name()
        self.assertEqual(error.exception.input_state, "not_started")
        self.assertIsNone(self.session.output_geometry)
        self.assertLessEqual(now[0], 0.05)
        self.assertLessEqual(query.call_count, 4)
        self.assertTrue(all(call.kwargs.get("deadline") == 0.05 for call in query.call_args_list))

    def test_preflight_rejects_changed_output_size_name_or_power_and_releases(self):
        initial = {"name": "HDMI-A-1", "width": 2, "height": 1, "power": "ON", "captured": True}
        self.session.output_geometry = initial
        for changed in ({"width": 3}, {"height": 2}, {"name": "HDMI-A-2"}, {"power": "OFF"}, {"captured": False}):
            with self.subTest(changed=changed):
                with mock.patch.object(self.session, "_query_selected_output", return_value={**initial, **changed}, create=True), \
                     mock.patch.object(self.session, "close") as close:
                    with self.assertRaises(agent.AgentError) as error:
                        self.session.check_geometry()
                self.assertEqual((error.exception.code, error.exception.input_state), ("geometry_changed", "not_started"))
                close.assert_called_once()

    def test_preflight_accepts_unchanged_output_without_releasing(self):
        initial = {"name": "HDMI-A-1", "width": 2, "height": 1, "power": "ON", "captured": True}
        self.session.output_geometry = initial
        with mock.patch.object(self.session, "_query_selected_output", return_value=initial.copy(), create=True), \
             mock.patch.object(self.session, "close") as close:
            self.session.check_geometry()
        close.assert_not_called()

    def test_missing_unpowered_or_malformed_control_geometry_rejects_input(self):
        initial = {"name": "HDMI-A-1", "width": 2, "height": 1, "power": "ON", "captured": True}
        self.session.output_geometry = initial
        cases = [[], [{**initial, "captured": False}], [{**initial, "power": "OFF"}], [{**initial, "power": "UNKNOWN"}],
                 [{**initial, "width": True}], [{**initial, "height": 0}],
                 [{**initial, "width": 65535, "height": 65535}]]
        for outputs in cases:
            with self.subTest(outputs=outputs):
                wire = FragmentedSocket(json.dumps({"id": 1, "code": 0, "data": outputs}).encode(), fragment=7)
                wire.connect = mock.Mock()
                with mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
                     mock.patch.object(agent.socket, "socket", return_value=wire), \
                     mock.patch.object(self.session, "close") as close:
                    with self.assertRaises(agent.AgentError) as error:
                        self.session.check_geometry()
                self.assertEqual((error.exception.code, error.exception.input_state), ("geometry_changed", "not_started"))
                self.assertEqual(len(wire.sent), 1)
                self.assertTrue(wire.closed)
                close.assert_called_once()

    def test_control_timeout_rejects_before_input_and_releases_session(self):
        wire = FragmentedSocket()
        wire.connect = mock.Mock()
        wire.recv = mock.Mock(side_effect=socket.timeout())
        with mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
             mock.patch.object(agent.socket, "socket", return_value=wire), \
             mock.patch.object(self.session, "close") as close:
            with self.assertRaises(agent.AgentError) as error:
                self.session.check_geometry()
        self.assertEqual((error.exception.code, error.exception.input_state), ("preflight_failed", "not_started"))
        self.assertTrue(wire.closed)
        close.assert_called_once()


class VisualStabilityTests(unittest.TestCase):
    """Use actual PPM files and encoding; replace only clock and Pi processes."""

    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        self.session = agent.OwnedWayVNC()
        self.session.directory = Path(self.runtime.name)
        self.session.environment = {"WAYLAND_DISPLAY": "/run/user/1000/wayland-0"}
        self.session.output_name = "HDMI-A-1"
        self.client = SimpleNamespace(width=4, height=2, close=mock.Mock(), pointer=mock.Mock())
        self.session.client = self.client
        self.geometry = {"name": "HDMI-A-1", "width": 4, "height": 2, "power": "ON", "captured": True}
        self.session.output_geometry = self.geometry.copy()
        self.desktop = agent.DesktopAgent(self.session)
        self.now = 0.0
        self.capture_seconds = 0.01
        self.samples = [self.ppm(1)]
        self.captures = []
        self.files = []
        self.processes = []
        self.wait_timeouts = []
        self.query_deadlines = []
        self.wait_failure = None
        temporary_file = tempfile.TemporaryFile

        def private_file(*args, **kwargs):
            output = temporary_file(*args, **kwargs)
            self.files.append(output)
            return output

        patches = [
            mock.patch.object(self.session, "ensure", return_value=self.client),
            mock.patch.object(self.session, "is_active", return_value=True),
            mock.patch.object(self.session, "_query_selected_output", side_effect=self.query),
            mock.patch.object(agent.time, "monotonic", side_effect=lambda: self.now),
            mock.patch.object(agent.time, "sleep", side_effect=self.advance),
            mock.patch.object(agent.shutil, "which", return_value="/usr/bin/grim"),
            mock.patch.object(agent.tempfile, "TemporaryFile", side_effect=private_file),
            mock.patch.object(agent.subprocess, "Popen", side_effect=self.capture),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def ppm(value):
        return b"P6\n4 2\n255\n" + bytes([value]) * 24

    def advance(self, seconds):
        self.now += seconds

    def query(self, deadline=None, sampling=False):
        self.query_deadlines.append(deadline)
        return self.geometry.copy()

    def capture(self, argv, **kwargs):
        index = len(self.captures)
        self.captures.append((self.now, argv, kwargs))
        output = kwargs["stdout"]
        self.assertEqual(output.tell(), 0)
        self.assertEqual(os.fstat(output.fileno()).st_size, 0)
        output.write(self.samples[min(index, len(self.samples) - 1)])
        process = mock.Mock()
        process.poll.return_value = 0

        def wait(timeout):
            self.wait_timeouts.append(timeout)
            if self.wait_failure is not None:
                failure, self.wait_failure = self.wait_failure, None
                process.poll.return_value = None
                raise failure
            if timeout < self.capture_seconds:
                self.advance(timeout)
                process.poll.return_value = None
                raise subprocess.TimeoutExpired("grim", timeout)
            self.advance(self.capture_seconds)
            process.poll.return_value = 0
            return 0

        process.wait.side_effect = wait
        self.processes.append(process)
        return process

    def observe(self, **params):
        return self.desktop.dispatch("wait_for_stable", params)

    def assert_clean(self):
        self.assertEqual(len(self.files), 2)
        self.assertTrue(all(output.closed for output in self.files))
        self.assertIsNone(self.session.capture_process)

    def test_unchanged_pixels_return_stable_exact_sample_and_fresh_usable_token(self):
        self.desktop.latest_frame_id = "a" * 32
        result = self.observe()
        stability = result["stability"]
        self.assertEqual(stability, {"stable": True, "timed_out": False,
                                     "elapsed_ms": 340, "samples": 4,
                                     "stable_ms": 300, "timeout_ms": 5000, "poll_ms": 100})
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"])), (4, 2, bytes([1]) * 24))
        self.assertEqual(result["region"], {"x": 0, "y": 0, "width": 4, "height": 2})
        self.assertNotEqual(result["frame_id"], "a" * 32)
        self.assertEqual(len(self.captures), 4)  # No unobserved final capture.
        self.assertTrue(all(item[1] == ["/usr/bin/grim", "-c", "-t", "ppm", "-o", "HDMI-A-1", "-"]
                            for item in self.captures))
        self.assertTrue(all(deadline == 5.0 for deadline in self.query_deadlines))
        self.assertEqual(self.wait_timeouts, [5.0, 4.89, 4.78, 4.67])
        self.client.pointer.assert_not_called()
        self.desktop.dispatch("move", {"x": 1, "y": 1, "frame_id": result["frame_id"]})
        self.client.pointer.assert_called_once_with(1, 1, 0)
        self.assert_clean()

    def test_change_restarts_unchanged_duration_at_first_changed_sample(self):
        self.samples = [self.ppm(1), self.ppm(2), self.ppm(2)]
        result = self.observe(stable_ms=200, poll_ms=100)
        self.assertTrue(result["stability"]["stable"])
        self.assertEqual(result["stability"]["samples"], 4)
        self.assertGreaterEqual(result["stability"]["elapsed_ms"], 330)
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[2], bytes([2]) * 24)
        self.assert_clean()

    def test_animation_times_out_with_latest_sample_and_no_capture_after_deadline(self):
        self.samples = [self.ppm(index) for index in range(8)]
        result = self.observe(stable_ms=200, timeout_ms=350, poll_ms=100)
        self.assertEqual(result["stability"], {"stable": False, "timed_out": True,
                                             "elapsed_ms": 350, "samples": 4,
                                             "stable_ms": 200, "timeout_ms": 350, "poll_ms": 100})
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[2], bytes([3]) * 24)
        for before, after in zip(self.captures, self.captures[1:]):
            self.assertGreaterEqual(after[0] - before[0] + 1e-9, self.capture_seconds + 0.1)
        self.assertTrue(all(item[0] < 0.35 for item in self.captures))
        self.assert_clean()

    def test_terminal_capture_deadline_returns_latest_valid_image_and_keeps_session(self):
        self.capture_seconds = 0.09
        self.samples = [self.ppm(1), self.ppm(2), self.ppm(3)[:-6]]
        result = self.observe(stable_ms=200, timeout_ms=350, poll_ms=50)
        self.assertEqual(result["stability"], {"stable": False, "timed_out": True,
                                             "elapsed_ms": 350, "samples": 2,
                                             "stable_ms": 200, "timeout_ms": 350, "poll_ms": 50})
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[2], bytes([2]) * 24)
        self.assertEqual(len(self.captures), 3)
        self.assertAlmostEqual(self.wait_timeouts[2], 0.07)
        self.processes[2].terminate.assert_called_once()
        self.assertGreater(self.now, 0.35)  # Cleanup is outside sampling elapsed_ms.
        self.client.close.assert_not_called()
        self.client.pointer.assert_not_called()
        self.desktop.dispatch("move", {"x": 1, "y": 1, "frame_id": result["frame_id"]})
        self.client.pointer.assert_called_once_with(1, 1, 0)
        self.assert_clean()

    def test_terminal_capture_deadline_without_valid_sample_is_error(self):
        self.capture_seconds = 0.2
        with self.assertRaisesRegex(agent.AgentError, "timed out.*valid sample"):
            self.observe(stable_ms=100, timeout_ms=100, poll_ms=50)
        self.processes[0].terminate.assert_called_once()
        self.client.close.assert_called_once()
        self.assertIsNone(self.desktop.latest_frame_id)
        self.assert_clean()

    def test_early_helper_timeout_after_valid_sample_still_raises(self):
        original = self.capture
        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            if len(self.captures) == 2:
                self.wait_failure = subprocess.TimeoutExpired("grim", 0.001)
            return process

        with mock.patch.object(agent.subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(agent.AgentError, "timed out"):
                self.observe(stable_ms=200, timeout_ms=350, poll_ms=50)
        self.assertEqual(len(self.captures), 2)
        self.client.close.assert_called_once()
        self.assert_clean()

    def geometry_deadline(self, query_number, *, early=False, during_connect=False):
        wires = []
        response = json.dumps({"id": 1, "code": 0, "data": [self.geometry]}).encode()
        def socket_factory(*args):
            wire = FragmentedSocket(response)
            wire.connect = mock.Mock()
            wires.append(wire)
            if len(wires) == query_number:
                if during_connect:
                    wire.connect.side_effect = lambda path: setattr(self, "now", 0.35)
                else:
                    def expire(count):
                        if not early:
                            self.now = 0.35
                        raise socket.timeout()
                    wire.recv = expire
            return wire

        original_query = agent.OwnedWayVNC._query_selected_output.__get__(self.session)
        with mock.patch.object(self.session, "_query_selected_output", side_effect=original_query), \
             mock.patch.object(agent.socket, "AF_UNIX", 1, create=True), \
             mock.patch.object(agent.socket, "socket", side_effect=socket_factory):
            result = self.observe(stable_ms=200, timeout_ms=350, poll_ms=50)
        self.assertTrue(all(wire.closed for wire in wires))
        if during_connect:
            self.assertEqual(wires[query_number - 1].sent, [])
        return result

    def test_terminal_pre_geometry_deadline_returns_previous_valid_image(self):
        self.capture_seconds = 0.09
        self.samples = [self.ppm(index) for index in (1, 2, 3)]
        result = self.geometry_deadline(5)
        self.assertEqual(result["stability"]["samples"], 2)
        self.assertEqual(result["stability"]["elapsed_ms"], 350)
        self.assertFalse(result["stability"]["stable"])
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[2], bytes([2]) * 24)
        self.assertEqual(len(self.captures), 2)
        self.client.close.assert_not_called()
        self.assert_clean()

    def test_geometry_connect_consuming_deadline_stops_before_query_send(self):
        self.samples = [self.ppm(index) for index in (1, 2, 3)]
        result = self.geometry_deadline(3, during_connect=True)
        self.assertEqual(result["stability"]["samples"], 1)
        self.assertTrue(result["stability"]["timed_out"])
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[2], bytes([1]) * 24)
        self.assertEqual(len(self.captures), 1)
        self.client.close.assert_not_called()
        self.assert_clean()

    def test_terminal_post_geometry_deadline_discards_unvalidated_candidate(self):
        self.samples = [self.ppm(index) for index in (1, 2, 3)]
        result = self.geometry_deadline(6)
        self.assertEqual(result["stability"]["samples"], 2)
        self.assertEqual(result["stability"]["elapsed_ms"], 350)
        self.assertFalse(result["stability"]["stable"])
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[2], bytes([2]) * 24)
        self.assertEqual(len(self.captures), 3)
        self.client.close.assert_not_called()
        self.assert_clean()

    def test_malformed_completed_candidate_is_not_hidden_by_post_geometry_expiry(self):
        self.samples = [self.ppm(1), self.ppm(2)[:-6]]
        with self.assertRaisesRegex(agent.AgentError, "pixel payload"):
            self.geometry_deadline(4)
        self.client.close.assert_called_once()
        self.assert_clean()

    def test_geometry_budget_expiry_without_valid_sample_is_error(self):
        with self.assertRaisesRegex(agent.AgentError, "timed out.*valid sample"):
            self.geometry_deadline(1)
        self.assertEqual(len(self.captures), 0)
        self.client.close.assert_called_once()
        self.assert_clean()

    def test_early_geometry_timeout_after_valid_sample_still_raises(self):
        with self.assertRaisesRegex(agent.AgentError, "query timed out"):
            self.geometry_deadline(3, early=True)
        self.assertEqual(len(self.captures), 1)
        self.client.close.assert_called_once()
        self.assert_clean()

    def test_geometry_mismatch_at_deadline_still_raises_after_valid_sample(self):
        def query(deadline=None, sampling=False):
            self.query_deadlines.append(deadline)
            if len(self.query_deadlines) == 3:
                self.now = deadline
                return {**self.geometry, "width": 5}
            return self.geometry.copy()

        with mock.patch.object(self.session, "_query_selected_output", side_effect=query):
            with self.assertRaises(agent.AgentError) as error:
                self.observe(stable_ms=200, timeout_ms=350, poll_ms=50)
        self.assertEqual(error.exception.code, "geometry_changed")
        self.client.close.assert_called_once()
        self.assert_clean()

    def test_nonzero_helper_at_deadline_still_raises_after_valid_sample(self):
        original = self.capture
        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            if len(self.captures) == 2:
                def fail(timeout):
                    self.advance(timeout)
                    return 1
                process.wait.side_effect = fail
            return process

        with mock.patch.object(agent.subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(agent.AgentError, "could not capture"):
                self.observe(stable_ms=200, timeout_ms=350, poll_ms=50)
        self.client.close.assert_called_once()
        self.assert_clean()

    def test_native_roi_detects_changes_hidden_by_returned_resize(self):
        # A 4-to-1 resize selects x=2,y=1, so changing x=0 is invisible in PNG.
        self.samples = [self.ppm(1)[:-24] + bytes([index]) * 3 + bytes([1]) * 21 for index in range(8)]
        result = self.observe(max_width=1, stable_ms=200, timeout_ms=350, poll_ms=100)
        self.assertFalse(result["stability"]["stable"])
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"])), (1, 1, bytes([1]) * 3))
        self.assert_clean()

    def test_every_native_roi_row_is_monitored(self):
        # Bottom-right pixel is inside ROI but not selected in a 4-to-1 image.
        self.samples = [self.ppm(1)[:-3] + bytes([index]) * 3 for index in range(8)]
        result = self.observe(max_width=1, stable_ms=200, timeout_ms=350, poll_ms=100)
        self.assertFalse(result["stability"]["stable"])
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"])), (1, 1, bytes([1]) * 3))

    def test_ppm_header_variation_does_not_count_as_pixel_change(self):
        self.samples = [self.ppm(1), b"P6\n# same RGB\n4 2\n255\n" + bytes([1]) * 24]
        result = self.observe(stable_ms=200, poll_ms=100)
        self.assertTrue(result["stability"]["stable"])
        self.assertEqual(result["stability"]["samples"], 3)

    def test_one_valid_sample_can_time_out_but_cannot_be_stable(self):
        result = self.observe(stable_ms=100, timeout_ms=100, poll_ms=100)
        self.assertEqual(result["stability"]["samples"], 1)
        self.assertEqual(result["stability"]["elapsed_ms"], 100)
        self.assertFalse(result["stability"]["stable"])
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"]))[2], bytes([1]) * 24)

    def test_maximum_timeout_minimum_poll_remains_bounded(self):
        self.capture_seconds = 0
        self.samples = [self.ppm(index % 256) for index in range(202)]
        result = self.observe(stable_ms=2000, timeout_ms=10000, poll_ms=50)
        self.assertFalse(result["stability"]["stable"])
        self.assertLessEqual(result["stability"]["samples"], 201)
        self.assertEqual(result["stability"]["elapsed_ms"], 10000)
        self.assert_clean()

    def test_pixels_outside_roi_do_not_reset_stability_and_return_exact_roi(self):
        self.samples = [self.ppm(1)[:-24] + bytes([index]) * 3 + bytes([1]) * 21 for index in range(8)]
        result = self.observe(x=1, y=0, width=3, height=2, max_width=2,
                              stable_ms=200, timeout_ms=350, poll_ms=100)
        self.assertTrue(result["stability"]["stable"])
        self.assertEqual(result["region"], {"x": 1, "y": 0, "width": 3, "height": 2})
        self.assertEqual(decode_png(base64.b64decode(result["image_base64"])), (2, 1, bytes([1]) * 6))
        self.assert_clean()

    def test_startup_and_final_encoding_are_outside_sampling_elapsed_time(self):
        def ensure():
            self.advance(7)
            return self.client

        encode = agent._ppm_png
        def slow_encode(*args):
            self.advance(2)
            return encode(*args)

        with mock.patch.object(self.session, "ensure", side_effect=ensure), \
             mock.patch.object(agent, "_ppm_png", side_effect=slow_encode):
            result = self.observe()
        self.assertEqual(result["stability"]["elapsed_ms"], 340)
        self.assertTrue(all(deadline == 12 for deadline in self.query_deadlines))
        self.assertAlmostEqual(self.now, 9.34)
        self.assert_clean()

    def test_capture_timeout_is_error_with_cleanup_and_invalidates_previous_token(self):
        self.desktop.latest_frame_id = "a" * 32
        self.wait_failure = subprocess.TimeoutExpired("grim", 0.2)
        with self.assertRaisesRegex(agent.AgentError, "timed out") as error:
            self.observe(stable_ms=100, timeout_ms=200, poll_ms=50)
        self.assertEqual(error.exception.input_state, "not_started")
        self.processes[0].terminate.assert_called_once()
        self.assertIsNone(self.desktop.latest_frame_id)
        self.assert_clean()

    def test_unresponsive_capture_is_killed_after_bounded_terminate_wait(self):
        original = self.capture
        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("grim", 0.2),
                                        subprocess.TimeoutExpired("grim", 2), 0]
            return process

        with mock.patch.object(agent.subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(agent.AgentError, "timed out"):
                self.observe(stable_ms=100, timeout_ms=200, poll_ms=50)
        self.processes[0].terminate.assert_called_once()
        self.processes[0].kill.assert_called_once()
        self.assertEqual(self.processes[0].wait.call_args_list,
                         [mock.call(timeout=0.2), mock.call(timeout=2), mock.call(timeout=2)])
        self.assert_clean()

    def test_capture_child_gets_hard_file_limit_without_changing_parent(self):
        resource = mock.Mock(RLIMIT_FSIZE=1)
        with mock.patch.object(agent, "resource", resource, create=True):
            self.observe()
            resource.setrlimit.assert_not_called()
            callback = self.captures[0][2]["preexec_fn"]
            callback()
        resource.setrlimit.assert_called_once_with(1, (agent.MAX_PPM_BYTES, agent.MAX_PPM_BYTES))

    def test_failed_capture_or_limit_setup_is_error_and_cleans_file(self):
        for failure in (OSError("cannot execute"), subprocess.SubprocessError("preexec failed")):
            with self.subTest(failure=failure):
                with mock.patch.object(agent.subprocess, "Popen", side_effect=failure):
                    with self.assertRaisesRegex(agent.AgentError, "capture failed") as error:
                        self.session.wait_for_stable((0, 0, 4, 2), None, 100, 200, 50)
                self.assertEqual(error.exception.input_state, "not_started")
                self.assertTrue(self.files[-1].closed)
                self.assertIsNone(self.session.capture_process)

    def test_capture_receives_budget_remaining_after_geometry_query(self):
        def query(deadline=None, sampling=False):
            self.advance(0.02)
            self.query_deadlines.append(deadline)
            return self.geometry.copy()

        with mock.patch.object(self.session, "_query_selected_output", side_effect=query):
            self.observe(stable_ms=100, timeout_ms=500, poll_ms=100)
        self.assertEqual(self.query_deadlines, [0.5] * 4)
        self.assertAlmostEqual(self.wait_timeouts[0], 0.48)
        self.assertAlmostEqual(self.wait_timeouts[1], 0.33)

    def test_deadline_exhausted_by_preflight_does_not_start_capture(self):
        def query(deadline=None, sampling=False):
            self.advance(0.1)
            return self.geometry.copy()

        with mock.patch.object(self.session, "_query_selected_output", side_effect=query):
            with self.assertRaisesRegex(agent.AgentError, "timed out"):
                self.observe(stable_ms=100, timeout_ms=100, poll_ms=100)
        self.assertEqual(self.captures, [])
        self.assert_clean()

    def test_geometry_query_error_after_valid_sample_never_claims_stable(self):
        with mock.patch.object(self.session, "_query_selected_output", side_effect=[
                self.geometry, self.geometry, agent.AgentError("query timed out")]):
            with self.assertRaisesRegex(agent.AgentError, "query timed out") as error:
                self.observe()
        self.assertEqual(error.exception.code, "preflight_failed")
        self.assertEqual(len(self.captures), 1)
        self.assert_clean()

    def test_oversized_ppm_fails_before_pixel_read(self):
        with mock.patch.object(agent, "MAX_PPM_BYTES", len(self.ppm(1)) - 1):
            with self.assertRaisesRegex(agent.AgentError, "byte limit"):
                self.observe()
        self.assert_clean()

    def test_nonzero_helper_exit_never_returns_an_observation(self):
        original = self.capture
        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            process.wait.side_effect = None
            process.wait.return_value = 1
            return process

        with mock.patch.object(agent.subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(agent.AgentError, "could not capture"):
                self.observe()
        self.assert_clean()

    def test_malformed_ppm_fails_instead_of_reporting_timeout_or_stable(self):
        valid = self.ppm(1)
        for invalid in (valid[:-1], valid + b"\0", valid.replace(b"4 2", b"2 4"), b"bad",
                        b"P6\n4 2\n255\n" + bytes(25)):
            with self.subTest(invalid=invalid[:16]):
                self.samples = [invalid]
                # Call session directly so failed sample validation retains fixture setup.
                with self.assertRaises(agent.AgentError):
                    self.session.wait_for_stable((0, 0, 4, 2), None, 100, 200, 50)
                self.assertTrue(self.files[-1].closed)
                self.assertIsNone(self.session.capture_process)

    def test_geometry_change_during_capture_raises_and_closes_session(self):
        def wait(timeout):
            self.advance(0.01)
            self.geometry["width"] = 5
            return 0

        original = self.capture
        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            process.wait.side_effect = wait
            return process

        with mock.patch.object(agent.subprocess, "Popen", side_effect=capture):
            with self.assertRaises(agent.AgentError) as error:
                self.observe()
        self.assertEqual(error.exception.code, "geometry_changed")
        self.client.close.assert_called_once()
        self.assert_clean()

    def test_invalid_wait_params_reject_before_ensure_or_capture(self):
        cases = ({"stable_ms": 49}, {"stable_ms": 2001}, {"stable_ms": True}, {"stable_ms": 1.5},
                 {"timeout_ms": 99}, {"timeout_ms": 10001}, {"timeout_ms": None},
                 {"poll_ms": 49}, {"poll_ms": 1001}, {"poll_ms": "100"},
                 {"poll_ms": 301}, {"stable_ms": 400, "timeout_ms": 300},
                 {"x": 0}, {"max_width": 0}, {"max_width": True}, {"extra": 1})
        with mock.patch.object(self.session, "ensure") as ensure:
            for params in cases:
                with self.subTest(params=params):
                    with self.assertRaises(agent.AgentError) as error:
                        self.observe(**params)
                    self.assertEqual(error.exception.code, "invalid_params")
            ensure.assert_not_called()
        self.assertEqual(self.captures, [])

    def test_out_of_bounds_roi_rejects_after_ensure_before_capture(self):
        with self.assertRaises(agent.AgentError) as error:
            self.observe(x=3, y=1, width=2, height=1)
        self.assertEqual(error.exception.code, "invalid_params")
        self.assertEqual(self.captures, [])


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.flock = mock.Mock(LOCK_EX=2, LOCK_NB=4)
        self.info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_nlink=1)
        patches = [
            mock.patch.object(agent, "fcntl", self.flock, create=True),
            mock.patch.object(agent.os, "O_NOFOLLOW", 0x20000, create=True),
            mock.patch.object(agent.os, "O_CLOEXEC", 0x80000, create=True),
            mock.patch.object(agent.os, "O_NONBLOCK", 0x800, create=True),
            mock.patch.object(agent.os, "getuid", return_value=1000, create=True),
            mock.patch.object(agent.os, "open", return_value=42),
            mock.patch.object(agent.os, "fstat", return_value=self.info),
            mock.patch.object(agent.os, "close"),
        ]
        self.patched = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)

    def test_secure_file_flags_and_descriptor_close_without_unlink(self):
        lease = agent.DesktopLease("/run/user/1000")
        args = agent.os.open.call_args.args
        self.assertEqual(Path(args[0]).name, "pi-desktop-bridge.lock")
        self.assertEqual(args[1] & agent.os.O_NOFOLLOW, agent.os.O_NOFOLLOW)
        self.assertEqual(args[1] & agent.os.O_CLOEXEC, agent.os.O_CLOEXEC)
        self.assertEqual(args[2], 0o600)
        self.flock.flock.assert_called_once_with(42, self.flock.LOCK_EX | self.flock.LOCK_NB)
        with mock.patch.object(agent.os, "unlink") as unlink:
            lease.close()
            lease.close()
        agent.os.close.assert_called_once_with(42)
        unlink.assert_not_called()

    def test_foreign_insecure_nonregular_and_hardlinked_files_are_rejected(self):
        cases = [
            {"st_uid": 1001}, {"st_mode": stat.S_IFREG | 0o644},
            {"st_mode": stat.S_IFDIR | 0o600}, {"st_nlink": 2},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_nlink=1)
                self.info.__dict__.update(change)
                agent.os.fstat.return_value = self.info
                with self.assertRaisesRegex(agent.AgentError, "lease"):
                    agent.DesktopLease("/run/user/1000")
        self.assertEqual(agent.os.close.call_count, len(cases))
        self.flock.flock.assert_not_called()

    def test_contended_lock_closes_only_attempted_descriptor(self):
        self.flock.flock.side_effect = BlockingIOError()
        with self.assertRaisesRegex(agent.AgentError, "already controlled"):
            agent.DesktopLease("/run/user/1000")
        agent.os.close.assert_called_once_with(42)


class StreamEntryTests(unittest.TestCase):
    def run_stream(self, incoming=b"START\n", ensure_error=None, relay_error=None, connect_error=None,
                   relay_signal=False, fail_ready_write=False):
        source, output = io.BytesIO(incoming), io.BytesIO()
        session, connection = mock.Mock(), mock.Mock()
        session.directory = Path("/private/owned")
        session.ensure.return_value = SimpleNamespace(width=1920, height=1080)
        connection.connect.side_effect = connect_error
        reads = []
        handlers = {}
        raised = None
        ready_write_started = False
        def ensure(**kwargs):
            self.assertEqual(source.tell(), 6)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            if ensure_error:
                raise ensure_error
            return SimpleNamespace(width=1920, height=1080)
        session.ensure.side_effect = ensure
        def read(fd, count):
            self.assertEqual(fd, 10)
            reads.append(count)
            return source.read(min(count, 2))
        def write(fd, value):
            nonlocal ready_write_started
            self.assertEqual(fd, 11)
            if fail_ready_write and output.getvalue().count(b"\n") == 1:
                if ready_write_started:
                    raise BrokenPipeError("viewer closed during readiness")
                ready_write_started = True
            # Exercise partial writes of both JSON records.
            return output.write(value[:17])
        def register(signum, handler):
            handlers[signum] = handler
            return agent.signal.SIG_DFL
        def relay(*args):
            if relay_signal:
                handlers[agent.signal.SIGTERM](agent.signal.SIGTERM, None)
            if relay_error:
                raise relay_error
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(agent.sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(fileno=lambda: 10))))
            stack.enter_context(mock.patch.object(agent.sys, "stdout", SimpleNamespace(buffer=SimpleNamespace(fileno=lambda: 11))))
            stack.enter_context(mock.patch.object(agent.sys, "stderr", io.StringIO()))
            stack.enter_context(mock.patch.object(agent.os, "get_blocking", return_value=True, create=True))
            blocking = stack.enter_context(mock.patch.object(agent.os, "set_blocking", create=True))
            stack.enter_context(mock.patch.object(agent.os, "read", side_effect=read))
            stack.enter_context(mock.patch.object(agent.os, "write", side_effect=write))
            stack.enter_context(mock.patch.object(agent.select, "select", side_effect=lambda r, w, x, timeout: (r, w, [])))
            signals = stack.enter_context(mock.patch.object(agent.signal, "signal", side_effect=register))
            stack.enter_context(mock.patch.object(agent, "OwnedWayVNC", return_value=session))
            stack.enter_context(mock.patch.object(agent.socket, "AF_UNIX", 1, create=True))
            factory = stack.enter_context(mock.patch.object(agent.socket, "socket", return_value=connection))
            relay_mock = stack.enter_context(mock.patch.object(agent, "_relay_stream", side_effect=relay))
            try:
                agent.stream_main()
            except SystemExit as exc:
                raised = exc
        return SimpleNamespace(source=source, output=output.getvalue(), session=session, connection=connection,
                               relay=relay_mock, factory=factory, blocking=blocking, signals=signals, reads=reads,
                               raised=raised)

    def test_provenance_precedes_lease_and_exact_start_preserves_pipelined_rfb(self):
        result = self.run_stream(b"START\nRFB 003.008\n")
        hello, ready = map(json.loads, result.output.splitlines())
        self.assertEqual(hello, {"mode": "rfb_stream", "protocol_version": agent.PROTOCOL_VERSION,
                               "agent_version": agent.AGENT_VERSION, "agent_sha256": agent.AGENT_SHA256})
        self.assertEqual(ready, {"ready": True, "width": 1920, "height": 1080, "max_fps": 1000})
        self.assertTrue(all(len(line) < 4096 for line in result.output.splitlines()))
        self.assertEqual(result.source.read(), b"RFB 003.008\n")
        self.assertLessEqual(max(result.reads), 6)
        result.session.ensure.assert_called_once_with(max_fps=1000, prime_capture=False)
        result.connection.connect.assert_called_once_with(str(Path("/private/owned/rfb.sock")))
        result.relay.assert_called_once_with(result.connection, 10, 11, result.session.process)
        result.connection.close.assert_called_once()
        result.session.close.assert_called_once()
        self.assertEqual(result.blocking.call_args_list, [mock.call(10, False), mock.call(11, False),
                                                         mock.call(11, True), mock.call(10, True)])
        registered = result.signals.call_args_list
        self.assertEqual(sum(call.args[1] == agent.signal.SIG_DFL for call in registered), len(registered) // 2)

    def test_invalid_or_missing_start_never_acquires_lease(self):
        for start in (b"", b"STAR", b"START\r\n", b"WRONG\n"):
            with self.subTest(start=start):
                result = self.run_stream(start)
                result.session.ensure.assert_not_called()
                result.factory.assert_not_called()
                result.relay.assert_not_called()
                error = json.loads(result.output.splitlines()[-1])["error"]
                self.assertEqual(error["code"], "invalid_request")
                result.session.close.assert_called_once()

    def test_busy_and_connection_failure_return_error_before_binary_phase(self):
        for options, code in (({"ensure_error": agent.AgentError("already controlled", code="busy")}, "busy"),
                              ({"connect_error": OSError("private path must not leak")}, "operation_failed")):
            with self.subTest(code=code):
                result = self.run_stream(**options)
                lines = result.output.splitlines()
                self.assertEqual(len(lines), 2)
                error = json.loads(lines[-1])["error"]
                self.assertEqual(error["code"], code)
                self.assertNotIn("private path", error["message"])
                result.relay.assert_not_called()
                result.session.close.assert_called_once()

    def test_relay_error_never_appends_json_to_rfb(self):
        result = self.run_stream(relay_error=agent.AgentError("stalled relay", code="timeout"))
        lines = result.output.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(json.loads(lines[-1])["ready"])
        result.connection.close.assert_called_once()
        result.session.close.assert_called_once()

    def test_signal_during_relay_closes_socket_and_owned_session(self):
        result = self.run_stream(relay_signal=True)
        self.assertIsInstance(result.raised, SystemExit)
        self.assertEqual(result.raised.code, 128 + agent.signal.SIGTERM)
        result.connection.close.assert_called_once()
        result.session.close.assert_called_once()
        self.assertEqual(len(result.output.splitlines()), 2)

    def test_partial_ready_write_never_appends_error_or_enters_relay(self):
        result = self.run_stream(fail_ready_write=True)
        self.assertEqual(result.output.count(b"\n"), 1)
        self.assertTrue(result.output.split(b"\n", 1)[1].startswith(b'{"ready":true'))
        result.relay.assert_not_called()
        result.connection.close.assert_called_once()
        result.session.close.assert_called_once()

    def test_start_deadline_and_record_limit_are_bounded(self):
        with mock.patch.object(agent.select, "select", return_value=([], [], [])), \
             mock.patch.object(agent.time, "monotonic", side_effect=[0.0, 10.0]):
            with self.assertRaisesRegex(agent.AgentError, "timed out"):
                agent._stream_read_start(10, 5.0)
        with mock.patch.object(agent.os, "write") as write:
            with self.assertRaises(agent.AgentError):
                agent._stream_json(11, {"message": "x" * 4096}, 10.0)
            write.assert_not_called()

    def test_stream_is_not_a_json_rpc_method(self):
        self.assertNotIn("stream", agent.PARAMETERS)
        self.assertNotIn("stream_main", agent.PARAMETERS)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux selectable stdio pipes")
class LinuxStreamRelayTests(unittest.TestCase):
    def setUp(self):
        self.input_read, self.input_write = os.pipe()
        self.output_read, self.output_write = os.pipe()
        self.connection, self.peer = socket.socketpair()
        self.connection.setblocking(False)
        os.set_blocking(self.input_read, False)
        os.set_blocking(self.output_write, False)
        self.peer.settimeout(3)
        self.process = SimpleNamespace(poll=lambda: None)
        self.errors = []
        self.worker = None

    def tearDown(self):
        # Closing the producer ends wakes every successful relay test.
        self.peer.close()
        if self.input_write is not None:
            os.close(self.input_write)
        if self.worker is not None:
            self.worker.join(3)
            self.assertFalse(self.worker.is_alive(), "relay did not stop")
        self.connection.close()
        for fd in (self.input_read, self.output_read, self.output_write):
            os.close(fd)

    def start_relay(self):
        def run():
            try:
                agent._relay_stream(self.connection, self.input_read, self.output_write, self.process)
            except Exception as exc:
                self.errors.append(exc)
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def test_bidirectional_payload_exceeds_chunk_size_and_eof_stops(self):
        self.start_relay()
        payload = bytes(range(256)) * 1024
        # Exchange in both directions repeatedly; pipes enforce real backpressure.
        for offset in range(0, len(payload), 4096):
            part = payload[offset:offset + 4096]
            self.assertEqual(os.write(self.input_write, part), len(part))
            received = bytearray()
            while len(received) < len(part):
                piece = self.peer.recv(len(part) - len(received))
                self.assertTrue(piece, "relay closed before forwarding input")
                received.extend(piece)
            self.assertEqual(received, part)
            self.peer.sendall(part[::-1])
            received.clear()
            while len(received) < len(part):
                readable, _, _ = agent.select.select([self.output_read], [], [], 3)
                self.assertTrue(readable, "relay did not forward server output")
                piece = os.read(self.output_read, len(part) - len(received))
                self.assertTrue(piece, "relay closed before forwarding server output")
                received.extend(piece)
            self.assertEqual(received, part[::-1])
        os.close(self.input_write)
        self.input_write = None
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.errors, [])

    def test_blocked_output_times_out_with_bounded_buffer(self):
        self.peer.setblocking(False)
        with mock.patch.object(agent, "IO_TIMEOUT", 0.1):
            self.start_relay()
            sent = 0
            deadline = time.monotonic() + 2
            while self.worker.is_alive() and time.monotonic() < deadline:
                try:
                    sent += self.peer.send(b"x" * 65536)
                except BlockingIOError:
                    time.sleep(0.01)
            self.worker.join(1)
        self.assertFalse(self.worker.is_alive())
        self.assertGreater(sent, 65536)
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], agent.AgentError)
        self.assertEqual(self.errors[0].code, "timeout")

    def test_owned_process_exit_stops_idle_relay(self):
        self.process = SimpleNamespace(poll=lambda: 1)
        self.start_relay()
        self.worker.join(1)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], agent.AgentError)

    def test_blocked_wayvnc_times_out_without_reading_unbounded_input(self):
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        os.set_blocking(self.input_write, False)
        with mock.patch.object(agent, "IO_TIMEOUT", 0.1):
            self.start_relay()
            deadline = time.monotonic() + 2
            while self.worker.is_alive() and time.monotonic() < deadline:
                try:
                    os.write(self.input_write, b"x" * 65536)
                except BlockingIOError:
                    time.sleep(0.01)
            self.worker.join(1)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], agent.AgentError)
        self.assertEqual(self.errors[0].code, "timeout")

    def test_wayvnc_eof_stops_idle_relay(self):
        self.start_relay()
        self.peer.shutdown(socket.SHUT_WR)
        self.worker.join(1)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.errors, [])


@unittest.skipUnless(sys.platform.startswith("linux"), "requires real Linux resource limits")
class LinuxCaptureLimitTests(unittest.TestCase):
    def test_capture_child_cannot_grow_sample_file_past_limit(self):
        parent_limit = agent.resource.getrlimit(agent.resource.RLIMIT_FSIZE)
        with tempfile.TemporaryFile() as output, mock.patch.object(agent, "MAX_PPM_BYTES", 1024):
            process = subprocess.Popen(
                [sys.executable, "-c", "import os; [os.write(1, b'x'*512) for _ in range(10)]"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                preexec_fn=agent._limit_capture_file,
            )
            try:
                self.assertNotEqual(process.wait(timeout=5), 0)
                self.assertEqual(os.fstat(output.fileno()).st_size, 1024)
                self.assertEqual(stat.S_IMODE(os.fstat(output.fileno()).st_mode), 0o600)
                self.assertEqual(agent.resource.getrlimit(agent.resource.RLIMIT_FSIZE), parent_limit)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires real Linux flock")
class LinuxLeaseTests(unittest.TestCase):
    def test_exclusive_lock_persists_inode_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as runtime:
            first = agent.DesktopLease(runtime)
            self.addCleanup(first.close)
            path = Path(runtime) / "pi-desktop-bridge.lock"
            inode = path.stat().st_ino
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaisesRegex(agent.AgentError, "already controlled"):
                agent.DesktopLease(runtime)
            first.close()
            second = agent.DesktopLease(runtime)
            second.close()
            self.assertEqual(path.stat().st_ino, inode)

    def test_lock_released_by_kernel_when_child_is_killed(self):
        with tempfile.TemporaryDirectory() as runtime:
            script = ("import sys,time; sys.path.insert(0, sys.argv[1]); "
                      "from pi_desktop_bridge.pi_agent import DesktopLease; "
                      "lease=DesktopLease(sys.argv[2]); print('ready',flush=True); time.sleep(30)")
            child = subprocess.Popen([sys.executable, "-c", script, str(Path(agent.__file__).parents[1]), runtime], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(agent.AgentError, "already controlled"):
                    agent.DesktopLease(runtime)
                child.kill()
                child.wait(timeout=5)
                lease = agent.DesktopLease(runtime)
                lease.close()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdout.close()

    def test_symlink_lease_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as runtime:
            target = Path(runtime) / "unrelated"
            target.write_text("preserve")
            (Path(runtime) / "pi-desktop-bridge.lock").symlink_to(target)
            with self.assertRaises(agent.AgentError):
                agent.DesktopLease(runtime)
            self.assertEqual(target.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
