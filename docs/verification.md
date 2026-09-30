# Verification record

Version 0.2.0 verified on 30 September 2026 against a Raspberry Pi 5 running Raspberry Pi OS / Debian 13 with labwc 0.9.2, WayVNC 0.9.1, grim 1.4 and wtype 0.4. The active output was 1920 × 1080.

| Check | Result |
| --- | --- |
| Full unit suite, Windows Python 3.14 | 108 run; 105 passed, 3 Linux-only tests skipped |
| Full unit suite, Windows Python 3.11 | 108 run; 105 passed, 3 Linux-only tests skipped |
| Agent suite on the actual Pi | All 69 passed, including real Linux file-lock contention, process-kill recovery and symlink rejection |
| Agent deployment | Remote SHA-256 matched the local packaged agent |
| Real MCP stdio client | Initialized and listed all ten tools |
| Lease-free diagnostics | Health checks left the session inactive and available to another client; CLI doctor reported matching source and ready prerequisites |
| Screenshot | Returned a model-visible, valid PNG of the actual desktop |
| Mouse input | Click reached a target away from the previous cursor; drag started and ended at the requested coordinates |
| Text and keyboard | Unicode em dash, café and checkmark received exactly; Control+a selected text for replacement |
| Scrolling | Two requested wheel ticks received at explicit coordinates as the first input after reconnecting |
| Invalid input | Rejected first action left the desktop available; rejection during a healthy session preserved that session |
| First request in a new connection | Click succeeded before any explicit status or screenshot call |
| Two independent SSH clients | Second client was rejected while the first held the desktop; disconnect was idempotent and another client could acquire the desktop after release |
| MCP concurrency and cancellation | Real stdio client with fake desktop transport exercised shared queue deadlines, zero-delivery queued cancellation, active cancellation and fresh-observation guards |
| Display changes and uncertain delivery | Fault-injection tests rejected changed output identity, dimensions and power before input; ambiguous outcomes and failed post-action captures required a successful explicit screenshot |
| WayVNC startup | Real startup and capture passed; bounded startup-only polling handles initially unknown output power metadata |
| Configured Codex launcher | Enabled entry loaded; its exact executable and arguments initialized ten tools and captured the Pi from an unrelated working directory |
| Package build | Source distribution and wheel built successfully |
| Live fixture cleanup | Temporary test windows and their files removed |

The real test is `scripts/live_smoke.py`; screenshots and machine-readable reports remain under ignored `_local/`. The resulting image was also inspected visually to confirm the entered text, click receipt and drag line. Cancellation and geometry-change failures were simulated; the live acceptance test did not change the Pi's physical display configuration or interrupt a real user action.

Codex's tool catalog needs a server/app reload after adding or upgrading an MCP entry. The configured launcher was verified through the official MCP SDK; this record does not claim that the current chat hot-loaded new tools.

Tests used an SSH alias reachable on the home network. Worldwide use requires a working VPN/mesh SSH route. The Pi and Windows computer are signed into Tailscale, but SSH through that route remains unverified from this command runner. The bridge does not change firewall rules.

The local repo marketplace was parsed successfully by Codex's plugin loader as version 0.2.0. Portable plugin files are included; direct MCP configuration is the verified installation.
