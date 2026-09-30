# Verification record

Version 0.5.0 verified on 30 September 2026 against a Raspberry Pi 5 running Raspberry Pi OS / Debian 13 with labwc 0.9.2, WayVNC 0.9.1, grim 1.4 and wtype 0.4. The active output was 1920 × 1080. Protocol remains 4; the final agent source SHA-256 was `5081807f3abaf3d428ba01f2894c2674d7e5822944eb0fe158f3f3e3d737731e`.

| Check | Result |
| --- | --- |
| Full unit suite, Windows Python 3.14 | 195 run; 191 passed, 4 Linux-only tests skipped |
| Full unit suite, Windows Python 3.11 | 195 run; 191 passed, 4 Linux-only tests skipped |
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
| Real same-protocol stale agent | Before deployment, the v0.4 Pi agent stayed available to health/doctor while status, capture and click requests were rejected as agent_source_mismatch/not_started; its session stayed inactive |
| Coherent startup source | A real child process atomically replaced its source during startup with different source of the same version; hello still identified the executed snapshot and the pipe methods log contained only hello/health/disconnect |
| Real idle release | Two-second test policy released the Pi during repeated health polls; the existing second SSH client then acquired it; the old image view was rejected |
| Reconnection and long active work | Fresh capture reconnected and mapped pointer movement worked; a 2.278-second visual wait exceeded the two-second idle interval without interruption and renewed the interval after completion |
| Disabled policy and shutdown | Idle timeout zero retained the real lease for three seconds; normal stdio shutdown then let the second client acquire it |
| Cancellation and release failures | Real SDK and in-process tests cover queued cancellation without renewal, active cancellation through worker completion, guard retention after auto-release, repeated shutdown cancellation and one quiet release attempt after failure |
| Package build | Source distribution and wheel built successfully |
| Live fixture cleanup | Temporary test windows and their files removed |

For v0.5, `scripts/live_smoke.py` and `scripts/live_lifecycle.py` passed all 13 and 8 checks respectively through the new snapshot launcher. The configured launcher also returned native, overview and waited images with the default 300-second policy from an unrelated working directory. Earlier v0.4 runs of `scripts/live_views.py` and `scripts/live_stability.py` passed all 8 and 9 checks; their unchanged crop/resizing/input and sampling results are retained below. The full current suites rerun the fake-peer coverage for those behaviors. Screenshots and reports remain under ignored `_local/`; images were decoded independently and inspected visually. Cancellation, uncertain delivery and geometry changes were simulated. Live tests did not change the Pi's physical display configuration or interrupt a real user action. Independent Astra review inspected the final implementation, reproduced the startup-source race, reviewed its fix, and reran all 21 transport and 50 server tests.

The views test used a pointer already inside the disposable fixture. Entering that fixture directly from the desktop produced pointer-entry events without button events; explicitly moving within the fixture before clicking worked. A separate native-coordinate probe also reproduced this at 50, 150 and 300 ms motion delays, so increased delay was not treated as a fix. The precise compositor/focus cause remains unconfirmed. Cross-window first-click delivery is not guaranteed; move into the target window and inspect its screenshot before deciding on further input. The bridge never retries input automatically.

Strict crop equality was checked against a full-resolution PPM-derived capture, using stable RGB tiles below text. Separately rendered native grim PNG and PPM captures showed occasional one-unit RGB differences along antialiased text and tile fringes. This record does not claim byte-identical RGB between those two native formats or across changing frames.

The retained v0.4 measurement used three captures per mode over the same LAN connection and fixture. The overview transferred 70.2% fewer PNG bytes and completed about 24.5% faster than the native full capture in this sample; these are measurements of this screen and route, not general latency guarantees. In the separate stability fixture, six 960-pixel post-action images were 538,673–540,198 bytes each, while a full screenshot was 1,813,902 bytes. The tested Codex launcher used a 960-pixel action-image default; unconfigured launchers retain native width.

| Capture | PNG dimensions | Median PNG bytes | Median request time |
| --- | --- | ---: | ---: |
| Native full | 1920 × 1080 | 1,810,230 | 0.5238 s |
| Overview | 960 × 540 | 539,160 | 0.3954 s |
| Region detail | 520 × 80 | 2,334 | 0.0424 s |

Stability compares native ROI pixels even when the returned PNG is downscaled. It reports equality across samples, not application readiness. Captures between polls may be missed; cursor blinking, animation and slight compositor-rendering differences can prevent equality. Session startup, helper cleanup, final PNG encoding and transfer are excluded from the reported sampling duration and remain inside the overall tool deadline. Two private bounded PPM files retain the latest valid sample while a candidate is captured; a sampling timeout does not promote an unfinished candidate.

Codex's tool catalog needs a server/app reload after adding or upgrading an MCP entry. The configured launcher was verified through the official MCP SDK; this record does not claim that the current chat hot-loaded new tools.

Tests used an SSH alias reachable on the home network. Worldwide use requires a working VPN/mesh SSH route. Off-network SSH through a VPN/mesh route was not verified in this setup. The bridge does not change firewall rules.

The local repo marketplace was parsed successfully by Codex's plugin loader as version 0.5.0. Portable plugin files are included; direct MCP configuration is the verified installation.
