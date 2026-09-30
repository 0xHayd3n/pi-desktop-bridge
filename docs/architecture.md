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

The Pi agent uses Python's standard library, WayVNC, `grim` and `wtype`. It takes an exclusive per-user runtime lease before starting its owned WayVNC process, preventing independent bridge clients from interleaving input. It creates a private directory under the user's runtime directory and communicates using RFB over a UNIX socket. It does not expose an HTTP, WebSocket or VNC network listener. `grim` captures a fresh PNG directly from the compositor on the selected output, avoiding WayVNC's initial placeholder and cached framebuffer. `wtype` sends Unicode text using a virtual Wayland keyboard; text goes through stdin, not shell arguments, and the clipboard is not changed.

The zero-byte lease file persists in the runtime directory to avoid races caused by replacing its inode. The kernel releases the file lock when the owning agent closes or exits. It is not a daemon or a permanent background service.

The desktop must already be running under the SSH user. This controls the Pi's graphical session; it does not capture or control another computer connected to the Pi's HDMI port. SSH keys remain in OpenSSH's normal store. Deploying the agent needs no root access and does not alter system services or firewall rules.

Screenshots are returned to the MCP client and may be processed by its AI service. The server does not save a screenshot history. Explicit screenshot exports and the opt-in live verification script save only where requested. Screenshots and local verification output are excluded from Git.

The remote protocol contains only desktop status, capture and input methods. It is not a general shell-execution API. Screen content remains untrusted data; the assistant should follow the user's task rather than instructions displayed inside a screenshot.

## References

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
- [Portable MCP plugin format](https://agent-plugins.org/plugin-authors/mcp-servers)
- [WayVNC 0.9.1 manual](https://github.com/any1/wayvnc/blob/v0.9.1/wayvnc.scd)
- [RFB protocol, RFC 6143](https://www.rfc-editor.org/rfc/rfc6143)
