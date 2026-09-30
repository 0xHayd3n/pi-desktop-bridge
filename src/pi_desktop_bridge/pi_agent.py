"""Standalone Pi desktop agent. Run with Python 3 over an SSH stdio channel.

No third-party imports, TCP listener, shell commands, or existing VNC sessions.
The owned WayVNC process exposes just one output of the logged-in Wayland user.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib

try:
    import fcntl
except ImportError:  # The controller and unit tests can import this on Windows.
    fcntl = None

try:
    import resource
except ImportError:  # Only the Linux capture child uses resource limits.
    resource = None


MAX_PIXELS = 16_777_216
MAX_TEXT_BYTES = 1_048_576
IO_TIMEOUT = 10.0
MAX_REQUEST_BYTES = 65_536
STREAM_CHUNK_BYTES = 65_536
# WayVNC 0.9.1's ext-image capture schedules from completion and subtracts
# 4 ms from this interval. A low ceiling can miss every other 60 Hz refresh.
# Request changed frames promptly; actual cadence follows the compositor,
# encoding and client backpressure. This is a capture ceiling, not a promise.
STREAM_MAX_FPS = 1000
STREAM_RECORD_BYTES = 4096
MAX_PNG_BYTES = MAX_PIXELS * 4 + MAX_TEXT_BYTES
MAX_PPM_HEADER = 256
MAX_PPM_BYTES = MAX_PIXELS * 3 + MAX_PPM_HEADER
PROTOCOL_VERSION = 4
AGENT_VERSION = "0.6.0"
# Identify the source that this process loaded, even if deployment replaces it.
AGENT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class AgentError(Exception):
    """An expected operational or validation error, safe to return over stdio."""

    def __init__(self, message, code="operation_failed", input_state="not_started"):
        super().__init__(" ".join(str(message).split())[:1024])
        self.code = code
        self.input_state = input_state

    def as_dict(self):
        return {"code": self.code, "message": str(self), "input_state": self.input_state}


class _SamplingDeadlineExpired(Exception):
    """Internal budget exhaustion, distinct from failed capture or geometry."""


def _dimensions(width, height):
    if not (1 <= width <= 65535 and 1 <= height <= 65535 and width * height <= MAX_PIXELS):
        raise AgentError("Desktop dimensions are invalid or exceed 16 megapixels")


def _png(width, height, bgrx):
    """Encode negotiated RFB BGRX pixels as lossless RGB PNG, one row at a time."""
    def chunk(kind, value):
        return struct.pack("!I", len(value)) + kind + value + struct.pack("!I", zlib.crc32(kind + value) & 0xffffffff)

    compressor = zlib.compressobj(3)
    compressed = bytearray()
    for y in range(height):
        row = bgrx[y * width * 4:(y + 1) * width * 4]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        compressed.extend(compressor.compress(b"\0" + rgb))
    compressed.extend(compressor.flush())
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", bytes(compressed)) + chunk(b"IEND", b""))


def _png_rgb_rows(width, height, rows):
    """Encode a bounded sequence of RGB rows without loading the PPM raster."""
    def chunk(kind, value):
        return (struct.pack("!I", len(value)) + kind + value
                + struct.pack("!I", zlib.crc32(kind + value) & 0xffffffff))

    compressor = zlib.compressobj(3)
    compressed = bytearray()
    count = 0
    for row in rows:
        if len(row) != width * 3 or count >= height:
            raise AgentError("Compositor image has invalid pixel rows")
        compressed.extend(compressor.compress(b"\0" + row))
        count += 1
    if count != height:
        raise AgentError("Compositor image has missing pixel rows")
    compressed.extend(compressor.flush())
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", bytes(compressed)) + chunk(b"IEND", b""))


def _ppm_header(output):
    """Read P6 header with a fixed limit, leaving the cursor at its first RGB byte."""
    read = 0

    def byte():
        nonlocal read
        if read >= MAX_PPM_HEADER:
            raise AgentError("Compositor PPM header exceeds the byte limit")
        value = output.read(1)
        read += 1
        if not value:
            raise AgentError("Compositor PPM header is incomplete")
        return value[0]

    if bytes((byte(), byte())) != b"P6":
        raise AgentError("grim returned an invalid PPM header")

    def token():
        value = byte()
        while True:
            if value == 35:  # PPM permits comments between header values.
                while byte() != 10:
                    pass
            elif value in b" \t\r\n":
                pass
            else:
                break
            value = byte()
        digits = bytearray()
        while 48 <= value <= 57:
            digits.append(value)
            if len(digits) > 10:
                raise AgentError("grim returned an invalid PPM header")
            value = byte()
        if not digits or value not in b" \t\r\n":
            raise AgentError("grim returned an invalid PPM header")
        return int(digits)

    # Require separator after magic; token() must not silently accept P6123.
    separator = byte()
    if separator not in b" \t\r\n":
        raise AgentError("grim returned an invalid PPM header")
    output.seek(-1, os.SEEK_CUR)
    read -= 1
    width, height, maxval = token(), token(), token()
    if maxval != 255:
        raise AgentError("grim returned an unsupported PPM maximum value")
    _dimensions(width, height)
    return width, height, output.tell()


def _validated_ppm(output, desktop_width, desktop_height):
    output.seek(0, os.SEEK_END)
    if output.tell() > MAX_PPM_BYTES:
        raise AgentError("Compositor image exceeds the byte limit")
    output.seek(0)
    source_width, source_height, raster_start = _ppm_header(output)
    if (source_width, source_height) != (desktop_width, desktop_height):
        raise AgentError("Compositor screenshot dimensions differ from the input desktop; reconnect after display changes",
                         code="geometry_changed")
    output.seek(0, os.SEEK_END)
    if output.tell() != raster_start + source_width * source_height * 3:
        raise AgentError("Compositor PPM pixel payload has an invalid size")
    return source_width, source_height, raster_start


def _ppm_digest(output, desktop_width, desktop_height, region):
    """Compare every native RGB pixel in the ROI, regardless of return size."""
    source_width, _, raster_start = _validated_ppm(output, desktop_width, desktop_height)
    x, y, width, height = region
    digest = hashlib.sha256()
    for row in range(y, y + height):
        output.seek(raster_start + (row * source_width + x) * 3)
        pixels = output.read(width * 3)
        if len(pixels) != width * 3:
            raise AgentError("Compositor PPM pixel payload is truncated")
        digest.update(pixels)
    return digest.digest()


def _limit_capture_file():
    # This standalone agent is single-threaded. Keep preexec work limited to
    # the child's kernel file-size limit; the parent's limits never change.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PPM_BYTES, MAX_PPM_BYTES))


def _ppm_png(output, desktop_width, desktop_height, region, max_width):
    source_width, _, raster_start = _validated_ppm(output, desktop_width, desktop_height)
    x, y, width, height = region
    image_width = min(width, max_width if max_width is not None else width)
    image_height = max(1, height * image_width // width)
    sample_x = [(2 * col + 1) * width // (2 * image_width) for col in range(image_width)]

    def rows():
        for row in range(image_height):
            source_y = y + (2 * row + 1) * height // (2 * image_height)
            output.seek(raster_start + (source_y * source_width + x) * 3)
            source = output.read(width * 3)
            if len(source) != width * 3:
                raise AgentError("Compositor PPM pixel payload is truncated")
            if image_width == width:
                yield source
            else:
                target = bytearray(image_width * 3)
                for col, source_x in enumerate(sample_x):
                    target[col * 3:col * 3 + 3] = source[source_x * 3:source_x * 3 + 3]
                yield target

    return _png_rgb_rows(image_width, image_height, rows())


class RFBClient:
    """Bounded RFB 3.7/3.8 client for a private, locally owned Unix socket."""

    def __init__(self, connection, timeout=IO_TIMEOUT):
        self.connection = connection
        self.timeout = timeout
        self.width = self.height = 0
        self.name = ""
        self.frame = bytearray()
        self.deadline = 0.0
        self.read_budget = 0

    def _begin_read(self):
        self.deadline = time.monotonic() + self.timeout
        self.read_budget = MAX_PIXELS * 16 + MAX_TEXT_BYTES * 2

    def _read(self, count):
        if count < 0 or count > self.read_budget:
            raise AgentError("RFB response exceeds the byte limit")
        self.read_budget -= count
        result = bytearray()
        while len(result) < count:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AgentError("RFB response timed out")
            try:
                self.connection.settimeout(remaining)
                block = self.connection.recv(min(count - len(result), 65536))
            except socket.timeout as exc:
                raise AgentError("RFB response timed out") from exc
            except OSError as exc:
                raise AgentError("RFB read failed: " + str(exc)) from exc
            if not block:
                raise AgentError("RFB server closed the connection")
            result.extend(block)
        return bytes(result)

    def send(self, data):
        try:
            self.connection.settimeout(self.timeout)
            self.connection.sendall(data)
        except socket.timeout as exc:
            raise AgentError("RFB write timed out") from exc
        except OSError as exc:
            raise AgentError("RFB write failed: " + str(exc)) from exc

    def _text(self):
        size = struct.unpack("!I", self._read(4))[0]
        if size > MAX_TEXT_BYTES:
            raise AgentError("RFB text exceeds the byte limit")
        return self._read(size).decode("utf-8", "replace")

    def handshake(self):
        self._begin_read()
        version = self._read(12)
        if version not in (b"RFB 003.007\n", b"RFB 003.008\n"):
            raise AgentError("Only RFB 3.7 and 3.8 are supported")
        self.send(version)
        count = self._read(1)[0]
        if not count:
            raise AgentError("RFB handshake rejected: " + self._text())
        if 1 not in self._read(count):
            raise AgentError("Owned WayVNC server did not offer None authentication")
        self.send(b"\x01")
        if version == b"RFB 003.008\n" and struct.unpack("!I", self._read(4))[0]:
            raise AgentError("RFB authentication failed: " + self._text())
        self.send(b"\x01")  # shared session: never evict another client
        width, height = struct.unpack("!HH", self._read(4))
        _dimensions(width, height)
        self._read(16)  # replace the server's native format below
        self.name = self._text()
        self.width, self.height = width, height
        self.frame = bytearray(width * height * 4)
        self.send(b"\0\0\0\0" + struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        self.send(struct.pack("!BBHii", 2, 0, 2, 0, -223))  # Raw, DesktopSize

    def _request_frame(self):
        self.send(struct.pack("!BBHHHH", 3, 0, 0, 0, self.width, self.height))

    def screenshot_png(self):
        self._begin_read()
        self._request_frame()
        covered = bytearray(self.width * self.height)
        remaining_pixels = len(covered)
        # Bounds both unsolicited message storms and excessive rectangle overhead.
        for _ in range(4096):
            kind = self._read(1)[0]
            if kind == 0:
                _, count = struct.unpack("!BH", self._read(3))
                if count > 4096:
                    raise AgentError("Too many RFB rectangles")
                resized = False
                for _ in range(count):
                    x, y, width, height, encoding = struct.unpack("!HHHHi", self._read(12))
                    if encoding == -223:
                        _dimensions(width, height)
                        self.width, self.height = width, height
                        self.frame = bytearray(width * height * 4)
                        covered = bytearray(width * height)
                        remaining_pixels = len(covered)
                        resized = True
                        continue
                    if encoding != 0:
                        raise AgentError("Unsupported RFB encoding: " + str(encoding))
                    if not width or not height or x + width > self.width or y + height > self.height:
                        raise AgentError("RFB rectangle is outside desktop dimensions")
                    data = self._read(width * height * 4)
                    for row in range(height):
                        start = (y + row) * self.width + x
                        self.frame[start * 4:(start + width) * 4] = data[row * width * 4:(row + 1) * width * 4]
                        remaining_pixels -= covered[start:start + width].count(0)
                        covered[start:start + width] = b"\x01" * width
                if remaining_pixels == 0:
                    return _png(self.width, self.height, self.frame)
                if resized or count == 0:
                    self._request_frame()
            elif kind == 1:  # SetColorMapEntries; unused in negotiated true color
                _, _, count = struct.unpack("!BHH", self._read(5))
                self._read(count * 6)
            elif kind == 2:  # Bell
                continue
            elif kind == 3:  # ServerCutText; never apply it to the clipboard
                self._read(3)
                self._text()
            else:
                raise AgentError("Unsupported RFB server message: " + str(kind))
        raise AgentError("RFB response exceeds the message limit")

    def pointer(self, x, y, mask=0):
        self.send(struct.pack("!BBHH", 5, mask, x, y))

    def key_event(self, keysym, down):
        self.send(struct.pack("!BB2xI", 4, int(down), keysym))

    def close(self):
        self.connection.close()


def _wayland_environment():
    if not hasattr(os, "getuid"):
        raise AgentError("The desktop agent must run on the Pi under its desktop user")
    uid = os.getuid()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/" + str(uid)))
    try:
        info = runtime.stat()
    except OSError as exc:
        raise AgentError("The desktop user's XDG_RUNTIME_DIR is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise AgentError("XDG_RUNTIME_DIR must be a private directory owned by the desktop user")
    selected = os.environ.get("WAYLAND_DISPLAY")
    if selected:
        path = Path(selected)
        candidates = [path if path.is_absolute() else runtime / path]
    else:
        candidates = [runtime / "wayland-0"] + sorted(runtime.glob("wayland-*"))
    for candidate in candidates:
        try:
            info = candidate.stat()
        except OSError:
            continue
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == uid:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = str(runtime.resolve())
            environment["WAYLAND_DISPLAY"] = str(candidate.resolve())
            return environment, str(runtime.resolve())
    raise AgentError("No Wayland socket owned by the current desktop user was found")


class DesktopLease:
    """An exclusive per-user lease held across input and subsequent captures.

    Leave the lock file in place: unlinking would let a contender lock a new
    inode while a previous session still holds the old one. Only close our FD.
    """

    def __init__(self, runtime):
        self.fd = None
        if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
            raise AgentError("Desktop leases require Linux flock and O_NOFOLLOW")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            self.fd = os.open(str(Path(runtime) / "pi-desktop-bridge.lock"), flags, 0o600)
            info = os.fstat(self.fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise AgentError("Desktop lease must be a single regular file owned by this user with mode 0600")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentError("Desktop is already controlled by another bridge session; disconnect that session first", code="busy") from exc
        except BaseException as exc:
            self.close()
            if isinstance(exc, OSError):
                raise AgentError("Cannot acquire private desktop lease: " + str(exc)) from exc
            raise

    def close(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)


class OwnedWayVNC:
    """Own exactly one child process and two sockets inside a private directory."""

    def __init__(self):
        self.process = None
        self.client = None
        self.directory = None
        self.wayland_display = None
        self.lease = None
        self.environment = None
        self.output_name = None
        self.output_geometry = None
        self.capture_process = None

    def ensure(self, *, max_fps=None, prime_capture=True):
        if self.client is not None:
            if self.process.poll() is None:
                return self.client
            self.close()
        environment, runtime = _wayland_environment()
        executable = shutil.which("wayvnc")
        if not executable:
            raise AgentError("wayvnc is not installed on the Pi", code="missing_dependency")
        try:
            self.lease = DesktopLease(runtime)
            self.directory = Path(tempfile.mkdtemp(prefix="pi-desktop-", dir=runtime))
            self.directory.chmod(0o700)
            rfb_socket = str(self.directory / "rfb.sock")
            if len(os.fsencode(rfb_socket)) >= 108:
                raise AgentError("XDG_RUNTIME_DIR is too long for a Unix socket path")
            self.wayland_display = environment["WAYLAND_DISPLAY"]
            self.environment = environment
            # Explicitly ignore the user's configuration: it may enable TCP,
            # authentication, or a shared control socket. umask applies to child
            # sockets without changing this process's global file permissions.
            command = [executable, "-C", os.devnull, "-u", "-S", str(self.directory / "control.sock"),
                       "-r", "-R"]
            if max_fps is not None:
                command.extend(["-f", str(max_fps)])
            command.append(rfb_socket)
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=sys.stderr,
                env=environment, close_fds=True, start_new_session=True, umask=0o077,
            )
            deadline = time.monotonic() + IO_TIMEOUT
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AgentError("Owned WayVNC exited during startup; see stderr diagnostics")
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    connection.settimeout(min(1.0, max(0.01, deadline - time.monotonic())))
                    connection.connect(rfb_socket)
                except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                    connection.close()
                    time.sleep(0.05)
                    continue
                except BaseException:
                    connection.close()
                    raise
                self.client = RFBClient(connection)
                self.client.handshake()
                self.output_name = self._select_output_name()
                # ServerInit describes a gray placeholder in WayVNC 0.9.1.
                # Its input devices exist at this point, but exposing the first
                # cached RFB framebuffer can precede real compositor capture.
                # Require a compositor capture before accepting the first input.
                if prime_capture:
                    self.screenshot_png()
                return self.client
            raise AgentError("Owned WayVNC did not become ready within 10 seconds")
        except BaseException:
            self.close()
            raise

    def _select_output_name(self):
        # WayVNC publishes ServerInit before its asynchronous output-power
        # event arrives. UNKNOWN is a startup state, not permission to input.
        deadline = time.monotonic() + IO_TIMEOUT
        while True:
            output = self._query_selected_output(deadline=deadline)
            if (output["width"], output["height"]) != (self.client.width, self.client.height):
                raise AgentError("WayVNC output dimensions differ from the input desktop; reconnect after display changes", code="geometry_changed")
            if output["power"] == "ON":
                self.output_geometry = output
                return output["name"]
            if output["power"] != "UNKNOWN":
                raise AgentError("WayVNC captured output is not powered on", code="geometry_changed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentError("WayVNC output power did not become ready before the startup deadline", code="timeout")
            time.sleep(min(0.02, remaining))

    def _query_selected_output(self, deadline=None, sampling=False):
        """Query only our private control socket for the RFB pointer's output."""
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if deadline is None:
            deadline = time.monotonic() + IO_TIMEOUT
        def set_timeout():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if sampling:
                    raise _SamplingDeadlineExpired()
                raise AgentError("WayVNC output query timed out")
            connection.settimeout(remaining)
        try:
            set_timeout()
            connection.connect(str(self.directory / "control.sock"))
            set_timeout()
            connection.sendall(b'{"id":1,"method":"output-list"}')
            data = bytearray()
            while len(data) < MAX_REQUEST_BYTES:
                set_timeout()
                block = connection.recv(min(4096, MAX_REQUEST_BYTES - len(data)))
                if not block:
                    raise AgentError("WayVNC control socket closed before its output response")
                data.extend(block)
                # WayVNC control JSON is self-delimiting, without a newline.
                try:
                    response = json.loads(data)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(response, dict) or response.get("id") != 1 or response.get("code") != 0:
                    raise AgentError("WayVNC output query failed")
                outputs = response.get("data")
                if not isinstance(outputs, list):
                    raise AgentError("WayVNC returned an invalid output list")
                selected = [item for item in outputs if isinstance(item, dict) and item.get("captured") is True]
                if len(selected) != 1:
                    raise AgentError("WayVNC did not identify exactly one captured output", code="geometry_changed")
                output = selected[0]
                name, width, height = output.get("name"), output.get("width"), output.get("height")
                if (not isinstance(name, str) or not 1 <= len(name) <= 255 or "\0" in name
                        or type(width) is not int or type(height) is not int or output.get("power") not in ("ON", "OFF", "UNKNOWN")):
                    raise AgentError("WayVNC captured output is unavailable or has invalid geometry", code="geometry_changed")
                try:
                    _dimensions(width, height)
                except AgentError as exc:
                    raise AgentError("WayVNC captured output dimensions are invalid or exceed 16 megapixels", code="geometry_changed") from exc
                return {"name": name, "width": width, "height": height, "power": output["power"], "captured": True}
            raise AgentError("WayVNC output response exceeds the byte limit")
        except socket.timeout as exc:
            if sampling and time.monotonic() >= deadline:
                raise _SamplingDeadlineExpired() from exc
            raise AgentError("WayVNC output query timed out") from exc
        except OSError as exc:
            raise AgentError("WayVNC output query failed: " + str(exc)) from exc
        finally:
            connection.close()

    def check_geometry(self, deadline=None, sampling=False):
        """Reject stale input coordinates before sending any desktop events."""
        try:
            current = (self._query_selected_output() if deadline is None else
                       self._query_selected_output(deadline=deadline, sampling=sampling))
            if self.output_geometry is None or current != self.output_geometry:
                raise AgentError("Desktop output changed; take a fresh screenshot before sending more input", code="geometry_changed")
            if sampling and time.monotonic() >= deadline:
                raise _SamplingDeadlineExpired()
        except AgentError as exc:
            if exc.code == "operation_failed":
                exc.code = "preflight_failed"
            self.close()
            raise

    def _stop_capture(self):
        if self.capture_process is None:
            return
        process = self.capture_process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        finally:
            self.capture_process = None

    def screenshot_png(self, region=None, max_width=None):
        """Capture the actual compositor output, never WayVNC's cached frame."""
        self.check_geometry()
        executable = shutil.which("grim")
        if not executable:
            raise AgentError("Install the grim package on the Pi to capture its desktop", code="missing_dependency")
        transformed = region is not None or max_width is not None
        # An anonymous private file keeps subprocess output out of stdout and
        # avoids an unbounded in-memory communicate() result before size checks.
        with tempfile.TemporaryFile(dir=self.directory) as output:
            try:
                command = ([executable, "-c", "-t", "ppm", "-o", self.output_name, "-"] if transformed else
                           [executable, "-c", "-t", "png", "-l", "3", "-o", self.output_name, "-"])
                self.capture_process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=sys.stderr,
                    env=self.environment, close_fds=True, start_new_session=True, umask=0o077,
                )
                if self.capture_process.wait(timeout=IO_TIMEOUT) != 0:
                    raise AgentError("grim could not capture the compositor output; see stderr diagnostics")
                size = output.seek(0, os.SEEK_END)
                if size > (MAX_PPM_BYTES if transformed else MAX_PNG_BYTES):
                    raise AgentError("Compositor image exceeds the byte limit")
                output.seek(0)
                if transformed:
                    if region is None:
                        region = (0, 0, self.client.width, self.client.height)
                    data = _ppm_png(output, self.client.width, self.client.height, region, max_width)
                    self.check_geometry()
                    return data
                header = output.read(33)
                if (len(header) != 33 or header[:8] != b"\x89PNG\r\n\x1a\n"
                        or header[8:16] != b"\0\0\0\rIHDR"
                        or zlib.crc32(header[12:29]) & 0xffffffff != struct.unpack("!I", header[29:33])[0]):
                    raise AgentError("grim returned an invalid PNG header")
                width, height = struct.unpack("!II", header[16:24])
                _dimensions(width, height)
                if (width, height) != (self.client.width, self.client.height):
                    raise AgentError("Compositor screenshot dimensions differ from the input desktop; reconnect after display changes", code="geometry_changed")
                data = header + output.read(MAX_PNG_BYTES - len(header) + 1)
                if not data.endswith(b"\0\0\0\0IEND\xaeB`\x82"):
                    raise AgentError("grim returned an incomplete PNG")
                self.check_geometry()
                return data
            except subprocess.TimeoutExpired as exc:
                raise AgentError("Compositor capture timed out") from exc
            except OSError as exc:
                raise AgentError("Compositor capture failed: " + str(exc)) from exc
            finally:
                self._stop_capture()

    def _capture_ppm(self, output, executable, deadline):
        """Overwrite one bounded sample file under the sampling deadline."""
        self.check_geometry(deadline=deadline, sampling=True)
        output.seek(0)
        output.truncate()
        try:
            if deadline <= time.monotonic():
                raise _SamplingDeadlineExpired()
            self.capture_process = subprocess.Popen(
                [executable, "-c", "-t", "ppm", "-o", self.output_name, "-"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=sys.stderr,
                env=self.environment, close_fds=True, start_new_session=True, umask=0o077,
                **({"preexec_fn": _limit_capture_file} if resource is not None else {}),
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _SamplingDeadlineExpired()
            if self.capture_process.wait(timeout=remaining) != 0:
                raise AgentError("grim could not capture the compositor output; see stderr diagnostics")
        except subprocess.TimeoutExpired as exc:
            if time.monotonic() >= deadline:
                raise _SamplingDeadlineExpired() from exc
            raise AgentError("Compositor capture timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError("Compositor capture failed: " + str(exc)) from exc
        finally:
            self._stop_capture()

    def wait_for_stable(self, region, max_width, stable_ms, timeout_ms, poll_ms):
        """Observe sampled ROI equality; retain and encode the final sample."""
        started = time.monotonic()
        deadline = started + timeout_ms / 1000
        executable = shutil.which("grim")
        if not executable:
            raise AgentError("Install the grim package on the Pi to capture its desktop", code="missing_dependency")
        width, height = self.client.width, self.client.height
        previous = None
        unchanged_since = None
        samples = 0
        stable = False
        # Keep the latest validated sample while a candidate is captured. Swap
        # and reuse these two bounded anonymous files; never accumulate rasters.
        with (tempfile.TemporaryFile(dir=self.directory) as latest,
              tempfile.TemporaryFile(dir=self.directory) as candidate):
            while time.monotonic() < deadline:
                try:
                    self._capture_ppm(candidate, executable, deadline)
                    digest = _ppm_digest(candidate, width, height, region)
                    self.check_geometry(deadline=deadline, sampling=True)
                except _SamplingDeadlineExpired:
                    break
                sampled = time.monotonic()
                if sampled > deadline:
                    break
                latest, candidate = candidate, latest
                samples += 1
                if digest != previous:
                    unchanged_since = sampled
                previous = digest
                if samples >= 2 and sampled >= unchanged_since + stable_ms / 1000:
                    stable = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(poll_ms / 1000, remaining))
            if not samples:
                raise AgentError("Compositor capture timed out before a valid sample")
            stability = {"stable": stable, "timed_out": not stable,
                         "elapsed_ms": max(0, min(timeout_ms, round((time.monotonic() - started) * 1000))),
                         "samples": samples, "stable_ms": stable_ms,
                         "timeout_ms": timeout_ms, "poll_ms": poll_ms}
            # PNG work is outside the sampling budget. No extra final capture.
            png = _ppm_png(latest, width, height, region, max_width)
        return png, stability

    def close(self):
        try:
            self._stop_capture()
        except (OSError, subprocess.TimeoutExpired) as exc:
            print("pi-desktop-agent capture cleanup: " + str(exc), file=sys.stderr)
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
            self.client = None
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print("pi-desktop-agent cleanup: " + str(exc), file=sys.stderr)
            self.process = None
        if self.directory is not None:
            try:
                shutil.rmtree(self.directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                print("pi-desktop-agent cleanup: " + str(exc), file=sys.stderr)
            self.directory = None
        if self.lease is not None:
            self.lease.close()
            self.lease = None
        self.output_name = None
        self.output_geometry = None
        self.wayland_display = None
        self.environment = None

    def is_active(self):
        return self.client is not None and self.process is not None and self.process.poll() is None


KEYSYMS = {
    "BackSpace": 0xff08, "Tab": 0xff09, "Return": 0xff0d, "Escape": 0xff1b,
    "Delete": 0xffff, "Insert": 0xff63, "Home": 0xff50, "End": 0xff57,
    "Left": 0xff51, "Up": 0xff52, "Right": 0xff53, "Down": 0xff54,
    "Page_Up": 0xff55, "Page_Down": 0xff56, "space": 0x20,
    "Shift_L": 0xffe1, "Shift_R": 0xffe2, "Control_L": 0xffe3, "Control_R": 0xffe4,
    "Alt_L": 0xffe9, "Alt_R": 0xffea, "Super_L": 0xffeb, "Super_R": 0xffec,
    **{"F" + str(i): 0xffbd + i for i in range(1, 13)},
}
BUTTONS = {"left": 1, "middle": 2, "right": 4}
WHEEL = {"up": 8, "down": 16, "left": 32, "right": 64}
PARAMETERS = {
    "hello": set(), "health": set(),
    "status": set(), "screenshot": {"x", "y", "width", "height", "max_width"}, "disconnect": set(),
    "wait_for_stable": {"x", "y", "width", "height", "max_width", "stable_ms", "timeout_ms", "poll_ms"},
    "move": {"x", "y", "frame_id"}, "click": {"x", "y", "button", "count", "frame_id"},
    "drag": {"start_x", "start_y", "end_x", "end_y", "button", "steps", "frame_id"},
    "scroll": {"x", "y", "direction", "ticks", "frame_id"}, "type_text": {"text"}, "key": {"keys"},
}
INPUT_METHODS = frozenset({"move", "click", "drag", "scroll", "type_text", "key"})


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgentError("%s must be an integer from %d to %d" % (name, minimum, maximum), code="invalid_params")
    return value


def _character_keysym(value):
    if value in ("\n", "\r"):
        return KEYSYMS["Return"]
    if value == "\t":
        return KEYSYMS["Tab"]
    if not value.isprintable() or 0xd800 <= ord(value) <= 0xdfff:
        raise AgentError("Text contains an unsupported control character", code="invalid_params")
    codepoint = ord(value)
    return codepoint if codepoint <= 255 else 0x01000000 | codepoint


class DesktopAgent:
    def __init__(self, session=None):
        self.session = session if session is not None else OwnedWayVNC()
        self.client = None
        self.position = None
        self.held_keys = []
        self.pointer_held = False
        self.typing_process = None
        self.input_started = False
        self.latest_frame_id = None
        self.latest_frame_client = None

    def _coordinates(self, params, x="x", y="y"):
        return (_integer(params.get(x), x, 0, 65535),
                _integer(params.get(y), y, 0, 65535))

    def _pointer(self, position, mask=0):
        # Track before send: sendall can fail after the peer received the event.
        self._consume_frame()
        self.input_started = True
        self.position = position
        if mask:
            self.pointer_held = True
        self.client.pointer(*position, mask)
        if not mask:
            self.pointer_held = False

    def _move_before_press(self, position):
        # labwc/XWayland can deliver a press at the previous cursor position
        # when motion and press share one virtual-pointer frame. Give motion
        # its own button-free frame and let pointer focus settle before press.
        self._pointer(position)
        time.sleep(0.05)

    def _release(self):
        error = None
        for value in reversed(self.held_keys[:]):
            try:
                self.client.key_event(value, False)
                self.held_keys.remove(value)
            except AgentError as exc:
                error = exc
        if self.pointer_held:
            try:
                self._pointer(self.position)
            except AgentError as exc:
                error = exc
        if error:
            raise error

    def _chord(self, values):
        try:
            for value in values:
                self._consume_frame()
                self.held_keys.append(value)
                self.input_started = True
                self.client.key_event(value, True)
        finally:
            self._release()

    def _stop_typing(self):
        if self.typing_process is None:
            return
        process = self.typing_process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        finally:
            if process.stdin is not None:
                process.stdin.close()
            self.typing_process = None

    def _type_text(self, text, executable):
        environment, _ = _wayland_environment()
        if environment["WAYLAND_DISPLAY"] != self.session.wayland_display:
            raise AgentError("Wayland display changed; disconnect and reconnect before typing", code="geometry_changed")
        # wtype decodes stdin using the locale; an SSH session may otherwise use C.
        environment["LC_ALL"] = "C.UTF-8"
        try:
            self._consume_frame()
            self.typing_process = subprocess.Popen(
                [executable, "-d", "2", "-"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=sys.stderr, env=environment,
                close_fds=True, start_new_session=True, umask=0o077,
            )
            self.input_started = True
            self.typing_process.communicate(text.encode("utf-8"), timeout=8 + len(text) * 0.008)
            if self.typing_process.returncode:
                raise AgentError("wtype failed; text may have been partially typed. Inspect the desktop before retrying", code="input_failed")
        except subprocess.TimeoutExpired as exc:
            raise AgentError("wtype timed out; text may have been partially typed. Inspect the desktop before retrying", code="timeout") from exc
        except OSError as exc:
            raise AgentError("wtype failed: " + str(exc), code="input_failed") from exc
        finally:
            self._stop_typing()

    def _consume_frame(self):
        self.latest_frame_id = None
        self.latest_frame_client = None

    def _prepare(self, method, params):
        """Reject malformed actions before any desktop session is acquired."""
        if not isinstance(method, str) or method not in PARAMETERS:
            raise AgentError("Unknown desktop method", code="unknown_method")
        if not isinstance(params, dict) or any(key not in PARAMETERS[method] for key in params):
            raise AgentError("Invalid parameters for " + method, code="invalid_params")
        prepared = {}
        if method in ("screenshot", "wait_for_stable"):
            region_keys = {"x", "y", "width", "height"}
            present = region_keys.intersection(params)
            if present and present != region_keys:
                raise AgentError("Provide x, y, width, and height together", code="invalid_params")
            if present:
                x, y = self._coordinates(params)
                width = _integer(params["width"], "width", 1, 65535)
                height = _integer(params["height"], "height", 1, 65535)
                if width * height > MAX_PIXELS:
                    raise AgentError("Screenshot region exceeds 16 megapixels", code="invalid_params")
                prepared["region"] = (x, y, width, height)
            if "max_width" in params:
                prepared["max_width"] = _integer(params["max_width"], "max_width", 1, 65535)
            if method == "wait_for_stable":
                prepared["stable_ms"] = _integer(params.get("stable_ms", 300), "stable_ms", 50, 2000)
                prepared["timeout_ms"] = _integer(params.get("timeout_ms", 5000), "timeout_ms", 100, 10000)
                prepared["poll_ms"] = _integer(params.get("poll_ms", 100), "poll_ms", 50, 1000)
                if not prepared["poll_ms"] <= prepared["stable_ms"] <= prepared["timeout_ms"]:
                    raise AgentError("Require poll_ms <= stable_ms <= timeout_ms", code="invalid_params")
        if method in ("move", "click", "drag", "scroll") and "frame_id" in params:
            frame_id = params["frame_id"]
            if type(frame_id) is not str or re.fullmatch(r"[0-9a-f]{32}", frame_id) is None:
                raise AgentError("frame_id must be 32 lowercase hexadecimal characters", code="invalid_params")
            prepared["frame_id"] = frame_id
        if method in ("move", "click"):
            prepared["position"] = self._coordinates(params)
        if method in ("click", "drag"):
            button = params.get("button", "left")
            if not isinstance(button, str) or button not in BUTTONS:
                raise AgentError("button must be left, middle, or right", code="invalid_params")
            prepared["mask"] = BUTTONS[button]
        if method == "click":
            prepared["count"] = _integer(params.get("count", 1), "count", 1, 2)
        elif method == "drag":
            prepared["start"] = self._coordinates(params, "start_x", "start_y")
            prepared["end"] = self._coordinates(params, "end_x", "end_y")
            prepared["steps"] = _integer(params.get("steps", 20), "steps", 1, 200)
        elif method == "scroll":
            direction = params.get("direction")
            if not isinstance(direction, str) or direction not in WHEEL:
                raise AgentError("direction must be up, down, left, or right", code="invalid_params")
            prepared["mask"] = WHEEL[direction]
            prepared["count"] = _integer(params.get("ticks", 1), "ticks", 1, 50)
            if ("x" in params) != ("y" in params):
                raise AgentError("Provide both x and y for a targeted scroll", code="invalid_params")
            prepared["targeted"] = "x" in params
            prepared["position"] = self._coordinates(params) if prepared["targeted"] else self.position
            if prepared["position"] is None:
                raise AgentError("Provide x and y for the first scroll, or move to a visible target first", code="invalid_params")
        elif method == "type_text":
            value = params.get("text")
            if not isinstance(value, str) or len(value) > 4096:
                raise AgentError("text must be a string of at most 4096 characters", code="invalid_params")
            text = value.replace("\r\n", "\n").replace("\r", "\n")
            for char in text:
                _character_keysym(char)  # Validate all text before starting wtype.
            prepared["text"] = text
            prepared["executable"] = shutil.which("wtype")
            if not prepared["executable"]:
                raise AgentError("Install the wtype package on the Pi to enable reliable Unicode text input", code="missing_dependency")
        elif method == "key":
            keys = params.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
                raise AgentError("keys must be a list of 1 to 8 key names", code="invalid_params")
            values = []
            for key in keys:
                if not isinstance(key, str):
                    raise AgentError("Each key must be a named key or printable character", code="invalid_params")
                if key in KEYSYMS:
                    value = KEYSYMS[key]
                elif len(key) == 1 and key.isprintable():
                    value = _character_keysym(key)
                else:
                    raise AgentError("Unknown key name", code="invalid_params")
                if value in values:
                    raise AgentError("A key chord cannot contain duplicate keys", code="invalid_params")
                values.append(value)
            prepared["keys"] = values
        return prepared

    def _hello(self):
        return {"protocol_version": PROTOCOL_VERSION, "agent_version": AGENT_VERSION,
                "agent_sha256": AGENT_SHA256, "capabilities": list(PARAMETERS)}

    def _health(self):
        checks = [{"name": "python", "ok": sys.version_info >= (3, 11),
                   "message": "Python 3.11 or newer is required"}]
        for name in ("wayvnc", "grim", "wtype"):
            found = shutil.which(name) is not None
            checks.append({"name": name, "ok": found,
                           "message": name + " is available" if found else "Install the " + name + " package on the Pi"})
        try:
            _wayland_environment()
            checks.append({"name": "wayland", "ok": True,
                           "message": "Private runtime directory and owned Wayland socket are available"})
        except AgentError as exc:
            checks.append({"name": "wayland", "ok": False, "message": str(exc)})
        return {**self._hello(), "desktop_ready": all(check["ok"] for check in checks),
                "session_active": self.session.is_active(), "checks": checks}

    def dispatch(self, method, params):
        self.input_started = False
        was_active = False
        try:
            was_active = self.session.is_active()
            prepared = self._prepare(method, params)
            if method == "hello":
                return self._hello()
            if method == "health":
                return self._health()
            if method == "disconnect":
                self.close()
                return {"ok": True}
            previous_client = self.client
            self.client = self.session.ensure()
            if previous_client is not None and self.client is not previous_client:
                was_active = False  # A replacement lease must not survive a rejected action.
                self.position = None
                self._consume_frame()
                if method == "scroll" and not prepared["targeted"]:
                    raise AgentError("Provide x and y after reconnecting the desktop session", code="invalid_params")
            if "frame_id" in prepared and (prepared["frame_id"] != self.latest_frame_id
                                           or self.latest_frame_client is not self.client):
                raise AgentError("Screenshot view is stale; take a fresh screenshot before sending input",
                                 code="stale_view")
            if method in INPUT_METHODS:
                self.session.check_geometry()
            for key in ("position", "start", "end"):
                if key in prepared:
                    x, y = prepared[key]
                    _integer(x, "x", 0, self.client.width - 1)
                    _integer(y, "y", 0, self.client.height - 1)
            if method == "status":
                return {"hostname": socket.gethostname(), "width": self.client.width,
                        "height": self.client.height, "desktop_name": self.client.name,
                        "wayland_display": self.session.wayland_display}
            if method in ("screenshot", "wait_for_stable"):
                desktop_width, desktop_height = self.client.width, self.client.height
                region = prepared.get("region", (0, 0, desktop_width, desktop_height))
                x, y, width, height = region
                if x + width > desktop_width or y + height > desktop_height:
                    raise AgentError("Screenshot region is outside desktop dimensions", code="invalid_params")
                max_width = prepared.get("max_width")
                if method == "wait_for_stable":
                    png, stability = self.session.wait_for_stable(
                        region, max_width, prepared["stable_ms"], prepared["timeout_ms"], prepared["poll_ms"])
                elif "region" in prepared or max_width is not None:
                    png = self.session.screenshot_png(region, max_width)
                else:
                    png = self.session.screenshot_png()
                image_width = min(width, max_width if max_width is not None else width)
                image_height = max(1, height * image_width // width)
                result = {"image_base64": base64.b64encode(png).decode("ascii"),
                          "mime_type": "image/png", "width": image_width, "height": image_height,
                          "desktop_width": desktop_width, "desktop_height": desktop_height,
                          "region": {"x": x, "y": y, "width": width, "height": height},
                          "frame_id": secrets.token_hex(16)}
                if method == "wait_for_stable":
                    result["stability"] = stability
                self.latest_frame_id = result["frame_id"]
                self.latest_frame_client = self.client
                return result
            if method == "move":
                self._pointer(prepared["position"])
            elif method in ("click", "scroll"):
                position, mask, count = prepared["position"], prepared["mask"], prepared["count"]
                if method == "click" or prepared["targeted"]:
                    self._move_before_press(position)
                for index in range(count):
                    try:
                        self._pointer(position, mask)
                    finally:
                        self._release()
                    if method == "click" and index + 1 < count:
                        time.sleep(0.08)
            elif method == "drag":
                start, end = prepared["start"], prepared["end"]
                mask, steps = prepared["mask"], prepared["steps"]
                self._move_before_press(start)
                try:
                    self._pointer(start, mask)
                    for index in range(1, steps + 1):
                        time.sleep(0.01)
                        point = tuple(round(a + (b - a) * index / steps) for a, b in zip(start, end))
                        self._pointer(point, mask)
                finally:
                    self._release()
            elif method == "type_text":
                self._type_text(prepared["text"], prepared["executable"])
            elif method == "key":
                self._chord(prepared["keys"])
            return {"ok": True}
        except AgentError as exc:
            if self.input_started:
                exc.input_state = "may_have_executed"
                if exc.code == "operation_failed":
                    exc.code = "input_failed"
            preserve = was_active and not self.input_started and exc.code in {
                "invalid_params", "unknown_method", "missing_dependency", "stale_view"}
            if not preserve:
                self.close()
            raise
        except BaseException as exc:
            self.close()
            if isinstance(exc, Exception):
                raise AgentError("Unexpected desktop failure; inspect the desktop before retrying",
                                 code="internal_error", input_state="may_have_executed") from exc
            raise

    def close(self):
        try:
            try:
                self._stop_typing()
            finally:
                self._release()
        except Exception as exc:
            print("pi-desktop-agent input cleanup: " + type(exc).__name__, file=sys.stderr)
        finally:
            try:
                self.session.close()
            except Exception as exc:
                print("pi-desktop-agent session cleanup: " + type(exc).__name__, file=sys.stderr)
            finally:
                self.client = None
                self._consume_frame()
                self.held_keys.clear()
                self.pointer_held = False
                self.position = None


def serve(desktop, incoming, outgoing):
    """Process serialized newline JSON requests; stdout carries no diagnostics."""
    try:
        while True:
            line = incoming.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                break
            request_id = None
            disconnect = False
            oversized = len(line.encode("utf-8")) > MAX_REQUEST_BYTES
            try:
                if oversized:
                    raise AgentError("Request exceeds the byte limit", code="invalid_request")
                try:
                    request = json.loads(line)
                except (ValueError, RecursionError) as exc:
                    raise AgentError("Request must be valid JSON", code="invalid_request") from exc
                if not isinstance(request, dict) or type(request.get("id")) is not int:
                    raise AgentError("Request id must be an integer", code="invalid_request")
                request_id = request["id"]
                if set(request) != {"id", "method", "params"}:
                    raise AgentError("Request requires id, method, and params", code="invalid_request")
                result = desktop.dispatch(request["method"], request["params"])
                response = {"id": request_id, "result": result}
                disconnect = request["method"] == "disconnect"
            except AgentError as exc:
                response = {"id": request_id, "error": exc.as_dict()}
            except Exception as exc:
                print("pi-desktop-agent: unexpected " + type(exc).__name__, file=sys.stderr)
                desktop.close()
                response = {"id": request_id, "error": AgentError("Desktop operation failed",
                            code="internal_error", input_state="may_have_executed").as_dict()}
            outgoing.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n")
            outgoing.flush()
            if disconnect or oversized:
                break
    finally:
        desktop.close()


def _stream_json(outgoing_fd, value, deadline):
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(data) > STREAM_RECORD_BYTES:
        raise AgentError("Stream startup record exceeds the byte limit")
    while data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("Stream startup write timed out", code="timeout")
        _, writable, _ = select.select([], [outgoing_fd], [], remaining)
        if not writable:
            continue
        try:
            written = os.write(outgoing_fd, data)
        except BlockingIOError:
            continue
        if not written:
            raise AgentError("Stream startup output closed")
        data = data[written:]


def _stream_read_start(incoming_fd, deadline):
    # Read exactly the acknowledgement: the next byte may already be RFB.
    expected, received = b"START\n", b""
    while len(received) < len(expected):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("Stream startup acknowledgement timed out", code="timeout")
        readable, _, _ = select.select([incoming_fd], [], [], remaining)
        if not readable:
            continue
        try:
            part = os.read(incoming_fd, len(expected) - len(received))
        except BlockingIOError:
            continue
        received += part
        if not part or not expected.startswith(received):
            raise AgentError("Stream startup requires START followed by a newline", code="invalid_request")


def _relay_stream(connection, incoming_fd, outgoing_fd, process):
    """Relay with one bounded buffer per direction and no background workers.

    A full buffer stops reading its producer until its consumer accepts it.
    The SSH pipes and the private socket therefore apply backpressure end to
    end. A stalled consumer or an exited owned process ends the viewer.
    """
    to_wayvnc = b""
    to_viewer = b""
    wayvnc_deadline = viewer_deadline = None
    while True:
        if process.poll() is not None:
            raise AgentError("Owned WayVNC exited during streaming")
        now = time.monotonic()
        deadlines = [value for value in (wayvnc_deadline, viewer_deadline) if value is not None]
        if deadlines and min(deadlines) <= now:
            raise AgentError("Stream consumer timed out", code="timeout")
        # Periodically notice a dead child even when both peers are idle.
        timeout = min([0.25] + [value - now for value in deadlines])
        readers = ([] if to_wayvnc else [incoming_fd]) + ([] if to_viewer else [connection])
        writers = ([connection] if to_wayvnc else []) + ([outgoing_fd] if to_viewer else [])
        readable, writable, _ = select.select(readers, writers, [], timeout)
        if connection in writable:
            try:
                sent = connection.send(to_wayvnc)
            except BlockingIOError:
                sent = None
            if sent == 0:
                return
            if sent:
                to_wayvnc = to_wayvnc[sent:]
                wayvnc_deadline = time.monotonic() + IO_TIMEOUT if to_wayvnc else None
        if outgoing_fd in writable:
            try:
                sent = os.write(outgoing_fd, to_viewer)
            except BlockingIOError:
                sent = None
            if sent == 0:
                return
            if sent:
                to_viewer = to_viewer[sent:]
                viewer_deadline = time.monotonic() + IO_TIMEOUT if to_viewer else None
        if incoming_fd in readable:
            try:
                to_wayvnc = os.read(incoming_fd, STREAM_CHUNK_BYTES)
            except BlockingIOError:
                continue
            if not to_wayvnc:
                return
            wayvnc_deadline = time.monotonic() + IO_TIMEOUT
        if connection in readable:
            try:
                to_viewer = connection.recv(STREAM_CHUNK_BYTES)
            except BlockingIOError:
                continue
            if not to_viewer:
                return
            viewer_deadline = time.monotonic() + IO_TIMEOUT


def stream_main():
    """Fixed SSH bootstrap entry: provenance, START, readiness, then raw RFB.

    This is deliberately separate from the JSON-RPC agent. The controller
    verifies the exact loaded source before acknowledging permission to acquire
    the desktop lease. No stream target or command comes from the browser.
    """
    session = OwnedWayVNC()
    connection = None
    binary_phase = False
    blocking_modes = []
    previous_handlers = []
    incoming_fd = sys.stdin.buffer.fileno()
    outgoing_fd = sys.stdout.buffer.fileno()

    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    try:
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                previous_handlers.append((signum, signal.signal(signum, stop)))
        for fd in (incoming_fd, outgoing_fd):
            blocking_modes.append((fd, os.get_blocking(fd)))
            os.set_blocking(fd, False)
        _stream_json(outgoing_fd, {
            "mode": "rfb_stream", "protocol_version": PROTOCOL_VERSION,
            "agent_version": AGENT_VERSION, "agent_sha256": AGENT_SHA256,
        }, time.monotonic() + IO_TIMEOUT)
        _stream_read_start(incoming_fd, time.monotonic() + IO_TIMEOUT)
        probe = session.ensure(max_fps=STREAM_MAX_FPS, prime_capture=False)
        # The probe validates geometry and output power without grim. Keep it
        # owned until teardown, but give the viewer a fresh, untouched handshake.
        _dimensions(probe.width, probe.height)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(IO_TIMEOUT)
        connection.connect(str(session.directory / "rfb.sock"))
        connection.setblocking(False)
        # If this record is only partly written, append no second JSON record.
        binary_phase = True
        _stream_json(outgoing_fd, {"ready": True, "width": probe.width, "height": probe.height,
                                   "max_fps": STREAM_MAX_FPS}, time.monotonic() + IO_TIMEOUT)
        _relay_stream(connection, incoming_fd, outgoing_fd, session.process)
    except Exception as exc:
        if not binary_phase:
            error = exc if isinstance(exc, AgentError) else AgentError("Desktop stream startup failed")
            try:
                _stream_json(outgoing_fd, {"error": error.as_dict()}, time.monotonic() + IO_TIMEOUT)
            except (OSError, AgentError):
                pass
        else:
            print("pi-desktop-agent stream ended: " + type(exc).__name__, file=sys.stderr)
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            try:
                # Destroying the two owned clients and WayVNC also destroys its
                # virtual input devices, releasing keys/buttons on viewer EOF.
                session.close()
            finally:
                for fd, blocking in reversed(blocking_modes):
                    try:
                        os.set_blocking(fd, blocking)
                    except OSError:
                        pass
                for signum, handler in reversed(previous_handlers):
                    signal.signal(signum, handler)


def main():
    desktop = DesktopAgent()

    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    try:
        serve(desktop, sys.stdin, sys.stdout)
    except BrokenPipeError:
        pass
    finally:
        desktop.close()


if __name__ == "__main__":
    main()
