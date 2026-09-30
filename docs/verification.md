# Verification record

Version 0.3.0 verified on 30 September 2026 against a Raspberry Pi 5 running Raspberry Pi OS / Debian 13 with labwc 0.9.2, WayVNC 0.9.1, grim 1.4 and wtype 0.4. The active output was 1920 × 1080.

| Check | Result |
| --- | --- |
| Full unit suite, Windows Python 3.14 | 126 run; 123 passed, 3 Linux-only tests skipped |
| Full unit suite, Windows Python 3.11 | 126 run; 123 passed, 3 Linux-only tests skipped |
| Agent suite on the actual Pi | All 79 passed, including real Linux file-lock contention, process-kill recovery and symlink rejection |
| Agent deployment | Remote SHA-256 matched the local packaged agent |
| Real MCP stdio client | Initialized and listed all ten tools |
| Lease-free diagnostics | Health checks left the session inactive and available to another client; CLI doctor reported matching source and ready prerequisites |
| Screenshot | Returned a model-visible, valid PNG of the actual desktop |
| Region pixel fidelity | Entire 520 × 80 crop matched a full-resolution PPM-derived reference through an independent PNG decoder; four known RGB/white tile interiors also matched |
| Image-coordinate input | A 960 × 540 overview click activated the intended button; a native crop drag reached exact fixture coordinates; scrolling through a resized crop delivered two ticks |
| Fresh-view enforcement | Reusing the overview after input was rejected; disconnect invalidated a view without reacquiring the desktop |
| PNG and typed-argument rejection | Fake-peer tests rejected valid-CRC malformed raster data without clearing the observation guard; actual SDK tool entry rejected boolean/string/float numeric arguments before transport |
| Mouse input | Click reached a target away from the previous cursor; drag started and ended at the requested coordinates |
| Text and keyboard | Unicode em dash, café and checkmark received exactly; Control+a selected text for replacement |
| Scrolling | Two requested wheel ticks received at explicit coordinates as the first input after reconnecting |
| Invalid input | Rejected first action left the desktop available; rejection during a healthy session preserved that session |
| First request in a new connection | Click succeeded before any explicit status or screenshot call |
| Two independent SSH clients | Second client was rejected while the first held the desktop; disconnect was idempotent and another client could acquire the desktop after release |
| MCP concurrency and cancellation | Real stdio client with fake desktop transport exercised shared queue deadlines, zero-delivery queued cancellation, active cancellation and fresh-observation guards |
| Display changes and uncertain delivery | Fault-injection tests rejected changed output identity, dimensions and power before input; ambiguous outcomes and failed post-action captures required a successful explicit screenshot |
| WayVNC startup | Real startup and capture passed; bounded startup-only polling handles initially unknown output power metadata |
| Configured Codex launcher | Enabled entry loaded; its exact executable and arguments initialized ten tools, exposed region/view schemas, and captured native and 960 × 540 images from an unrelated working directory |
| Package build | Source distribution and wheel built successfully |
| Live fixture cleanup | Temporary test windows and their files removed |

The real tests are `scripts/live_smoke.py` and `scripts/live_views.py`; screenshots and machine-readable reports remain under ignored `_local/`. The resulting images were also inspected visually. Cancellation and geometry-change failures were simulated; the live acceptance tests did not change the Pi's physical display configuration or interrupt a real user action.

The views test used a pointer already inside the disposable fixture. Entering that fixture directly from the desktop produced pointer-entry events without button events; explicitly moving within the fixture before clicking worked. A separate native-coordinate probe also reproduced this at 50, 150 and 300 ms motion delays, so increased delay was not treated as a fix. The precise compositor/focus cause remains unconfirmed. Cross-window first-click delivery is not guaranteed; move into the target window and inspect its screenshot before deciding on further input. The bridge never retries input automatically.

Strict crop equality was checked against a full-resolution PPM-derived capture, using stable RGB tiles below text. Separately rendered native grim PNG and PPM captures showed occasional one-unit RGB differences along antialiased text and tile fringes. This record does not claim byte-identical RGB between those two native formats or across changing frames.

Three captures per mode over the same LAN connection and fixture produced these medians. The overview transferred 70.2% fewer PNG bytes and completed about 21.6% faster than the native full capture in this sample; these are measurements of this screen and route, not general latency guarantees. Input tools still return native full screenshots.

| Capture | PNG dimensions | Median PNG bytes | Median request time |
| --- | --- | ---: | ---: |
| Native full | 1920 × 1080 | 1,810,790 | 0.5265 s |
| Overview | 960 × 540 | 539,372 | 0.4130 s |
| Region detail | 520 × 80 | 2,217 | 0.0405 s |

Codex's tool catalog needs a server/app reload after adding or upgrading an MCP entry. The configured launcher was verified through the official MCP SDK; this record does not claim that the current chat hot-loaded new tools.

Tests used an SSH alias reachable on the home network. Worldwide use requires a working VPN/mesh SSH route. The Pi and Windows computer are signed into Tailscale, but SSH through that route remains unverified from this command runner. The bridge does not change firewall rules.

The local repo marketplace was parsed successfully by Codex's plugin loader as version 0.3.0. Portable plugin files are included; direct MCP configuration is the verified installation.
