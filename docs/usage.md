# Tool reference

[Installation and overview](../README.md) · [Architecture](architecture.md)

## Available tools

| Tool | Operation |
| --- | --- |
| `desktop_health` | Check prerequisites without taking control; show local recovery state |
| `desktop_status` | Desktop size, connection information and local recovery state |
| `desktop_screenshot` | Full PNG, region detail, or a smaller overview using `max_width` |
| `desktop_wait_for_stable` | Sample a source area until unchanged for a chosen interval; return its final image and stability result |
| `desktop_move` | Move to absolute screenshot pixel coordinates |
| `desktop_click` | Left, middle or right click; single or double |
| `desktop_drag` | Drag between two screenshot coordinates |
| `desktop_scroll` | Scroll up, down, left or right at an explicit `x`, `y` target |
| `desktop_type` | Type text into the focused app |
| `desktop_key` | Press a key or shortcut, for example `["Control_L", "a"]` |
| `desktop_disconnect` | Release the desktop; the next observation reconnects |

By default, mouse coordinates use the original desktop's dimensions and top-left origin. Take a screenshot before choosing a target. The tools operate the active desktop, so a local person moving focus can change where subsequent input goes. Keyboard behavior depends on the target app and compositor; the live verification checks the actual Pi rather than inferring behavior from protocol messages.

When switching windows, move the pointer into the target window and inspect the returned screenshot before clicking. Focus and compositor behavior can affect click delivery; the bridge does not repeat clicks automatically.

For a smaller overview, call `desktop_screenshot` with `{"max_width": 960}`. For original-pixel detail, pass a region such as `{"x": 200, "y": 150, "width": 640, "height": 400}`. Supply all four region fields together; they refer to the original desktop. `max_width` also works with regions and never enlarges an image. Downsampling uses nearest-neighbor pixel centers; use a native-resolution region to read small text.

All six input tools accept `capture_max_width`, for example `desktop_click` with `{"x": 400, "y": 300, "capture_max_width": 960}`. The action still uses original desktop coordinates unless `view_id` is supplied; only its resulting image is resized. Start the server with `serve --host pi-desktop --capture-max-width 960` to set a default for all post-action images. A per-call value overrides it; `65535` requests native width on supported desktops. The default remains native when no server setting is supplied. Explicit `desktop_screenshot` and `desktop_wait_for_stable` use their own `max_width` and stay independent of that setting. Invalid capture settings are rejected before any input is sent.

To observe repainting, call `desktop_wait_for_stable` with a source region and optional `max_width`. For example, `{"x": 200, "y": 150, "width": 640, "height": 400, "max_width": 640, "stable_ms": 300, "timeout_ms": 5000, "poll_ms": 100}` watches that region's native RGB pixels and returns the last sampled image. The JSON metadata includes `stability.stable`, `timed_out`, `elapsed_ms` and `samples`. A normal sampling timeout returns the latest image with `stable: false`; capture or geometry failures return an error. Only the final PNG travels over SSH.

The defaults are 300 ms unchanged, a 5000 ms sampling budget and at least 100 ms between captures. Valid ranges are 50–2000 for `stable_ms`, 100–10000 for `timeout_ms`, and 50–1000 for `poll_ms`, with `poll_ms <= stable_ms <= timeout_ms`. The sampling budget starts after session setup; bounded helper cleanup, final PNG encoding and transfer are outside it but still inside the tool's overall request budget. `elapsed_ms` measures sampling only and is capped at `timeout_ms`. Each poll captures the full output on the Pi even when monitoring a region. Choose a small area that excludes unrelated clocks, animations or blinking cursors. Sampled equality does not prove an app is ready: changes between polls can be missed, and a quiet phase can end after the tool returns.

Each image includes JSON metadata with `view_id`, its actual pixel dimensions, the original desktop size and the source region. To use coordinates measured in that image, supply its `view_id` to `desktop_move`, `desktop_click`, `desktop_drag` or targeted `desktop_scroll`. The bridge performs the conversion. Without `view_id`, those tools continue to take original desktop coordinates. A new capture, an input action, disconnect or a replaced session invalidates older views. A rejected stale view sends no input; capture again to obtain the current view. A view ID verifies the capture/session relationship; it cannot detect application changes or someone moving focus since the image was taken.

For scrolling, pass both `x` and `y` inside the visible pane. Omitting them uses the last position moved by this bridge; a new connection rejects an untargeted scroll rather than guessing a position. Before sending input, the agent verifies that the captured output and its dimensions still match the input session. A changed display rejects the action before delivery; take a new screenshot to reconnect with its current geometry.

Only one bridge session can control a desktop user at a time. A second client receives a busy error until the first client closes its bridge connection. This prevents separate assistants from interleaving input. Text is delivered through `wtype`'s virtual keyboard without replacing the clipboard. Screenshots use `grim` to capture the compositor directly, avoiding WayVNC's startup placeholder and cached frames.

Call `desktop_disconnect` when finished so another client can use the Pi without restarting the MCP server. The call is idempotent. A fresh screenshot opens a new SSH/desktop session when needed.

The MCP server also releases a desktop session after **five minutes without desktop activity**. Successful status, capture, wait and input calls renew that interval after they finish; health polls and rejected or queued-cancelled calls do not. Active operations finish before idle cleanup can run. Automatic release closes the owned SSH/desktop connection and invalidates image views. Take a new screenshot before using mapped coordinates again. Start `serve --idle-timeout 600` to choose another interval in seconds, or `--idle-timeout 0` to disable it; values from 0 to 3600 are accepted.

Health and status include `session_policy` with the configured `idle_timeout_seconds`, a locally inferred `local_session_active` flag, `idle_remaining_seconds`, and `auto_release_count`. Local state is conservative after uncertain results; the remote health `session_active` field describes that agent's current session. Idle cleanup does not clear an uncertain-input recovery requirement. A failed background release is attempted once for that activity interval and leaves later explicit observations available to reconnect.

The client checks protocol compatibility and the packaged agent's SHA-256 before desktop operations on each SSH connection. A compatible but different source is blocked before any desktop request is written; `hello`, health and cleanup remain available for diagnostics. The SSH launcher reads one bounded source snapshot, executes those exact bytes and binds the advertised hash before starting the agent. This detects deployment drift; SSH authentication and host-key checking still establish identity. Update the repo, run `uv sync --frozen`, deploy the agent again and restart the MCP server when upgrading. The expected hash is pinned when the client process loads, so replacing files on disk requires a restart.

Validation and prerequisite errors explain what was rejected and confirm that input did not start. If delivery becomes uncertain, or input was acknowledged but its resulting image could not be captured, further input is blocked until an explicit full-desktop `desktop_screenshot` succeeds. A region capture does not clear this requirement. A full-desktop overview with `max_width` can clear it. Input is never automatically replayed. Health and status expose the local `observation_required` flag and `recovery_action` hint. Health, status, visual waits and disconnect do not clear this requirement.

Each MCP tool has one request budget covering its queue wait, protocol handshake, input and resulting capture (60 seconds by default). A queued call that is cancelled or expires sends no input and does not cancel the active call. If input has already started, cancellation waits for that bounded operation and its cleanup before releasing the session lock, then requires a fresh screenshot. Stopping an owned SSH process has a separate bounded cleanup allowance.

## Other MCP clients

Use an absolute path to your repository virtual environment. Replace the example path below; on macOS/Linux, use `/absolute/path/to/pi-desktop-bridge/.venv/bin/python`.

```json
{
  "mcpServers": {
    "pi_desktop": {
      "command": "C:/path/to/pi-desktop-bridge/.venv/Scripts/python.exe",
      "args": ["-m", "pi_desktop_bridge", "serve", "--host", "pi-desktop"]
    }
  }
}
```

This is a configuration example, not a universal IDE extension. The client must implement MCP image handling and tool calls. ChatGPT in a web browser cannot directly launch this local stdio server.
