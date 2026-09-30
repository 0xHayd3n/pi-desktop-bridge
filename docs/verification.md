# Verification record

Version 0.4.0 verified on 30 September 2026 against a Raspberry Pi 5 running Raspberry Pi OS / Debian 13 with labwc 0.9.2, WayVNC 0.9.1, grim 1.4 and wtype 0.4. The active output was 1920 × 1080. The final agent source SHA-256 was `f23588d013cf1090ae46786e7f9ad868703a39b9c5f8536166754e8da15d0614`.

| Check | Result |
| --- | --- |
| Full unit suite, Windows Python 3.14 | 172 run; 168 passed, 4 Linux-only tests skipped |
| Full unit suite, Windows Python 3.11 | 172 run; 168 passed, 4 Linux-only tests skipped |
| Agent suite on the actual Pi | All 114 passed, including real Linux file-lock contention, process-kill recovery, symlink rejection and child file-size limits |
| Agent deployment | Remote SHA-256 matched the local packaged agent |
| Real MCP stdio client | Initialized and listed all eleven tools |
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
| Configured Codex launcher | Enabled entry loaded; exact executable/arguments initialized eleven tools from an unrelated working directory, exposed action sizing and wait schemas, captured native and 960 × 540 images, and returned a validated wait image |
| Post-action image sizing | All six input tools returned 960 × 540 full-source images; per-call 480-pixel override worked; explicit screenshot remained 1920 × 1080 |
| Repainting and visual stability | Owned patch changed then settled; wait returned a 10 × 10 image containing exactly the expected green pixels after 871 ms and ten valid samples |
| Animated wait timeout | A continuously changing patch returned a valid final sample with stable=false, timed_out=true and elapsed_ms=1200; thirteen samples were accepted |
| ROI exclusion and wait views | Unrelated animation did not prevent a quiet ROI settling; a wait view supported mapped input and was rejected after consumption |
| Terminal sampling deadline | Live timeout reproduced an initial failure; deterministic deadline-enforcing regressions cover expiry during grim and pre/post geometry queries, retained valid-image identity, malformed candidates and zero-valid-sample errors |
| Recovery visibility | Status/health exposed local recovery flags; fault-injection tests confirmed stable and timed-out waits preserve uncertain-input guards |
| Package build | Source distribution and wheel built successfully |
| Live fixture cleanup | Temporary test windows and their files removed |

The real tests are `scripts/live_smoke.py`, `scripts/live_views.py` and `scripts/live_stability.py`; all 13, 8 and 9 checks respectively passed. Screenshots and machine-readable reports remain under ignored `_local/`. Images were decoded independently to verify pixels; screenshot output was also inspected visually. Cancellation, uncertain delivery and geometry changes were simulated; live acceptance tests did not change the Pi's physical display configuration or interrupt a real user action. An independent Astra review inspected the implementation, reproduced the deadline bug, reviewed its fix, and reran focused agent/server/CLI suites.

The views test used a pointer already inside the disposable fixture. Entering that fixture directly from the desktop produced pointer-entry events without button events; explicitly moving within the fixture before clicking worked. A separate native-coordinate probe also reproduced this at 50, 150 and 300 ms motion delays, so increased delay was not treated as a fix. The precise compositor/focus cause remains unconfirmed. Cross-window first-click delivery is not guaranteed; move into the target window and inspect its screenshot before deciding on further input. The bridge never retries input automatically.

Strict crop equality was checked against a full-resolution PPM-derived capture, using stable RGB tiles below text. Separately rendered native grim PNG and PPM captures showed occasional one-unit RGB differences along antialiased text and tile fringes. This record does not claim byte-identical RGB between those two native formats or across changing frames.

Three captures per mode over the same LAN connection and fixture produced these medians. The overview transferred 70.2% fewer PNG bytes and completed about 24.5% faster than the native full capture in this sample; these are measurements of this screen and route, not general latency guarantees. In the separate stability fixture, six 960-pixel post-action images were 538,673–540,198 bytes each, while a full screenshot was 1,813,902 bytes. Your local Codex launcher is configured with a 960-pixel action-image default; unconfigured launchers retain native width.

| Capture | PNG dimensions | Median PNG bytes | Median request time |
| --- | --- | ---: | ---: |
| Native full | 1920 × 1080 | 1,810,230 | 0.5238 s |
| Overview | 960 × 540 | 539,160 | 0.3954 s |
| Region detail | 520 × 80 | 2,334 | 0.0424 s |

Stability compares native ROI pixels even when the returned PNG is downscaled. It reports equality across samples, not application readiness. Captures between polls may be missed; cursor blinking, animation and slight compositor-rendering differences can prevent equality. Session startup, helper cleanup, final PNG encoding and transfer are excluded from the reported sampling duration and remain inside the overall tool deadline. Two private bounded PPM files retain the latest valid sample while a candidate is captured; a sampling timeout does not promote an unfinished candidate.

Codex's tool catalog needs a server/app reload after adding or upgrading an MCP entry. The configured launcher was verified through the official MCP SDK; this record does not claim that the current chat hot-loaded new tools.

Tests used an SSH alias reachable on the home network. Worldwide use requires a working VPN/mesh SSH route. The Pi and Windows computer are signed into Tailscale, but SSH through that route remains unverified from this command runner. The bridge does not change firewall rules.

The local repo marketplace was parsed successfully by Codex's plugin loader as version 0.4.0. Portable plugin files are included; direct MCP configuration is the verified installation.
