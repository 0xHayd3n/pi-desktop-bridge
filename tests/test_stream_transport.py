"""Provenance, byte framing, SSH flow control, and stream worker teardown."""

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from pi_desktop_bridge import PROTOCOL_VERSION
from pi_desktop_bridge import stream_transport as stream_module
from pi_desktop_bridge.transport import AGENT_BOOTSTRAP, EXPECTED_AGENT_SHA256


def record(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


HELLO = {"mode": "rfb_stream", "protocol_version": PROTOCOL_VERSION,
         "agent_version": "0.6.1", "agent_sha256": EXPECTED_AGENT_SHA256}
READY = {"ready": True, "width": 1920, "height": 1080, "max_fps": 1000}
BANNER = b"RFB 003.008\n\x00\xffbinary\n"


class MemoryChannel:
    """A byte channel with explicit abort state, independent of JSON framing."""

    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False
        self.aborted = threading.Event()
        self.transport = SimpleNamespace(close=mock.Mock(side_effect=self.aborted.set))

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, count):
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data

    def recv_stderr(self, count):
        return b""

    def send(self, value):
        if self.closed or self.aborted.is_set():
            raise OSError("closed transport")
        self.sent.extend(value)
        return len(value)

    def get_transport(self):
        return self.transport

    def shutdown_write(self):
        pass

    def exit_status_ready(self):
        return True

    def close(self):
        self.closed = True


def receive_bytes(stream, length, timeout=3):
    data = bytearray()
    deadline = time.monotonic() + timeout
    while len(data) < length and time.monotonic() < deadline:
        part = stream.recv()
        if part is None:
            continue
        if not part:
            raise AssertionError("stream ended before the expected bytes")
        data.extend(part)
    if len(data) != length:
        raise AssertionError(f"expected {length} bytes, received {len(data)}")
    return bytes(data)


class StreamHandshakeTests(unittest.TestCase):
    def make_stream(self, incoming):
        channel = MemoryChannel(incoming)
        stream = stream_module.DesktopStream(channel=channel)
        self.addCleanup(stream.close)
        return stream, channel

    def test_hello_and_ready_coalesced_with_binary_preserve_exact_prefix(self):
        stream, channel = self.make_stream(record(HELLO) + record(READY) + BANNER)
        self.assertIs(stream.negotiate(time.monotonic() + 1), stream)
        self.assertEqual(channel.sent, b"START\n")
        self.assertEqual((stream.width, stream.height, stream.max_fps), (1920, 1080, 1000))
        self.assertEqual(stream.recv(), BANNER)
        self.assertEqual(stream.recv(), b"")

    def test_untrusted_hello_never_sends_start(self):
        bad_values = [
            {**HELLO, "agent_sha256": "0" * 64},
            {**HELLO, "protocol_version": True},
            {**HELLO, "protocol_version": PROTOCOL_VERSION + 1},
            {**HELLO, "mode": "json"},
            {**HELLO, "agent_version": 6},
            {**HELLO, "socket": "/browser-controlled"},
            {key: value for key, value in HELLO.items() if key != "agent_sha256"},
        ]
        for hello in bad_values:
            with self.subTest(hello=hello):
                stream, channel = self.make_stream(record(hello))
                with self.assertRaises(stream_module.StreamError) as caught:
                    stream.negotiate(time.monotonic() + 1)
                self.assertEqual(caught.exception.code, "agent_source_mismatch")
                self.assertEqual(channel.sent, b"")

    def test_ready_requires_exact_schema_types_dimensions_and_fps(self):
        bad_values = [
            {**READY, "ready": 1}, {**READY, "width": True},
            {**READY, "height": "1080"}, {**READY, "width": 0},
            {**READY, "height": 65536}, {**READY, "width": 4096, "height": 4097},
            {**READY, "max_fps": 1000.0}, {**READY, "max_fps": 60},
            {**READY, "extra": True}, {key: value for key, value in READY.items() if key != "height"},
        ]
        for ready in bad_values:
            with self.subTest(ready=ready):
                stream, channel = self.make_stream(record(HELLO) + record(ready))
                with self.assertRaises(stream_module.StreamError):
                    stream.negotiate(time.monotonic() + 1)
                self.assertEqual(channel.sent, b"START\n")
                self.assertEqual((stream.width, stream.height), (0, 0))

    def test_ready_accepts_exact_pixel_limit_and_single_pixel_desktop(self):
        for width, height in ((4096, 4096), (1, 1)):
            with self.subTest(dimensions=(width, height)):
                stream, _ = self.make_stream(record(HELLO) + record({**READY, "width": width, "height": height}))
                stream.negotiate(time.monotonic() + 1)
                self.assertEqual((stream.width, stream.height), (width, height))

    def test_startup_error_codes_are_allowlisted_without_exposing_details(self):
        for remote_code, expected in (("busy", "busy"), ("timeout", "timeout"),
                                      ("private-server-detail", "stream_failed")):
            stream, _ = self.make_stream(record(HELLO) + record({"error": {
                "code": remote_code, "message": "secret-path-and-password"}}))
            with self.assertRaises(stream_module.StreamError) as caught:
                stream.negotiate(time.monotonic() + 1)
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("secret", str(caught.exception))

    def test_header_limit_counts_newline_and_rejects_unframed_data(self):
        # A valid JSON object padded to exactly the advertised record boundary.
        permitted = b'{}' + b' ' * 4093 + b'\n'
        self.assertEqual(len(permitted), 4096)
        stream, _ = self.make_stream(permitted + BANNER)
        self.assertEqual(stream._json_line(time.monotonic() + 1), {})
        self.assertEqual(stream.recv(), BANNER)
        for data in (b'{}' + b' ' * 4094 + b'\n', b'x' * 4097, b'[]\n', b'\xff\n', b'{bad}\n', b''):
            with self.subTest(data=data[:20]):
                stream, _ = self.make_stream(data)
                with self.assertRaises(stream_module.StreamError):
                    stream._json_line(time.monotonic() + 1)

    def test_fragmented_header_and_idle_deadline(self):
        stream, channel = self.make_stream(b"")
        incoming = iter([None, b'{"ok"', b':true}\n', BANNER])
        channel.recv = lambda _count: next(incoming)
        self.assertEqual(stream._json_line(time.monotonic() + 1), {"ok": True})
        self.assertEqual(stream.recv(), BANNER)
        channel.recv = lambda _count: None
        started = time.monotonic()
        with self.assertRaises(stream_module.StreamError):
            stream._json_line(started + 0.02)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_gui_factory_runs_only_fixed_stream_bootstrap_and_cleans_mismatch(self):
        from pi_desktop_bridge.gui_transport import GUITransport
        for digest in (EXPECTED_AGENT_SHA256, "0" * 64):
            channel = MemoryChannel(record({**HELLO, "agent_sha256": digest}) + record(READY))
            transport = mock.Mock()
            transport.is_active.return_value = True
            transport.open_session.return_value = channel
            bridge = GUITransport("pi", SimpleNamespace(get_transport=lambda: transport))
            with mock.patch("pi_desktop_bridge.gui_transport._exec_command_until") as execute:
                if digest == EXPECTED_AGENT_SHA256:
                    stream = stream_module.open_desktop_stream(bridge)
                    stream.close()
                else:
                    with self.assertRaises(stream_module.StreamError):
                        stream_module.open_desktop_stream(bridge)
                    self.assertEqual(channel.sent, b"")
                expected = AGENT_BOOTSTRAP.replace("namespace['main']()", "namespace['stream_main']()")
                self.assertEqual(execute.call_args.args[1], "python3 -u -c " + shlex.quote(expected))
                self.assertTrue(channel.closed)

    def test_startup_result_racing_with_deadline_is_closed_and_never_returned(self):
        from pi_desktop_bridge.gui_transport import GUITransport
        aborted = threading.Event()
        transport = SimpleNamespace(close=aborted.set)
        bridge = GUITransport("pi", SimpleNamespace(get_transport=lambda: transport))
        candidate = mock.Mock()
        baseline = set(threading.enumerate())
        def start(_bridge, _owned):
            if not aborted.wait(1):
                raise AssertionError("startup transport was not aborted")
            return candidate
        with mock.patch.object(stream_module, "START_TIMEOUT", 0.03), \
             mock.patch.object(stream_module, "_open_desktop_stream", side_effect=start):
            with self.assertRaises(stream_module.StreamError):
                stream_module.open_desktop_stream(bridge)
        candidate.close.assert_called_once()
        self.assertFalse([worker for worker in set(threading.enumerate()) - baseline
                          if worker.name == "pi-stream-start" and worker.is_alive()])


class StreamWriteTests(unittest.TestCase):
    def make_stream(self, channel):
        stream = stream_module.DesktopStream(channel=channel)
        self.addCleanup(stream.close)
        return stream

    def test_partial_sends_preserve_bytes_and_invalid_payloads_never_send(self):
        channel = MemoryChannel()
        normal_send = channel.send
        channel.send = lambda data: normal_send(data[:3])
        stream = self.make_stream(channel)
        payload = bytes(range(256))
        stream.send(payload)
        self.assertEqual(channel.sent, payload)
        for value in (b"", "text", bytearray(b"x"), b"x" * 65537):
            with self.subTest(value_type=type(value)), self.assertRaises(stream_module.StreamError):
                stream.send(value)
        self.assertEqual(channel.sent, payload)

    def test_partial_send_failure_aborts_transport_and_prevents_later_input(self):
        import paramiko
        for failure in (socket.timeout, paramiko.SSHException):
            with self.subTest(failure=failure):
                channel = MemoryChannel()
                calls = []
                def send(data):
                    calls.append(bytes(data))
                    if len(calls) == 1:
                        channel.sent.extend(data[:2])
                        return 2
                    raise failure("remote private diagnostics")
                channel.send = send
                stream = self.make_stream(channel)
                with self.assertRaises(stream_module.StreamError) as caught:
                    stream.send(b"input-event")
                channel.transport.close.assert_called_once()
                self.assertTrue(channel.closed)
                with self.assertRaises(stream_module.StreamError):
                    stream.send(b"late-input")
                self.assertEqual(len(calls), 2)
                self.assertNotIn("private", str(caught.exception))
                self.assertTrue(all(not worker.is_alive() for worker in stream._threads))

    def test_total_write_budget_is_not_reset_by_partial_progress(self):
        channel = MemoryChannel()
        normal_send = channel.send
        def send(data):
            time.sleep(0.02)
            return normal_send(data[:1])
        channel.send = send
        stream = self.make_stream(channel)
        before = set(threading.enumerate())
        started = time.monotonic()
        with mock.patch.object(stream_module, "WRITE_TIMEOUT", 0.06):
            with self.assertRaises(stream_module.StreamError):
                stream.send(b"x" * 64)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertLess(len(channel.sent), 64)
        channel.transport.close.assert_called_once()
        sent = bytes(channel.sent)
        time.sleep(0.05)
        self.assertEqual(channel.sent, sent)
        self.assertFalse([worker for worker in set(threading.enumerate()) - before
                          if worker.name == "pi-stream-write" and worker.is_alive()])

    def test_blocked_write_is_unblocked_by_transport_abort_and_joined(self):
        channel = MemoryChannel()
        finished = threading.Event()
        def send(_data):
            try:
                if not channel.aborted.wait(1):
                    raise AssertionError("send worker was not aborted")
                raise EOFError("transport closed")
            finally:
                finished.set()
        channel.send = send
        stream = self.make_stream(channel)
        with mock.patch.object(stream_module, "WRITE_TIMEOUT", 0.03):
            with self.assertRaises(stream_module.StreamError):
                stream.send(b"input")
        self.assertTrue(finished.is_set())
        channel.transport.close.assert_called_once()
        self.assertTrue(channel.closed)


class AliasPipeTests(unittest.TestCase):
    def make_process(self, *, flood=False, stall_input=False, ignore_term=False):
        script = "import os,sys\n"
        if ignore_term:
            script += "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        script += (
            "sys.stderr.buffer.write(b'diagnostics'*10000); sys.stderr.buffer.flush()\n"
            f"sys.stdout.buffer.write({record(HELLO)!r}); sys.stdout.buffer.flush()\n"
            "assert sys.stdin.buffer.read(6) == b'START\\n'\n"
            f"sys.stdout.buffer.write({record(READY) + BANNER!r}); sys.stdout.buffer.flush()\n"
        )
        if flood:
            script += "sys.stdout.buffer.write(b'x'*1048576); sys.stdout.buffer.flush()\n"
        if stall_input:
            script += "import time; time.sleep(30)\n"
        script += (
            "while True:\n"
            " data=os.read(0,65536)\n"
            " if not data: break\n"
            " sys.stdout.buffer.write(data); sys.stdout.buffer.flush()\n"
        )
        process = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(self.stop_process, process)
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if not pipe.closed:
                pipe.close()

    def test_real_byte_pipes_preserve_raw_prefix_and_binary_echo_and_join_readers(self):
        process = self.make_process()
        stream = stream_module.DesktopStream(process=process)
        self.addCleanup(stream.close)
        stream.negotiate(time.monotonic() + 5)
        self.assertEqual(receive_bytes(stream, len(BANNER)), BANNER)
        payload = bytes(range(256)) * 256
        stream.send(payload)
        self.assertEqual(receive_bytes(stream, len(payload)), payload)
        stream.close()
        stream.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(all(not worker.is_alive() for worker in stream._threads))
        self.assertTrue(all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr)))

    def test_alias_reader_queue_stays_bounded_and_close_releases_blocked_producer(self):
        process = self.make_process(flood=True)
        stream = stream_module.DesktopStream(process=process)
        self.addCleanup(stream.close)
        stream.negotiate(time.monotonic() + 5)
        deadline = time.monotonic() + 2
        while stream._queue.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(stream._queue.qsize(), 2)
        with stream._queue.mutex:
            buffered = tuple(stream._queue.queue)
        self.assertLessEqual(sum(map(len, buffered)), 2 * 65536)
        self.assertTrue(any(worker.is_alive() for worker in stream._threads))
        started = time.monotonic()
        stream.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(process.poll())
        self.assertTrue(all(not worker.is_alive() for worker in stream._threads))

    def test_alias_blocked_input_write_terminates_process_and_joins_all_workers(self):
        process = self.make_process(stall_input=True)
        baseline = set(threading.enumerate())
        stream = stream_module.DesktopStream(process=process)
        self.addCleanup(stream.close)
        stream.negotiate(time.monotonic() + 5)
        self.assertEqual(receive_bytes(stream, len(BANNER)), BANNER)
        started = time.monotonic()
        with mock.patch.object(stream_module, "WRITE_TIMEOUT", 0.03):
            with self.assertRaises(stream_module.StreamError):
                # Enough for even a large OS pipe to fill without a consumer.
                for _ in range(32):
                    stream.send(b"x" * 65536)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(process.poll())
        self.assertFalse([worker for worker in set(threading.enumerate()) - baseline
                          if worker.name.startswith("pi-stream-") and worker.is_alive()])

    def test_external_close_releases_alias_write_before_its_own_deadline(self):
        process = self.make_process(stall_input=True)
        baseline = set(threading.enumerate())
        stream = stream_module.DesktopStream(process=process)
        self.addCleanup(stream.close)
        stream.negotiate(time.monotonic() + 5)
        self.assertEqual(receive_bytes(stream, len(BANNER)), BANNER)
        entered, errors = threading.Event(), []
        original_write = stream_module.os.write
        def write(fd, value):
            entered.set()
            return original_write(fd, value)
        def send():
            try:
                for _ in range(32):
                    stream.send(b"x" * 65536)
            except Exception as exc:
                errors.append(exc)
        caller = threading.Thread(target=send, name="fixture-input-caller", daemon=True)
        with mock.patch.object(stream_module, "WRITE_TIMEOUT", 10), \
             mock.patch.object(stream_module.os, "write", side_effect=write):
            caller.start()
            try:
                self.assertTrue(entered.wait(1), "write worker never entered the pipe write")
                started = time.monotonic()
                stream.close()
                self.assertLess(time.monotonic() - started, 3)
            finally:
                stream.close()
                caller.join(timeout=3)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], stream_module.StreamError)
        self.assertIsNotNone(process.poll())
        self.assertFalse([worker for worker in set(threading.enumerate()) - baseline
                          if worker.name.startswith("pi-stream-") and worker.is_alive()])

    @unittest.skipUnless(os.name == "posix", "requires a child that can ignore SIGTERM")
    def test_posix_stalled_write_kills_sigterm_ignoring_child_and_closes_stream(self):
        process = self.make_process(stall_input=True, ignore_term=True)
        baseline = set(threading.enumerate())
        stream = stream_module.DesktopStream(process=process)
        try:
            stream.negotiate(time.monotonic() + 5)
            self.assertEqual(receive_bytes(stream, len(BANNER)), BANNER)
            started = time.monotonic()
            with mock.patch.object(stream_module, "WRITE_TIMEOUT", 0.03):
                with self.assertRaises(stream_module.StreamError):
                    for _ in range(32):
                        stream.send(b"x" * 65536)
            self.assertLess(time.monotonic() - started, 6)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertTrue(stream._closed.is_set())
            self.assertTrue(all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr)))
            self.assertFalse([worker for worker in set(threading.enumerate()) - baseline
                              if worker.name.startswith("pi-stream-") and worker.is_alive()])
        finally:
            # A regression must not leave this deliberately resistant child alive.
            self.stop_process(process)
            stream.close()


class ParamikoWindowTests(unittest.TestCase):
    def make_pair(self):
        import paramiko
        client_socket, server_socket = socket.socketpair()
        client = paramiko.Transport(client_socket)
        server = paramiko.Transport(server_socket)
        self.addCleanup(client.close)
        self.addCleanup(server.close)

        class Interface(paramiko.ServerInterface):
            def check_auth_none(self, username):
                return paramiko.AUTH_SUCCESSFUL

            def check_channel_request(self, kind, channel_id):
                return paramiko.OPEN_SUCCEEDED

        server.add_server_key(paramiko.RSAKey.generate(1024))
        server.start_server(event=threading.Event(), server=Interface())
        client.start_client(timeout=3)
        client.auth_none("fixture")
        channel = client.open_session(window_size=32768, max_packet_size=32768, timeout=3)
        remote = server.accept(timeout=3)
        self.assertIsNotNone(remote)
        self.addCleanup(remote.close)
        remote.settimeout(3)
        self.assertEqual(channel.in_window_size, 32768)
        return client, channel, remote

    def assert_stream_workers_stopped(self, baseline):
        self.assertFalse([worker for worker in set(threading.enumerate()) - baseline
                          if worker.name.startswith("pi-stream-") and worker.is_alive()])

    def test_stderr_larger_than_32k_window_does_not_starve_rfb(self):
        _client, channel, remote = self.make_pair()
        errors, acknowledgements = [], []
        complete = threading.Event()

        def produce():
            try:
                # Paramiko shares stdout/stderr flow control for a channel.
                remote.sendall_stderr(b"diagnostic\n" * 5000)
                remote.sendall(record(HELLO))
                ack = bytearray()
                while len(ack) < 6:
                    piece = remote.recv(6 - len(ack))
                    if not piece:
                        raise AssertionError("client closed before START")
                    ack.extend(piece)
                acknowledgements.append(bytes(ack))
                remote.sendall(record(READY) + BANNER)
                remote.send_exit_status(0)
            except Exception as exc:
                errors.append(exc)
            finally:
                complete.set()

        writer = threading.Thread(target=produce, name="fixture-ssh-producer", daemon=True)
        stream = stream_module.DesktopStream(channel=channel)
        self.addCleanup(stream.close)
        writer.start()
        try:
            stream.negotiate(time.monotonic() + 5)
            self.assertEqual(receive_bytes(stream, len(BANNER)), BANNER)
            self.assertTrue(complete.wait(3), "stderr window never reopened")
            self.assertEqual(acknowledgements, [b"START\n"])
            self.assertEqual(errors, [])
        finally:
            stream.close()
            remote.close()
            writer.join(timeout=3)
        self.assertFalse(writer.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in stream._threads))

    def test_rekey_stall_during_close_aborts_transport_and_joins_workers(self):
        client, channel, _remote = self.make_pair()
        baseline = set(threading.enumerate())
        stream = stream_module.DesktopStream(channel=channel)
        self.addCleanup(stream.close)
        # Exercise Paramiko's real user-message gate, which ignores channel
        # socket timeouts while waiting for SSH key negotiation.
        client.clear_to_send.clear()
        started = time.monotonic()
        with mock.patch.object(stream_module, "CLOSE_TIMEOUT", 0.06):
            stream.close()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(client.is_active())
        self.assertTrue(channel.closed)
        self.assert_stream_workers_stopped(baseline)

    def test_rekey_stall_during_send_never_delivers_input_after_timeout(self):
        client, channel, remote = self.make_pair()
        baseline = set(threading.enumerate())
        stream = stream_module.DesktopStream(channel=channel)
        self.addCleanup(stream.close)
        client.clear_to_send.clear()
        started = time.monotonic()
        with mock.patch.object(stream_module, "WRITE_TIMEOUT", 0.06):
            with self.assertRaises(stream_module.StreamError):
                stream.send(b"must-not-arrive-later")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(client.is_active())
        client.clear_to_send.set()
        self.assertEqual(remote.recv(65536), b"")
        with self.assertRaises(stream_module.StreamError):
            stream.send(b"late-input")
        self.assert_stream_workers_stopped(baseline)

    def test_rekey_stall_before_open_session_timeout_is_bounded_by_start_budget(self):
        from pi_desktop_bridge.gui_transport import GUITransport
        client, _channel, _remote = self.make_pair()
        baseline = set(threading.enumerate())
        bridge = GUITransport("pi", SimpleNamespace(get_transport=lambda: client))
        client.clear_to_send.clear()
        started = time.monotonic()
        with mock.patch.object(stream_module, "START_TIMEOUT", 0.06):
            with self.assertRaises(stream_module.StreamError):
                stream_module.open_desktop_stream(bridge)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(client.is_active())
        self.assert_stream_workers_stopped(baseline)


if __name__ == "__main__":
    unittest.main()
