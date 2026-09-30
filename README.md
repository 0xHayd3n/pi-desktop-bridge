# Pi Desktop Bridge

[![CI](https://github.com/0xHayd3n/pi-desktop-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/0xHayd3n/pi-desktop-bridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Give **Codex and other MCP-enabled assistants** eyes and hands on a Raspberry Pi's Wayland desktop. Capture real screenshots, move and click the mouse, drag, scroll, type Unicode text, and use keyboard shortcuts over an existing SSH connection.

The bridge runs on your computer and starts a small user-owned agent on the Pi. It uses the Pi's existing graphical session, with no extra hardware and no exposed VNC or web port. This controls the **Pi's own desktop**; controlling another computer needs a separate software connection or hardware KVM.

## Features

- **Visual desktop control:** eleven MCP tools for screenshots, status, mouse, keyboard, and disconnect.
- **Efficient images:** smaller overviews and native-resolution regions, with screenshot coordinates mapped back to the desktop.
- **Observe after acting:** input tools return a fresh screenshot; visual waits sample an area until its pixels settle.
- **Controlled sessions:** one client at a time, explicit disconnect, and configurable idle release after five minutes by default.
- **Recovery checks:** stale views and mismatched agent source are rejected; uncertain input requires a fresh screenshot before more input.
- **SSH transport:** strict host-key checking, existing SSH credentials, and private UNIX sockets on the Pi.

## Requirements

| Where | Required |
| --- | --- |
| Raspberry Pi | A running wlroots-compatible Wayland desktop, such as Raspberry Pi OS with labwc; `python3`, `wayvnc`, `grim`, and `wtype` |
| SSH connection | A trusted host key and login without a password prompt; log in as the **same user who owns the graphical session** |
| Client computer | Python 3.11+, OpenSSH, [uv](https://docs.astral.sh/uv/), and Git |
| Assistant application | A local stdio MCP client that supports image content and tool calls, such as Codex |

No Python packages are installed on the Pi. If its desktop tools are missing, install them there:

```sh
sudo apt-get install --no-install-recommends wayvnc grim wtype
```

Raspberry Pi OS Lite alone has no desktop to capture. A headless Pi still needs a running graphical session with an output configured by its compositor. This MCP integration is separate from Codex's native Computer Use tool.

## Quick start

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

- The bridge runs as the SSH user. It adds no network listener and uses an owned private WayVNC UNIX socket. OpenSSH retains your keys and authenticates the host.
- Source hashes detect deployment drift; they do not attest that the remote machine is trustworthy. Use only a Pi and SSH account you trust.
- Screenshots and typed content are sent to the MCP client and may be processed by its AI service. The server does not retain a screenshot history; explicit exports and opt-in verification scripts can save images locally.
- Input is never automatically replayed. After uncertain delivery, inspect a fresh full-desktop screenshot before deciding whether to repeat an action.
- Reports and screenshots under `_local/`, the `.venv/` environment, and `.env` files are ignored by Git. Exports saved elsewhere can be tracked: keep credentials and personal captures out of commits, issues, and pull requests.

See [architecture and trust boundaries](docs/architecture.md) for the protocol, process lifecycle, and data flow.

## Remote access and current limits

For use away from home, configure the SSH alias to a reachable VPN/mesh address or another working SSH route. A `.local` hostname provides LAN discovery, not worldwide access. VPN login or ping alone does not prove SSH reachability; test SSH from the network where you will run the client.

The tested setup uses Raspberry Pi 5, Raspberry Pi OS / Debian 13, labwc, and a 1920 × 1080 output. Off-network SSH was not verified in that setup. Cross-window first-click delivery can depend on compositor focus; move and observe before clicking. A visual stability result means sampled pixels matched, not that an application is ready. See the [verification record](docs/verification.md) for evidence and remaining limits.

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
uv build
```

The unit suite covers protocol framing, image validation, bounds, concurrency, cancellation, session release, deployment, and recovery. CI runs on Windows with Python 3.14 and Ubuntu with Python 3.11.

The live tests operate a real Pi desktop through the MCP SDK:

```sh
uv run python scripts/live_smoke.py --host pi-desktop
uv run python scripts/live_views.py --host pi-desktop
uv run python scripts/live_stability.py --host pi-desktop
uv run python scripts/live_lifecycle.py --host pi-desktop
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
