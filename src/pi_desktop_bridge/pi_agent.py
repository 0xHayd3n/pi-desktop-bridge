"""Standalone Pi desktop agent. Run with Python 3 over an SSH stdio channel.

No third-party imports, TCP listener, shell commands, or existing VNC sessions.
The owned WayVNC process exposes just one output of the logged-in Wayland user.
"""

import base64
import json
import os
from pathlib import Path
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


MAX_PIXELS = 16_777_216
MAX_TEXT_BYTES = 1_048_576
IO_TIMEOUT = 10.0
MAX_REQUEST_BYTES = 65_536
MAX_PNG_BYTES = MAX_PIXELS * 4 + MAX_TEXT_BYTES


class AgentError(Exception):
    """An expected operational or validation error, safe to return over stdio."""


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
                raise AgentError("Desktop is already controlled by another bridge session; disconnect that session first") from exc
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
        self.capture_process = None

    def ensure(self):
        if self.client is not None:
            if self.process.poll() is None:
                return self.client
            self.close()
        environment, runtime = _wayland_environment()
        executable = shutil.which("wayvnc")
        if not executable:
            raise AgentError("wayvnc is not installed on the Pi")
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
            self.process = subprocess.Popen(
                [executable, "-C", os.devnull, "-u", "-S", str(self.directory / "control.sock"),
                 "-r", "-R", rfb_socket],
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
                self.screenshot_png()
                return self.client
            raise AgentError("Owned WayVNC did not become ready within 10 seconds")
        except BaseException:
            self.close()
            raise

    def _select_output_name(self):
        """Query only our private control socket for the RFB pointer's output."""
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + IO_TIMEOUT
        try:
            connection.settimeout(IO_TIMEOUT)
            connection.connect(str(self.directory / "control.sock"))
            connection.sendall(b'{"id":1,"method":"output-list"}')
            data = bytearray()
            while len(data) < MAX_REQUEST_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentError("WayVNC output query timed out")
                connection.settimeout(remaining)
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
                names = [item.get("name") for item in outputs if isinstance(item, dict) and item.get("captured") is True]
                if (len(names) != 1 or not isinstance(names[0], str)
                        or not 1 <= len(names[0]) <= 255 or "\0" in names[0]):
                    raise AgentError("WayVNC did not identify exactly one captured output")
                return names[0]
            raise AgentError("WayVNC output response exceeds the byte limit")
        except socket.timeout as exc:
            raise AgentError("WayVNC output query timed out") from exc
        except OSError as exc:
            raise AgentError("WayVNC output query failed: " + str(exc)) from exc
        finally:
            connection.close()

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

    def screenshot_png(self):
        """Capture the actual compositor output, never WayVNC's cached frame."""
        executable = shutil.which("grim")
        if not executable:
            raise AgentError("Install the grim package on the Pi to capture its desktop")
        # An anonymous private file keeps subprocess output out of stdout and
        # avoids an unbounded in-memory communicate() result before size checks.
        with tempfile.TemporaryFile(dir=self.directory) as output:
            try:
                self.capture_process = subprocess.Popen(
                    [executable, "-c", "-t", "png", "-l", "3", "-o", self.output_name, "-"],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=sys.stderr,
                    env=self.environment, close_fds=True, start_new_session=True, umask=0o077,
                )
                if self.capture_process.wait(timeout=IO_TIMEOUT) != 0:
                    raise AgentError("grim could not capture the compositor output; see stderr diagnostics")
                size = output.seek(0, os.SEEK_END)
                if size > MAX_PNG_BYTES:
                    raise AgentError("Compositor image exceeds the byte limit")
                output.seek(0)
                header = output.read(33)
                if (len(header) != 33 or header[:8] != b"\x89PNG\r\n\x1a\n"
                        or header[8:16] != b"\0\0\0\rIHDR"
                        or zlib.crc32(header[12:29]) & 0xffffffff != struct.unpack("!I", header[29:33])[0]):
                    raise AgentError("grim returned an invalid PNG header")
                width, height = struct.unpack("!II", header[16:24])
                _dimensions(width, height)
                if (width, height) != (self.client.width, self.client.height):
                    raise AgentError("Compositor screenshot dimensions differ from the input desktop; reconnect after display changes")
                data = header + output.read(MAX_PNG_BYTES - len(header) + 1)
                if not data.endswith(b"\0\0\0\0IEND\xaeB`\x82"):
                    raise AgentError("grim returned an incomplete PNG")
                return data
            except subprocess.TimeoutExpired as exc:
                raise AgentError("Compositor capture timed out") from exc
            except OSError as exc:
                raise AgentError("Compositor capture failed: " + str(exc)) from exc
            finally:
                self._stop_capture()

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
    "status": set(), "screenshot": set(), "disconnect": set(),
    "move": {"x", "y"}, "click": {"x", "y", "button", "count"},
    "drag": {"start_x", "start_y", "end_x", "end_y", "button", "steps"},
    "scroll": {"x", "y", "direction", "ticks"}, "type_text": {"text"}, "key": {"keys"},
}


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgentError("%s must be an integer from %d to %d" % (name, minimum, maximum))
    return value


def _character_keysym(value):
    if value in ("\n", "\r"):
        return KEYSYMS["Return"]
    if value == "\t":
        return KEYSYMS["Tab"]
    if not value.isprintable() or 0xd800 <= ord(value) <= 0xdfff:
        raise AgentError("Text contains an unsupported control character")
    codepoint = ord(value)
    return codepoint if codepoint <= 255 else 0x01000000 | codepoint


class DesktopAgent:
    def __init__(self, session=None):
        self.session = session if session is not None else OwnedWayVNC()
        self.client = None
        self.position = (0, 0)
        self.held_keys = []
        self.pointer_held = False
        self.typing_process = None

    def _coordinates(self, params, x="x", y="y"):
        return (_integer(params.get(x), x, 0, self.client.width - 1),
                _integer(params.get(y), y, 0, self.client.height - 1))

    def _pointer(self, position, mask=0):
        # Track before send: sendall can fail after the peer received the event.
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
                self.held_keys.append(value)
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

    def _type_text(self, text):
        executable = shutil.which("wtype")
        if not executable:
            raise AgentError("Install the wtype package on the Pi to enable reliable Unicode text input")
        environment, _ = _wayland_environment()
        if environment["WAYLAND_DISPLAY"] != self.session.wayland_display:
            raise AgentError("Wayland display changed; disconnect and reconnect before typing")
        # wtype decodes stdin using the locale; an SSH session may otherwise use C.
        environment["LC_ALL"] = "C.UTF-8"
        try:
            self.typing_process = subprocess.Popen(
                [executable, "-d", "2", "-"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=sys.stderr, env=environment,
                close_fds=True, start_new_session=True, umask=0o077,
            )
            self.typing_process.communicate(text.encode("utf-8"), timeout=8 + len(text) * 0.008)
            if self.typing_process.returncode:
                raise AgentError("wtype failed; text may have been partially typed. Inspect the desktop before retrying")
        except subprocess.TimeoutExpired as exc:
            raise AgentError("wtype timed out; text may have been partially typed. Inspect the desktop before retrying") from exc
        except OSError as exc:
            raise AgentError("wtype failed; text may have been partially typed: " + str(exc)) from exc
        finally:
            self._stop_typing()

    def dispatch(self, method, params):
        if not isinstance(method, str) or method not in PARAMETERS:
            raise AgentError("Unknown desktop method")
        if not isinstance(params, dict) or any(key not in PARAMETERS[method] for key in params):
            raise AgentError("Invalid parameters for " + method)
        if method == "disconnect":
            self.close()
            return {"ok": True}
        self.client = self.session.ensure()
        # Validate an entire action before emitting any event.
        if method in ("move", "click"):
            position = self._coordinates(params)
        if method in ("click", "drag"):
            button = params.get("button", "left")
            if not isinstance(button, str) or button not in BUTTONS:
                raise AgentError("button must be left, middle, or right")
            mask = BUTTONS[button]
        if method == "click":
            count = _integer(params.get("count", 1), "count", 1, 2)
        elif method == "drag":
            start = self._coordinates(params, "start_x", "start_y")
            end = self._coordinates(params, "end_x", "end_y")
            steps = _integer(params.get("steps", 20), "steps", 1, 200)
        elif method == "scroll":
            direction = params.get("direction")
            if not isinstance(direction, str) or direction not in WHEEL:
                raise AgentError("direction must be up, down, left, or right")
            mask = WHEEL[direction]
            count = _integer(params.get("ticks", 1), "ticks", 1, 50)
            position = self._coordinates(params) if "x" in params or "y" in params else self.position
            _integer(position[0], "x", 0, self.client.width - 1)
            _integer(position[1], "y", 0, self.client.height - 1)
        elif method == "type_text":
            value = params.get("text")
            if not isinstance(value, str) or len(value) > 4096:
                raise AgentError("text must be a string of at most 4096 characters")
            text = value.replace("\r\n", "\n").replace("\r", "\n")
            for char in text:
                _character_keysym(char)  # Validate all text before starting wtype.
        elif method == "key":
            keys = params.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
                raise AgentError("keys must be a list of 1 to 8 key names")
            values = []
            for key in keys:
                if not isinstance(key, str):
                    raise AgentError("Each key must be a named key or printable character")
                if key in KEYSYMS:
                    value = KEYSYMS[key]
                elif len(key) == 1 and key.isprintable():
                    value = _character_keysym(key)
                else:
                    raise AgentError("Unknown key name")
                if value in values:
                    raise AgentError("A key chord cannot contain duplicate keys")
                values.append(value)
        try:
            if method == "status":
                return {"hostname": socket.gethostname(), "width": self.client.width,
                        "height": self.client.height, "desktop_name": self.client.name,
                        "wayland_display": self.session.wayland_display}
            if method == "screenshot":
                png = self.session.screenshot_png()
                return {"image_base64": base64.b64encode(png).decode("ascii"),
                        "mime_type": "image/png", "width": self.client.width, "height": self.client.height}
            if method == "move":
                self._pointer(position)
            elif method in ("click", "scroll"):
                if method == "click":
                    self._move_before_press(position)
                for index in range(count):
                    try:
                        self._pointer(position, mask)
                    finally:
                        self._release()
                    if method == "click" and index + 1 < count:
                        time.sleep(0.08)
            elif method == "drag":
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
                self._type_text(text)
            elif method == "key":
                self._chord(values)
            return {"ok": True}
        except BaseException:
            # A partially read frame cannot safely be reused, and a failed input
            # may have reached the compositor. Closing removes our virtual devices.
            self.close()
            raise

    def close(self):
        try:
            try:
                self._stop_typing()
            finally:
                self._release()
        except (AgentError, OSError):
            pass
        finally:
            self.session.close()
            self.client = None
            self.held_keys.clear()
            self.pointer_held = False


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
                    raise AgentError("Request exceeds the byte limit")
                try:
                    request = json.loads(line)
                except (ValueError, RecursionError) as exc:
                    raise AgentError("Request must be valid JSON") from exc
                if not isinstance(request, dict) or type(request.get("id")) is not int:
                    raise AgentError("Request id must be an integer")
                request_id = request["id"]
                if set(request) != {"id", "method", "params"}:
                    raise AgentError("Request requires id, method, and params")
                result = desktop.dispatch(request["method"], request["params"])
                response = {"id": request_id, "result": result}
                disconnect = request["method"] == "disconnect"
            except AgentError as exc:
                response = {"id": request_id, "error": {"message": str(exc)[:1024]}}
            except Exception as exc:
                print("pi-desktop-agent: " + str(exc), file=sys.stderr)
                desktop.close()
                response = {"id": request_id, "error": {"message": "Desktop operation failed"}}
            outgoing.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n")
            outgoing.flush()
            if disconnect or oversized:
                break
    finally:
        desktop.close()


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
