# Verification record

Version 0.6.0 verified on 30 September 2026 on the same Raspberry Pi 5 / Debian 13 / labwc setup described below. Protocol remains 4; the packaged and deployed agent SHA-256 is `f952f1022e4e4d61f72c579e8ffed828b74fc5a547ff603ae3ff2ea990a3dd88`.

| v0.6 check | Result |
| --- | --- |
| Full unit suites, Windows Python 3.14 and 3.11 | 303 tests each; 293 passed, ten Linux-only tests skipped |
| Agent suite on Linux | All 128 passed on the actual Pi against the final source in 0.956 seconds, including real file locks, sockets, relay backpressure and owned process cleanup |
| Dashboard access and request boundaries | Twenty-six tests cover Bearer/Host/Origin checks, bounded JSON, serialized operations, heartbeat release, cancellation, stale sessions and geometry, uncertain input, and both stages of bounded click preparation |
| GUI SSH transport | Fifteen tests on both Python versions use a disposable real Paramiko SSH server: unknown/changed host keys, password isolation, fixed deployment/checksum, response deadlines, EOF ambiguity, cleanup and input sent once |
| Exec acknowledgment deadlines | A server withholding its SSH exec acknowledgment was independently reproduced; the repaired startup and deployment returned in 0.153/0.166 seconds for 0.15-second budgets, with no surviving exec worker |
| Live localhost dashboard API | All eight checks passed: unauthorized/foreign-origin rejection, saved-alias login and deployment, valid 960 × 540 PNG from the 1920 × 1080 desktop, click, Unicode and hotkey replacement, scrolling, exact drag endpoints, disconnect and stale-session rejection |
| Live direct GUI transport | Paramiko authenticated with existing local keys, deployed the fixed agent, verified its source and captured a valid PNG from the actual Pi |
| Continuous desktop stream | Source-checked agent opens a private WayVNC socket and relays raw RFB; no grim screenshot polling or capture before clicking. Native WebSocket did not open in the tested Codex browser, while authenticated HTTP streamed ten separate probe chunks in about 0.97 seconds; the viewer uses HTTP streaming |
| Stream transport checks | Twenty-two tests cover strict metadata, partial writes, bounded queues, real SSH-window backpressure, stalled writes/rekey/cleanup and real owned-process termination; five alias/process checks also passed on WSL |
| HTTP stream checks | Sixteen tests cover generation-bound tickets, stale identities, bounded bodies/queues, delivery acknowledgments, cancelled input, startup/disconnect races, output stalls and joined cleanup; twelve dashboard stream tests cover exclusivity and lifecycle |
| Codex browser controls | Minimal login opens the full-window stream. Ordinary key events produced exact text; ASCII paste, Control+a, click, scroll and drag reached the owned fixture. Unsupported Unicode paste was rejected before any text was sent. F6 focused Disconnect, and one click returned to login |
| Browser gesture checks | Node checks cover raw ordered input, queue limits, lifecycle races, held-input release, clipboard invalidation, unsupported paste and actual SetEncodings wire bytes; only Fence is omitted, while ContinuousUpdates remains |
| Configured MCP launcher | Exact existing executable/arguments initialized all eleven tools from an unrelated working directory; native, overview and waited captures passed |
| Package and isolated consumer | Source distribution and wheel byte-match all current source/web assets; an installed wheel from an unrelated working directory loads the UI routes, serves all 67 web files with matching bytes/MIME types, and passes dependency checks |
| Publication privacy | Gitleaks 8.30.1 found no secrets in the candidate files or full Git history; private captures, reports, credentials and local connection details remain outside the release |

The password/first-trust flow was verified against the disposable local SSH server. Actual Pi login used its already trusted local SSH credentials. No new password was requested in chat or written to configuration, and host-key stores were not changed. Live tests used owned disposable test windows; their screenshots and reports remain under ignored `_local/`. The viewer is separate from Codex's native Computer Use backend. Off-network SSH remains unverified. An intermittent LAN `.local` lookup failure was bypassed only in an ignored diagnostic process with the same strict known-host identity; this does not establish worldwide reachability.

The former PNG viewer ran at about 1.3 fps. The continuous stream with a 60 fps capture cap and Fence enabled displayed 129 changed frames over 5.079 seconds (25.4 updates/s). Omitting Fence displayed 899 painted frames over 29.967 seconds (30.0 updates/s), using a counter without canvas pixel readback. The fixture's actual pixels changed 185 times in 3.000 seconds (61.7 Hz). A 120 fps capture-cap experiment still displayed about 30 updates/s. Requesting captures promptly with a 1000 fps ceiling yielded 4549 completed nonempty noVNC canvas paints over 80.959 seconds (56.2 updates/s). A fresh session on the final deployed source yielded 2530 paints over 51.763 seconds (48.9 updates/s). This counts rendered canvas updates, rather than physical monitor presentation. These are measurements of this Pi, screen activity and LAN route; a configured maximum is not a guarantee of 60 displayed fps.

The high-ceiling small-patch animation used about 3.0% of one Pi CPU core in WayVNC and 0.6% in the stream agent over five seconds. With animation idle, WayVNC used about 0.2% and the agent accumulated no jiffy at the 0.2% measurement resolution. This checks idle spinning on this setup; it does not characterize full-screen video load.

Five key-to-known-pixel samples were 350.2, 78.8, 75.5, 80.5 and 90.6 ms; the first was a cold sample. A later fresh-session sample was 103 ms. The fixture confirms received input; timing instrumentation identifies its green/magenta patch after rendering. No input is automatically replayed. Streaming events have different delivery semantics from the separate MCP snapshot tools; cross-compositor focus remains a practical limit.

The final deployed source returned key-to-known-patch samples of 99.6, 97.4, 51.0, 55.9 and 67.7 ms (median 67.7 ms). That probe reads pixels only while a latency sample is pending; the displayed-rate counter performs no pixel readback. Final keyboard input and F6 focus escape were also exercised on that source.

## Retained v0.5 and earlier evidence

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
