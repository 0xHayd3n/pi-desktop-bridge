# Pi Desktop Bridge

An MCP server that lets Codex and other MCP-enabled assistants see and operate a Raspberry Pi's Wayland desktop through an existing SSH connection.

The bridge returns real PNG screenshots to the model and provides mouse movement, clicks, dragging, scrolling, text entry and keyboard shortcuts. Screenshots can show a smaller overview or a full-resolution region, with image coordinates mapped back to the desktop. Input tools return a fresh screenshot after the action, with configurable image size. A visual wait can observe an area until its sampled pixels stop changing. It runs over SSH and uses a private WayVNC UNIX socket on the Pi; no VNC or web port is opened.

## Requirements

- A Raspberry Pi with an active **wlroots-compatible Wayland desktop**, such as Raspberry Pi OS with labwc.
- `python3`, `wayvnc`, `grim` and `wtype` installed on the Pi. No Python packages are installed there. Install missing desktop tools with `sudo apt-get install --no-install-recommends wayvnc grim wtype`.
- An SSH host alias that works without a password prompt, with its host key already trusted.
- Python 3.11+ and [uv](https://docs.astral.sh/uv/) on the computer running the MCP client.
- A client that supports local stdio MCP and image content, such as Codex. Client capabilities differ; this is not the native Computer Use tool.

This controls the **Pi's own desktop**. The Pi does not need a capture dongle or another attachment. It does not provide HDMI capture or hardware KVM control of a different PC. A headless Pi still needs a running graphical session with an output configured by its compositor.

## Install and connect

Clone this repository and run these commands from its directory:

```sh
uv sync --frozen
uv run pi-desktop-bridge deploy --host pi-desktop
uv run pi-desktop-bridge doctor --host pi-desktop
uv run pi-desktop-bridge status --host pi-desktop
```

`pi-desktop` is an example SSH alias. Substitute your working alias, or create a concrete `Host pi-desktop` entry in your SSH config. The deployment copies the agent into `~/.local/share/pi-desktop-bridge` under the SSH user, checks the file's SHA-256, and needs no `sudo`.

`doctor` reports SSH/agent compatibility, source consistency, required tools and the desktop user's Wayland environment as JSON. It exits with status 1 when a check fails and includes next steps. It checks prerequisites without taking the desktop lease; it does not reserve the desktop, guarantee a capture, or prove an off-network VPN route.

For Codex, add the server using the Python executable inside this repository's `.venv`:

```powershell
codex mcp add pi_desktop -- D:\Coding\pi-desktop-bridge\.venv\Scripts\python.exe -m pi_desktop_bridge serve --host pi-desktop
codex mcp get pi_desktop
```

On macOS/Linux, use `/absolute/path/to/pi-desktop-bridge/.venv/bin/python` instead. Restart the server in Codex's MCP settings, or restart Codex, to load the new tools. The [official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp) describes shared CLI, desktop and IDE configuration.

For another MCP client, configure the same executable and arguments:

```json
{
  "mcpServers": {
    "pi_desktop": {
      "command": "D:/Coding/pi-desktop-bridge/.venv/Scripts/python.exe",
      "args": ["-m", "pi_desktop_bridge", "serve", "--host", "pi-desktop"]
    }
  }
}
```

This is a configuration example, not a universal IDE extension. The client must implement MCP image handling and tool calls. ChatGPT in a web browser cannot directly launch this local stdio server.

## Tools

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

The client checks protocol compatibility before its first operation on each SSH connection. Update the repo, run `uv sync --frozen`, deploy the agent again and restart the MCP server when upgrading. A compatible protocol alone does not prove that the deployed source is current; `doctor` also compares its SHA-256.

Validation and prerequisite errors explain what was rejected and confirm that input did not start. If delivery becomes uncertain, or input was acknowledged but its resulting image could not be captured, further input is blocked until an explicit full-desktop `desktop_screenshot` succeeds. A region capture does not clear this requirement. A full-desktop overview with `max_width` can clear it. Input is never automatically replayed. Health and status expose the local `observation_required` flag and `recovery_action` hint. Health, status, visual waits and disconnect do not clear this requirement.

Each MCP tool has one request budget covering its queue wait, protocol handshake, input and resulting capture (60 seconds by default). A queued call that is cancelled or expires sends no input and does not cancel the active call. If input has already started, cancellation waits for that bounded operation and its cleanup before releasing the session lock, then requires a fresh screenshot. Stopping an owned SSH process has a separate bounded cleanup allowance.

## Optional Codex plugin distribution

This repository also contains a portable `plugin.json`, `mcp.json` and a repo marketplace. The plugin's default SSH alias is `pi-desktop`; deploy the agent and ensure that alias works before enabling it. The plugin launcher uses `uv` and stores its environment under the client's plugin data directory.

```sh
codex plugin marketplace add 0xHayd3n/pi-desktop-bridge
```

Then install/enable Pi Desktop Bridge from that marketplace in a compatible Codex client. Private GitHub repositories require GitHub access. The portable files are supplied for distribution; direct MCP configuration is the primary integration tested here. This repository is not a public Plugins Directory listing. Plugin format and local marketplace behavior are described in the [official packaging documentation](https://developers.openai.com/plugins/build/plugins).

## Verification

```sh
uv run python -m unittest discover -s tests -v
uv run python scripts/live_smoke.py --host pi-desktop
uv run python scripts/live_views.py --host pi-desktop
uv run python scripts/live_stability.py --host pi-desktop
```

The first command uses fake protocol peers to check framing, bounds, PNG pixels, held-input release, concurrency, deployment and error handling. The other commands are **opt-in real desktop tests**: they open a disposable Tk window on the Pi, exercise the real MCP stdio client and desktop input, then close and remove the fixture. The views test compares crop RGB pixels, exercises image-coordinate input, and records payload sizes and request times. The stability test drives an owned changing patch to check settling, animation timeout and resized action images. These tests need `tkinter` and an Xwayland display at `:0` on the Pi. Screenshots and reports are saved under ignored `_local/` and are not published.

See the [verification record](docs/verification.md) for actual checks and remaining integration limits.

## Troubleshooting and removal

- **SSH failure:** run `ssh <alias>` in a normal terminal. The bridge uses `BatchMode=yes` and `StrictHostKeyChecking=yes`; it will not request a password or silently trust a new host key.
- **No desktop/output:** log into the Pi's supported Wayland desktop. Raspberry Pi OS Lite alone has no desktop to capture. The bridge reports missing Wayland/WayVNC support instead of installing a desktop or changing login settings.
- **Connection lost after input:** the action may already have happened. Take another screenshot and assess the result before retrying. Input remains blocked until that capture succeeds.
- **Agent mismatch or missing prerequisites:** run `pi-desktop-bridge doctor --host <alias>`. After updating, deploy again and restart/reconnect the MCP server. No tools are silently installed on the Pi.
- **Desktop busy:** ask the controlling client to call `desktop_disconnect` or stop its server. Health checks can run without acquiring the lease.
- **Using it away from home:** the SSH alias must resolve to a reachable VPN/mesh address or another SSH route. A `.local` LAN hostname alone does not provide worldwide access. Tailscale login and ping do not prove that SSH is allowed from the computer running the client.
- **Disable:** use `codex mcp remove pi_desktop` or disable the server in your client. Closing the MCP server stops its SSH agent and owned WayVNC process; no system service is installed.
- **Uninstall the Pi agent:** after stopping clients, remove only `~/.local/share/pi-desktop-bridge` under the SSH user. This does not remove WayVNC, SSH or their existing configuration.

See [architecture and data handling](docs/architecture.md) for the transport, process lifecycle and trust boundaries.
