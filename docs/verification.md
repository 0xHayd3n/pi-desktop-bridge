# Verification record

Verified on 30 September 2026 against a Raspberry Pi 5 running Raspberry Pi OS / Debian 13 with labwc 0.9.2, WayVNC 0.9.1, grim 1.4 and wtype 0.4. The active output was 1920 × 1080.

| Check | Result |
| --- | --- |
| Full unit suite, Windows Python 3.14 | 57 run; 54 passed, 3 Linux-only tests skipped |
| Full unit suite, Windows Python 3.11 | 57 run; 54 passed, 3 Linux-only tests skipped |
| Agent suite on the actual Pi | All 45 passed, including real Linux file-lock contention, process-kill recovery and symlink rejection |
| Agent deployment | Remote SHA-256 matched the local packaged agent |
| Real MCP stdio client | Initialized and listed all eight tools |
| Screenshot | Returned a model-visible, valid PNG of the actual desktop |
| Mouse input | Click reached a target away from the previous cursor; drag started and ended at the requested coordinates |
| Text and keyboard | Unicode em dash, café and checkmark received exactly; Control+a selected text for replacement |
| Scrolling | Two requested wheel ticks received |
| First request in a new connection | Click succeeded before any explicit status or screenshot call |
| Two independent SSH clients | Second client was rejected while the first held the desktop; it succeeded after release |
| Configured Codex launcher | Enabled entry loaded; its exact executable and arguments initialized eight tools and captured the Pi from an unrelated working directory |
| Package build | Source distribution and wheel built successfully |
| Live fixture cleanup | Temporary test windows and their files removed |

The real test is `scripts/live_smoke.py`; screenshots and machine-readable reports remain under ignored `_local/`. The resulting image was also inspected visually to confirm the entered text, click receipt and drag line.

Codex's native tool catalog needs a server/app reload after adding a new MCP entry. The configured launcher was verified through the official MCP SDK; this record does not claim that the current chat hot-loaded new tools.

Tests used an SSH alias reachable on the home network. Worldwide use requires a working VPN/mesh SSH route. The Pi and Windows computer are signed into Tailscale, but SSH through that route remains unverified from this command runner. The bridge does not change firewall rules.

The local repo marketplace was parsed successfully by Codex's plugin loader. Portable plugin files are included; direct MCP configuration is the verified installation.
