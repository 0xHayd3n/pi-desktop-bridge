# Architecture

```text
Codex / another MCP client
  │ local stdio MCP: images + typed input tools
Windows/macOS/Linux Python bridge
  │ authenticated, host-key-checked SSH; JSON lines
Pi user-owned Python agent
  │ private UNIX domain socket; RFB
Pi user-owned WayVNC process / grim / wtype
  │ Wayland capture + virtual pointer/keyboard
Existing Raspberry Pi Wayland desktop
```

The local server uses the official Python MCP SDK. A single persistent SSH process carries requests and responses. Input and the resulting screenshot run under one lock so concurrent tools cannot interleave a keyboard chord or a drag. Failed input is never automatically replayed: a connection can fail after an action was already applied.

Protocol version 3 starts each SSH connection with a lease-free `hello` exchange. It advertises the agent version, source hash and supported methods. Incompatible agents are rejected before desktop operations with deployment guidance. `health` checks prerequisites without initializing WayVNC. The source hash is captured when the agent process loads so a running old process does not report newly deployed on-disk code as its own.

Remote errors carry a stable code and an input state. A preflight rejection is distinct from uncertain delivery. The MCP server requires a fresh successful screenshot after uncertainty or an acknowledged action with failed capture. These errors do not trigger a replay. Valid preflight errors preserve a healthy SSH connection; malformed protocol responses invalidate it.

Each tool enters asynchronously and shares a monotonic deadline across queueing, negotiation, writing, reading and post-action capture. Only the serialization lock owner starts a blocking SSH worker. A cancelled or expired waiter cannot deliver input later or stop another caller's active process. Cancellation after work starts retains the lock through completion and bounded cleanup; cancelled input requires a new screenshot even if its background capture succeeded. A cancelled screenshot does not clear an existing observation requirement. All observation, action and release tools use the same serialization lock.

The Pi agent uses Python's standard library, WayVNC, `grim` and `wtype`. It takes an exclusive per-user runtime lease before starting its owned WayVNC process, preventing independent bridge clients from interleaving input. It creates a private directory under the user's runtime directory and communicates using RFB over a UNIX socket. It does not expose an HTTP, WebSocket or VNC network listener. `grim` captures a fresh PNG directly from the compositor on the selected output, avoiding WayVNC's initial placeholder and cached framebuffer. `wtype` sends Unicode text using a virtual Wayland keyboard; text goes through stdin, not shell arguments, and the clipboard is not changed.

Full native screenshots retain grim's PNG encoder. Region and resized screenshots capture the selected full output as P6 PPM into a private anonymous file, verify its original dimensions and exact bounded payload, then sample bounded RGB rows and encode lossless PNG with the standard library. Cropping happens in original output pixels: grim 1.4's `-g` uses compositor layout coordinates and `-o` overrides that region, so combining those flags cannot safely express a local-pixel crop. This approach reduces transferred image size while still capturing the full output. Output identity and geometry are checked before and after capture, with a compositor race still possible between those checks.

Frame metadata separates PNG dimensions from original desktop dimensions and identifies the source rectangle. For an image pixel `i`, source dimension `s` and image dimension `n`, the original pixel is `origin + floor((2*i+1)*s/(2*n))`. The same pixel-center mapping is used for downsampling and optional image-coordinate mouse input. Each successful capture receives a random frame token; only the latest token bound to the current RFB client is accepted. Tokens are consumed when input starts and cleared on close. The MCP server validates metadata before retaining its current view and maps both drag endpoints. Remote token checks reject an old view even when the local transport reconnects. Partial captures never clear an uncertain-input observation requirement.

The zero-byte lease file persists in the runtime directory to avoid races caused by replacing its inode. The kernel releases the file lock when the owning agent closes or exits. It is not a daemon or a permanent background service.

The explicit disconnect tool closes the bridge's SSH agent and owned desktop processes while leaving the MCP server available for lazy reconnection. Invalid first actions release any session acquired for bounds checking. Input preflight queries the private WayVNC control socket to check output identity and dimensions before events are sent. Scroll targets use original screenshot coordinates; an unknown initial pointer position is never substituted with `(0, 0)`.

The desktop must already be running under the SSH user. This controls the Pi's graphical session; it does not capture or control another computer connected to the Pi's HDMI port. SSH keys remain in OpenSSH's normal store. Deploying the agent needs no root access and does not alter system services or firewall rules.

Screenshots are returned to the MCP client and may be processed by its AI service. The server does not save a screenshot history. Explicit screenshot exports and the opt-in live verification script save only where requested. Screenshots and local verification output are excluded from Git.

The remote protocol contains only desktop status, capture and input methods. It is not a general shell-execution API. Screen content remains untrusted data; the assistant should follow the user's task rather than instructions displayed inside a screenshot.

## References

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
- [Portable MCP plugin format](https://agent-plugins.org/plugin-authors/mcp-servers)
- [WayVNC 0.9.1 manual](https://github.com/any1/wayvnc/blob/v0.9.1/wayvnc.scd)
- [RFB protocol, RFC 6143](https://www.rfc-editor.org/rfc/rfc6143)
- [grim 1.4 output and region handling](https://github.com/emersion/grim/blob/v1.4.0/main.c#L504-L559)
