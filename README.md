# Pi Desktop Bridge

An MCP server that lets Codex and other MCP-enabled assistants see and operate a Raspberry Pi's Wayland desktop through an existing SSH connection.

The bridge returns real PNG screenshots to the model and provides mouse movement, clicks, dragging, scrolling, text entry and keyboard shortcuts. Input tools return a fresh screenshot after the action. It runs over SSH and uses a private WayVNC UNIX socket on the Pi; no VNC or web port is opened.

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
uv run pi-desktop-bridge status --host pi-desktop
```

`pi-desktop` is an example SSH alias. Substitute your working alias, or create a concrete `Host pi-desktop` entry in your SSH config. The deployment copies the agent into `~/.local/share/pi-desktop-bridge` under the SSH user, checks the file's SHA-256, and needs no `sudo`.

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
| `desktop_status` | Desktop size and connection information |
| `desktop_screenshot` | Current PNG screenshot |
| `desktop_move` | Move to absolute screenshot pixel coordinates |
| `desktop_click` | Left, middle or right click; single or double |
| `desktop_drag` | Drag between two screenshot coordinates |
| `desktop_scroll` | Scroll up, down, left or right |
| `desktop_type` | Type text into the focused app |
| `desktop_key` | Press a key or shortcut, for example `["Control_L", "a"]` |

Coordinates use the original screenshot's dimensions and top-left origin. Take a screenshot before choosing a target. The tools operate the active desktop, so a local person moving focus can change where subsequent input goes. Keyboard behavior depends on the target app and compositor; the live verification checks the actual Pi rather than inferring behavior from protocol messages.

Only one bridge session can control a desktop user at a time. A second client receives a busy error until the first client closes its bridge connection. This prevents separate assistants from interleaving input. Text is delivered through `wtype`'s virtual keyboard without replacing the clipboard. Screenshots use `grim` to capture the compositor directly, avoiding WayVNC's startup placeholder and cached frames.

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
```

The first command uses fake protocol peers to check framing, bounds, PNG pixels, held-input release, concurrency, deployment and error handling. The second is an **opt-in real desktop test**: it opens a disposable Tk window on the Pi, exercises the real MCP stdio client and desktop input, then closes and removes the fixture. It needs `tkinter` and an Xwayland display at `:0` on the Pi. Screenshots and the report are saved under ignored `_local/live/` and are not published.

See the [verification record](docs/verification.md) for actual checks and remaining integration limits.

## Troubleshooting and removal

- **SSH failure:** run `ssh <alias>` in a normal terminal. The bridge uses `BatchMode=yes` and `StrictHostKeyChecking=yes`; it will not request a password or silently trust a new host key.
- **No desktop/output:** log into the Pi's supported Wayland desktop. Raspberry Pi OS Lite alone has no desktop to capture. The bridge reports missing Wayland/WayVNC support instead of installing a desktop or changing login settings.
- **Connection lost after input:** the action may already have happened. Take another screenshot and assess the result before retrying. The bridge never automatically replays input.
- **Using it away from home:** the SSH alias must resolve to a reachable VPN/mesh address or another SSH route. A `.local` LAN hostname alone does not provide worldwide access. Tailscale login and ping do not prove that SSH is allowed from the computer running the client.
- **Disable:** use `codex mcp remove pi_desktop` or disable the server in your client. Closing the MCP server stops its SSH agent and owned WayVNC process; no system service is installed.
- **Uninstall the Pi agent:** after stopping clients, remove only `~/.local/share/pi-desktop-bridge` under the SSH user. This does not remove WayVNC, SSH or their existing configuration.

See [architecture and data handling](docs/architecture.md) for the transport, process lifecycle and trust boundaries.
