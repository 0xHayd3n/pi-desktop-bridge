"""Bounded raw RFB channel through the authenticated GUI SSH connection."""

from __future__ import annotations

import json
import os
import queue
import shlex
import socket
import subprocess
import threading
import time

from . import PROTOCOL_VERSION
from .transport import AGENT_BOOTSTRAP, EXPECTED_AGENT_SHA256, _ssh_environment, ssh_argv


CHUNK_SIZE = 65_536
WRITE_TIMEOUT = 5.0
CLOSE_TIMEOUT = 2.5
START_TIMEOUT = 25.0
STREAM_BOOTSTRAP = AGENT_BOOTSTRAP.replace("namespace['main']()", "namespace['stream_main']()")


class StreamError(RuntimeError):
    def __init__(self, code="stream_failed"):
        super().__init__("Desktop stream could not be established or continued.")
        self.code = code


def _terminate_process(process):
    """End a blocked pipe owner even when a POSIX child ignores SIGTERM."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)
    except ProcessLookupError:
        pass


class DesktopStream:
    """One raw channel with bounded read-ahead and finite writes/teardown."""

    def __init__(self, *, process=None, channel=None):
        if (process is None) == (channel is None):
            raise ValueError("provide one stream endpoint")
        self.process, self.channel = process, channel
        self.width = self.height = 0
        self.max_fps = 1000
        self._pending = b""
        self._closed = threading.Event()
        self._closing = threading.Event()
        self._close_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._writing = threading.Event()
        self._queue = queue.Queue(maxsize=2)
        self._threads = []
        if channel is not None:
            channel.settimeout(0.5)
            thread = threading.Thread(target=self._drain_channel_stderr,
                                      name="pi-stream-stderr", daemon=True)
            self._threads.append(thread)
            thread.start()
        else:
            for target, name in ((self._read_output, "pi-stream-read"), (self._drain_stderr, "pi-stream-stderr")):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._threads.append(thread)
                thread.start()

    def _read_output(self):
        try:
            while not self._closed.is_set():
                data = os.read(self.process.stdout.fileno(), CHUNK_SIZE)
                if not data:
                    break
                while not self._closing.is_set():
                    try:
                        self._queue.put(data, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError):
            pass
        finally:
            while not self._closing.is_set():
                try:
                    self._queue.put(b"", timeout=0.1)
                    break
                except queue.Full:
                    continue

    def _drain_stderr(self):
        try:
            while os.read(self.process.stderr.fileno(), 4096):
                pass  # Never log remote diagnostics or submitted credentials.
        except (OSError, ValueError):
            pass

    def _drain_channel_stderr(self):
        while not self._closed.is_set():
            try:
                if not self.channel.recv_stderr(4096):
                    return
            except socket.timeout:
                continue
            except (OSError, EOFError, ValueError):
                return

    def recv(self):
        if self._closing.is_set():
            return b""
        if self._pending:
            data, self._pending = self._pending, b""
            return data
        try:
            if self.channel is not None:
                return self.channel.recv(CHUNK_SIZE)
            return self._queue.get(timeout=0.5)
        except (socket.timeout, queue.Empty):
            return None
        except (OSError, EOFError, ValueError) as exc:
            raise StreamError() from exc

    def send(self, data):
        if not isinstance(data, bytes) or not 0 < len(data) <= CHUNK_SIZE:
            raise StreamError()
        with self._send_lock:
            if self._closing.is_set():
                raise StreamError()
            finished = threading.Event()
            errors = []

            def write():
                try:
                    view = memoryview(data)
                    while view:
                        if self._closing.is_set():
                            raise OSError("closed stream")
                        written = (self.channel.send(view) if self.channel is not None
                                   else os.write(self.process.stdin.fileno(), view))
                        if written <= 0:
                            raise OSError("closed pipe")
                        view = view[written:]
                except Exception as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            worker = threading.Thread(target=write, name="pi-stream-write", daemon=True)
            self._writing.set()
            worker.start()
            if not finished.wait(WRITE_TIMEOUT):
                # Closing the underlying SSH transport also ends rekey/window
                # waits, preventing a stalled write from delivering input later.
                if self.channel is not None:
                    self.channel.get_transport().close()
                else:
                    _terminate_process(self.process)
                self.close()
                worker.join(timeout=1.0)
                self._writing.clear()
                raise StreamError()
            worker.join()
            self._writing.clear()
            if errors:
                if self.channel is not None:
                    self.channel.get_transport().close()
                self.close()
                raise StreamError() from errors[0]

    def _json_line(self, deadline):
        pending = bytearray()
        while time.monotonic() < deadline:
            data = self.recv()
            if data is None:
                continue
            if not data:
                raise StreamError()
            pending.extend(data)
            newline = pending.find(b"\n")
            if newline >= 0:
                if newline >= 4096:
                    raise StreamError()
                self._pending = bytes(pending[newline + 1:])
                try:
                    value = json.loads(pending[:newline])
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise StreamError() from exc
                if type(value) is not dict:
                    raise StreamError()
                return value
            if len(pending) > 4096:
                raise StreamError()
        raise StreamError()

    def negotiate(self, deadline):
        hello = self._json_line(deadline)
        if (set(hello) != {"mode", "protocol_version", "agent_version", "agent_sha256"}
                or hello.get("mode") != "rfb_stream"
                or type(hello.get("protocol_version")) is not int
                or hello["protocol_version"] != PROTOCOL_VERSION
                or hello.get("agent_sha256") != EXPECTED_AGENT_SHA256
                or not isinstance(hello.get("agent_version"), str)):
            raise StreamError("agent_source_mismatch")
        self.send(b"START\n")  # Never acquire a stream lease before checking its source.
        ready = self._json_line(deadline)
        if "error" in ready:
            error = ready["error"]
            code = error.get("code") if type(error) is dict else None
            raise StreamError(code if code in {"busy", "missing_dependency", "geometry_changed", "timeout"} else "stream_failed")
        width, height = ready.get("width"), ready.get("height")
        if (set(ready) != {"ready", "width", "height", "max_fps"} or ready.get("ready") is not True
                or type(width) is not int or type(height) is not int
                or not 1 <= width <= 65535 or not 1 <= height <= 65535
                or width * height > 16_777_216 or type(ready.get("max_fps")) is not int
                or ready["max_fps"] != 1000):
            raise StreamError()
        self.width, self.height = width, height
        return self

    def close(self):
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closing.set()
            if self.channel is not None:
                finished = threading.Event()

                def graceful_close():
                    try:
                        self.channel.shutdown_write()
                        deadline = time.monotonic() + 2.0
                        while not self.channel.exit_status_ready() and time.monotonic() < deadline:
                            try:
                                self.channel.recv(CHUNK_SIZE)
                            except (socket.timeout, OSError, EOFError):
                                pass
                    except Exception:
                        pass
                    finally:
                        try:
                            self.channel.close()
                        except Exception:
                            pass
                        finished.set()

                worker = threading.Thread(target=graceful_close, name="pi-stream-close", daemon=True)
                worker.start()
                if not finished.wait(CLOSE_TIMEOUT):
                    # EOF/close messages can also wait for SSH rekey. Terminate
                    # that transport before joining, rather than leaving owned
                    # input or a cleanup worker behind.
                    self.channel.get_transport().close()
                worker.join(timeout=1.0)
            else:
                # On Windows FileIO.close can wait behind an active os.write.
                # End the child first so closing its stdin cannot defeat the
                # disconnect/write deadline while that pipe is backpressured.
                if self._writing.is_set() and self.process.poll() is None:
                    _terminate_process(self.process)
                try:
                    self.process.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    _terminate_process(self.process)
                for pipe in (self.process.stdout, self.process.stderr):
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
            self._closed.set()
            for thread in self._threads:
                thread.join(timeout=1.0)


def _open_desktop_stream(bridge, owned):
    """Start only the fixed agent stream entry; never accept a shell/path from the browser."""
    from .gui_transport import GUITransport, _exec_command_until

    deadline = time.monotonic() + START_TIMEOUT
    stream = None
    channel = None
    try:
        command = "python3 -u -c " + shlex.quote(STREAM_BOOTSTRAP)
        if isinstance(bridge, GUITransport):
            client = bridge._client
            transport = client.get_transport() if client is not None else None
            if transport is None or not transport.is_active():
                raise StreamError()
            channel = transport.open_session(timeout=8.0)
            _exec_command_until(channel, command, deadline)
            stream = DesktopStream(channel=channel)
        else:
            process = subprocess.Popen(ssh_argv(bridge.host, command), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=_ssh_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            stream = DesktopStream(process=process)
        owned.append(stream)
        return stream.negotiate(deadline)
    except Exception as exc:
        if stream is not None:
            stream.close()
        elif channel is not None:
            channel.close()
        if isinstance(exc, StreamError):
            raise
        raise StreamError() from exc


def open_desktop_stream(bridge):
    """Bound the entire setup, including SSH rekey waits before channel timeouts."""
    from .gui_transport import GUITransport

    finished = threading.Event()
    owned, result, errors = [], [], []

    def start():
        try:
            result.append(_open_desktop_stream(bridge, owned))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=start, name="pi-stream-start", daemon=True)
    worker.start()
    if not finished.wait(START_TIMEOUT):
        if isinstance(bridge, GUITransport) and bridge._client is not None:
            transport = bridge._client.get_transport()
            if transport is not None:
                transport.close()
        if owned:
            owned[0].close()
        worker.join(timeout=5.0)
        # A result racing with expiry is never attached to the browser.
        if result:
            result[0].close()
        raise StreamError()
    worker.join()
    if errors:
        raise errors[0]
    return result[0]
