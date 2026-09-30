# Pi Desktop Bridge

[![CI](https://github.com/0xHayd3n/pi-desktop-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/0xHayd3n/pi-desktop-bridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Give **Codex and other MCP-enabled assistants** eyes and hands on a Raspberry Pi's Wayland desktop. Open a local connection and desktop viewer in a browser tab, or use MCP tools to capture screenshots, move and click the mouse, drag, scroll, type Unicode text, and use keyboard shortcuts over SSH.

The bridge runs on your computer and starts a small user-owned agent on the Pi. It uses the Pi's existing graphical session, with no extra hardware or Pi-side VNC/web network port. This controls the **Pi's own desktop**; controlling another computer needs a separate software connection or hardware KVM.

## Features

- **Visual desktop control:** eleven MCP tools for screenshots, status, mouse, keyboard, and disconnect.
- **Connection UI:** sign in with a Pi address, username, and password, or use an existing SSH alias; credentials stay in the local process rather than MCP tool arguments.
- **Desktop stream:** noVNC displays continuous WayVNC updates over SSH, with direct mouse, drag, scroll, and keyboard input.
- **Efficient images:** smaller overviews and native-resolution regions, with screenshot coordinates mapped back to the desktop.
- **Observe after acting:** input tools return a fresh screenshot; visual waits sample an area until its pixels settle.
- **Controlled sessions:** one client at a time, explicit disconnect, and configurable idle release after five minutes by default.
- **Recovery checks:** stale views and mismatched agent source are rejected; uncertain input requires a fresh screenshot before more input.
- **SSH transport:** strict host-key checking, existing SSH credentials, and private UNIX sockets on the Pi.

## Requirements

| Where | Required |
| --- | --- |
| Raspberry Pi | A running wlroots-compatible Wayland desktop, such as Raspberry Pi OS with labwc; `python3`, `wayvnc`, `grim`, and `wtype` |
| SSH connection | SSH enabled on the Pi; log in as the **same user who owns the graphical session**. The viewer supports a password or local keys; direct MCP setup requires an already trusted host key and login without a password prompt |
| Client computer | Python 3.11+, OpenSSH, [uv](https://docs.astral.sh/uv/), and Git |
| Assistant application | A local stdio MCP client that supports image content and tool calls, such as Codex |

No Python packages are installed on the Pi. If its desktop tools are missing, install them there:

```sh
sudo apt-get install --no-install-recommends wayvnc grim wtype
```

Raspberry Pi OS Lite alone has no desktop to capture. A headless Pi still needs a running graphical session with an output configured by its compositor. This MCP integration is separate from Codex's native Computer Use tool.

## Open the Pi in a Codex tab

Clone the repository on the computer running Codex, then launch the local viewer:

```sh
git clone https://github.com/0xHayd3n/pi-desktop-bridge.git
cd pi-desktop-bridge
uv sync --frozen
uv run pi-desktop-bridge ui
```

Open the printed private launch link in Codex's built-in browser, or ask Codex to launch this command and open its link for you. No interactive terminal SSH login is needed for this flow. Enter the Pi's reachable address, graphical-session username, and password in the viewer, or choose **Use a saved SSH connection** and enter your existing SSH alias. To prefill that alias, launch with `ui --host <alias>`.

For a previously unknown SSH host, check its fingerprint against a trusted source before selecting **Trust & connect**. Existing OpenSSH host keys are checked; a changed known key is rejected. New confirmations are held for this connection rather than saved to your SSH configuration. Passwords remain in memory while connected and are released on disconnect; they are not saved or passed through MCP tools.

The viewer can deploy or update the packaged agent after login. The Pi still needs its supported desktop and desktop tools; the viewer does not install system packages or enable SSH. Its listener binds only to `127.0.0.1` and uses a private launch capability. Treat the launch link as private. Host and Origin checks, bounded requests, and a restrictive content security policy protect its local API.

After login, the Pi desktop fills the viewer. Click inside it to focus, then click, drag, scroll, type, or use keyboard shortcuts directly. Plain ASCII paste, including tabs and newlines, uses an explicit browser clipboard gesture. Unsupported paste is rejected in full; the viewer does not synchronize clipboards in the background. The separate MCP text tool supports Unicode. Press **F6** to move focus to **Disconnect**.

The viewer carries continuous RFB updates through an authenticated local HTTP response and SSH connection. Ordered input requests are acknowledged after their SSH writes. There is no screenshot polling or capture before clicking. The browser moves the cursor locally, so pointer movement does not wait for a video update; the Pi supplies its cursor shape, with a small dot when the shape is absent or transparent. Clicks and application responses still depend on the remote connection.

A high capture ceiling avoids WayVNC's delay between changed frames. In v0.6.0, the tested 60 Hz Pi displayed about 49–56 canvas updates per second, with sampled key-to-screen times of 51–100 ms. The v0.6.1 cursor and direct-login TCP changes have passed local checks; a new live Pi comparison is pending. Actual rate and input latency depend on the Pi, desktop activity, encoding, browser, and network route. An idle desktop sends changes when needed. This is a custom browser viewer, separate from Codex's native Computer Use backend. [Codex's browser supports local web applications](https://learn.chatgpt.com/docs/browser).

Disconnect when finished. The viewer releases its SSH session after 30 seconds without its visible-tab heartbeat. Raw stream traffic does not keep a hidden or abandoned tab alive. It holds the Pi's exclusive desktop lease while active; another MCP client must wait for release. A failed stream requires explicit reconnection; input is never replayed automatically.

## Direct MCP setup

### 1. Check SSH

The examples use the SSH alias `pi-desktop`. Replace it with your working alias, or create a `Host pi-desktop` entry in your SSH config. Confirm that it connects as the desktop user:

```sh
ssh pi-desktop
```

Resolve host-key trust and key-based authentication in your normal SSH client first. The bridge does not prompt for passwords or silently trust new hosts.

### 2. Install and deploy

Run on the computer hosting your MCP client:

```sh
git clone https://github.com/0xHayd3n/pi-desktop-bridge.git
cd pi-desktop-bridge
uv sync --frozen
uv run pi-desktop-bridge deploy --host pi-desktop
uv run pi-desktop-bridge doctor --host pi-desktop
```

Deployment installs the agent into `~/.local/share/pi-desktop-bridge` under the SSH user and verifies its SHA-256. It needs no `sudo`, system service, or firewall changes. `doctor` reports compatibility, tools, and Wayland prerequisites without taking the desktop lease; it does not guarantee a capture or prove an off-network route.

### 3. Connect Codex

From the cloned repository, register its virtual-environment Python executable. The optional 960-pixel setting reduces post-action image size; explicit screenshots keep their own sizing options.

**Windows / PowerShell:**

```powershell
$bridgePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
codex mcp add pi_desktop -- $bridgePython -m pi_desktop_bridge serve --host pi-desktop --capture-max-width 960
codex mcp get pi_desktop
```

**macOS / Linux:**

```sh
codex mcp add pi_desktop -- "$PWD/.venv/bin/python" -m pi_desktop_bridge serve --host pi-desktop --capture-max-width 960
codex mcp get pi_desktop
```

Restart the MCP server in your client's settings, or restart Codex, to load the tools. See [OpenAI's MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp) for supported Codex clients and configuration.

Other MCP clients can use the same executable and arguments; see the [client configuration example](docs/usage.md#other-mcp-clients). Client support varies. ChatGPT web cannot directly launch this local stdio server.

### 4. Use the desktop

Start by asking your assistant:

> Take a 960-pixel overview of the Pi desktop and describe what is visible. Wait for my next instruction before changing anything.

When switching windows, move the pointer into the target window and inspect the returned screenshot before clicking. A local person changing focus can affect where input goes. Call `desktop_disconnect` when finished to release control immediately.

## Tools

| Tool | Purpose |
| --- | --- |
| `desktop_health` | Check prerequisites and recovery state without taking control |
| `desktop_status` | Inspect desktop size, connection, and session policy |
| `desktop_screenshot` | Capture a full desktop, overview, or region |
| `desktop_wait_for_stable` | Observe a source area until sampled pixels settle, or return a timeout image |
| `desktop_move` | Move the pointer |
| `desktop_click` | Single or double click with the left, middle, or right button |
| `desktop_drag` | Drag between two coordinates |
| `desktop_scroll` | Scroll vertically or horizontally at a target |
| `desktop_type` | Type text into the focused application |
| `desktop_key` | Press a key or shortcut |
| `desktop_disconnect` | Release control and allow later reconnection |

See the [tool reference](docs/usage.md) for image coordinates, `view_id`, crop and resize examples, stability timing, idle policy, and error recovery.

## Security and data handling

- The Pi agent runs as the SSH user and uses an owned private WayVNC UNIX socket. Its transport is SSH; the optional viewer's web listener is confined to local loopback. SSH credentials stay on the client computer.
- Source hashes detect deployment drift; they do not attest that the remote machine is trustworthy. Use only a Pi and SSH account you trust.
- Screenshots and typed content are sent to the MCP client and may be processed by its AI service. The server does not retain a screenshot history; explicit exports and opt-in verification scripts can save images locally.
- Input is never automatically replayed. After uncertain delivery, inspect a fresh full-desktop screenshot before deciding whether to repeat an action.
- Reports and screenshots under `_local/`, the `.venv/` environment, and `.env` files are ignored by Git. Exports saved elsewhere can be tracked: keep credentials and personal captures out of commits, issues, and pull requests.

See [architecture and trust boundaries](docs/architecture.md) for the protocol, process lifecycle, and data flow.

## Remote access and current limits

For use away from home, configure the SSH alias to a reachable VPN/mesh address or another working SSH route. A `.local` hostname provides LAN discovery, not worldwide access. VPN login or ping alone does not prove SSH reachability; test SSH from the network where you will run the client.

The tested setup uses Raspberry Pi 5, Raspberry Pi OS / Debian 13, labwc, and a 1920 × 1080 output. Off-network SSH was not verified in that setup. Direct MCP cross-window first-click delivery can depend on compositor focus; move and observe before clicking. The streamed viewer sends pointer and key events directly, with no delivery guarantee on every compositor. A visual stability result means sampled pixels matched, not that an application is ready. See the [verification record](docs/verification.md) for measured streaming performance and remaining limits.

## Upgrade

```sh
git pull --ff-only
uv sync --frozen
uv run pi-desktop-bridge deploy --host pi-desktop
uv run pi-desktop-bridge doctor --host pi-desktop
```

Restart the MCP server after upgrading. The expected agent hash is pinned when the client process starts.

## Optional Codex plugin

A portable `plugin.json`, `mcp.json`, and repository marketplace are included:

```sh
codex plugin marketplace add 0xHayd3n/pi-desktop-bridge
```

Install and enable Pi Desktop Bridge from that marketplace in a compatible Codex client. Its default alias is `pi-desktop`; deploy the agent and make that alias work first. The launcher uses `uv` and keeps its environment under the client's plugin data directory.

Direct MCP configuration is the primary verified integration. The plugin files have been parsed by Codex's loader, but this repository is not a public Plugins Directory listing. See [OpenAI's plugin packaging documentation](https://developers.openai.com/plugins/build/plugins).

## Development and verification

```sh
uv run python -m unittest discover -s tests -v
node scripts/check_ui_gestures.cjs
uv build
```

The unit suite covers protocol framing, image validation, bounds, concurrency, cancellation, session release, deployment, and recovery. CI runs on Windows with Python 3.14 and Ubuntu with Python 3.11.

The live tests operate a real Pi desktop through the MCP SDK:

```sh
uv run python scripts/live_smoke.py --host pi-desktop
uv run python scripts/live_views.py --host pi-desktop
uv run python scripts/live_stability.py --host pi-desktop
uv run python scripts/live_lifecycle.py --host pi-desktop
uv run python scripts/live_dashboard.py --host pi-desktop
```

These are **opt-in input tests**. They open a disposable Tk window, operate it, then remove their fixture. They require `tkinter` and an Xwayland display at `:0` on the Pi. Images and reports go into ignored `_local/`. Run them when nobody else is using the desktop. See the [verification record](docs/verification.md) for checks actually completed.

## Troubleshooting and removal

- **SSH failure:** verify the same alias in a normal terminal. The bridge uses `BatchMode=yes` and `StrictHostKeyChecking=yes`.
- **Missing desktop or tools:** run `uv run pi-desktop-bridge doctor --host pi-desktop`. Check that SSH uses the graphical session's user.
- **Agent source mismatch:** update, deploy again, and restart the MCP server.
- **Desktop busy:** disconnect the controlling client, stop its server, or wait for its configured idle release. Health checks do not take the lease.
- **Connection lost after input:** the action may already have happened. Take a fresh full-desktop screenshot and inspect it before retrying.
- **Disable:** run `codex mcp remove pi_desktop`, or disable the server in your client. Closing the server stops its owned SSH agent and WayVNC process.
- **Remove the agent:** stop clients, then remove only `~/.local/share/pi-desktop-bridge` under the SSH user. Existing SSH and desktop tools remain installed.

## License

[MIT](LICENSE). Independent project; not affiliated with Raspberry Pi or OpenAI.
