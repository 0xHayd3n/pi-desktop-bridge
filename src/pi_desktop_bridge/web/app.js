import RFB from "./vendor/novnc/core/rfb.js";
import { encodings } from "./vendor/novnc/core/encodings.js";
import { initLogging } from "./vendor/novnc/core/util/logging.js";

// Remote key and clipboard contents must never reach the browser console.
initLogging("none");

// NeatVNC requests a Fence reply after each frame when this optional encoding
// is advertised. On the HTTP channel, each reply needs a separate input POST.
// Keep all other encodings, including ContinuousUpdates, in their normal order.
const clientEncodings = RFB.messages.clientEncodings;
RFB.messages.clientEncodings = function (sock, offered) {
  return clientEncodings.call(this, sock, offered.filter((value) => value !== encodings.pseudoEncodingFence));
};

const $ = (id) => document.getElementById(id);
const ui = {
  form: $("connection-form"), loginView: $("login-view"), viewerView: $("viewer-view"),
  mode: $("connection-mode"), modeToggle: $("mode-toggle"), hostLabel: $("host-label"), portField: $("port-field"),
  connectionError: $("connection-error"), viewerError: $("viewer-error"), viewerErrorConnected: $("viewer-error-connected"),
  host: $("host"), username: $("username"), password: $("password"), port: $("port"), deploy: $("deploy"),
  credentialFields: $("credentials-fields"), connect: $("connect-button"), disconnect: $("disconnect-button"),
  connectedHost: $("connected-host"), status: $("connection-status"), dot: $("status-dot"),
  stage: $("screen-stage"), placeholder: $("screen-placeholder"), screenMessage: $("screen-message"),
  dialog: $("host-key-dialog"), trust: $("host-key-trust"), cancel: $("host-key-cancel"),
  keyHost: $("host-key-host"), keyAlgorithm: $("host-key-algorithm"), keyFingerprint: $("host-key-fingerprint")
};

const hash = new URLSearchParams(location.hash.slice(1));
const token = hash.get("token");
if (location.hash) history.replaceState(null, "", location.pathname + location.search);

let state = null;
let rfb = null;
let streamChannel = null;
let streamGeneration = null;
let streamPending = false;
let streamFailed = false;
let handshakeTimer = 0;
let lifecycle = 0;
let connectionPending = false;
let heartbeatPending = false;
let pendingTrust = null;
let pastePending = false;
let pasteOperation = 0;
let pointerHeld = false;
let pointerPosition = null;

ui.stage.dataset.documentHidden = String(document.hidden);
ui.stage.dataset.streamPhase = "idle";
ui.stage.dataset.transport = "http";
ui.stage.dataset.httpOpen = "false";
ui.stage.dataset.httpChunks = "0";
ui.stage.dataset.httpInputPosts = "0";
ui.stage.dataset.rfbCreated = "false";

function setMessage(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function setViewerError(message) {
  setMessage(ui.viewerError, message);
  setMessage(ui.viewerErrorConnected, message);
}

function friendlyError(data, fallback) {
  const code = data?.error?.code;
  if (code === "host_key_mismatch" || code === "host_key_changed") return "The Pi's SSH identity changed. Connection stopped. Verify its fingerprint outside this viewer before trying again.";
  if (code === "unauthorized" || code === "invalid_token") return "This viewer link has expired. Reopen it from Pi Desktop Bridge.";
  const message = data?.error?.message;
  return typeof message === "string" && message.length < 300 ? message : fallback;
}

async function api(path, body) {
  const headers = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? "GET" : "POST", headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store", credentials: "same-origin"
    });
  } catch {
    throw { error: { code: "network", message: "The viewer could not reach its local bridge. Check that it is still running." } };
  }
  let data;
  try { data = await response.json(); }
  catch { throw { error: { code: "response", message: "The bridge returned an unreadable response. Reopen the viewer to try again." } }; }
  if (!response.ok) throw data;
  return data;
}

// noVNC accepts a WebSocket-shaped raw channel. The browser's embedded
// WebSocket never reaches this localhost bridge, so this channel carries the
// same RFB byte stream over authenticated, same-origin HTTP requests.
class HttpRfbChannel {
  constructor(generation, ticket) {
    this.generation = generation;
    this.ticket = ticket;
    this.readyState = 0;
    this.protocol = "binary";
    this.binaryType = "arraybuffer";
    this.onerror = null;
    this.onmessage = null;
    this.onopen = null;
    this.onclose = null;
    this._abort = new AbortController();
    this._reader = null;
    this._streamId = null;
    this._queue = [];
    this._queuedBytes = 0;
    this._flushScheduled = false;
    this._flushing = false;
    this._closeNotified = false;
  }

  async start() {
    try {
      const response = await fetch("/api/stream-open", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ generation: this.generation, ticket: this.ticket }),
        signal: this._abort.signal, cache: "no-store", credentials: "same-origin"
      });
      this.ticket = null;
      if (!response.ok || !response.headers.get("content-type")?.startsWith("application/octet-stream")) throw new Error("stream_open_failed");
      const streamId = response.headers.get("X-Pi-Stream-Id");
      if (typeof streamId !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(streamId) || !response.body?.getReader) throw new Error("stream_header_failed");
      this._streamId = streamId;
      if (this.readyState !== 0) { this._bestEffortClose(); await response.body.cancel(); return; }
      this._reader = response.body.getReader();
      this.readyState = 1;
      ui.stage.dataset.httpOpen = "true";
      ui.stage.dataset.streamPhase = "rfb-handshake";
      this.onopen?.({ type: "open" });
      while (this.readyState === 1) {
        const { done, value } = await this._reader.read();
        if (this.readyState !== 1) break;
        if (done) throw new Error("stream_ended");
        if (!value?.byteLength) continue;
        for (let offset = 0; offset < value.byteLength && this.readyState === 1; offset += 65536) {
          const bytes = value.slice(offset, offset + 65536);
          ui.stage.dataset.httpChunks = String(Number(ui.stage.dataset.httpChunks) + 1);
          this.onmessage?.({ data: bytes.buffer });
        }
      }
    } catch {
      if (this.readyState < 2) this._fail();
    }
  }

  send(data) {
    if (this.readyState !== 1) throw new Error("Stream is not open");
    const length = data?.byteLength;
    if (!Number.isInteger(length) || length < 0 || this._queuedBytes + length > 262144) {
      this._fail();
      return;
    }
    if (!length) return;
    // noVNC immediately reuses its send queue after this call returns.
    const copy = new Uint8Array(data).slice();
    this._queue.push(copy);
    this._queuedBytes += length;
    this._scheduleFlush();
  }

  _scheduleFlush() {
    if (this._flushScheduled || this._flushing || this.readyState !== 1) return;
    this._flushScheduled = true;
    queueMicrotask(() => { this._flushScheduled = false; void this._flushInput(); });
  }

  _takeChunk() {
    const size = Math.min(65536, this._queue.reduce((sum, item) => sum + item.byteLength, 0));
    const chunk = new Uint8Array(size);
    let copied = 0;
    while (copied < size) {
      const item = this._queue[0];
      const take = Math.min(item.byteLength, size - copied);
      chunk.set(item.subarray(0, take), copied);
      copied += take;
      if (take === item.byteLength) this._queue.shift();
      else this._queue[0] = item.subarray(take);
    }
    return chunk;
  }

  async _flushInput() {
    if (this._flushing || this.readyState !== 1) return;
    this._flushing = true;
    try {
      while (this.readyState === 1 && this._queue.length) {
        const chunk = this._takeChunk();
        let deadline;
        const response = await Promise.race([fetch("/api/stream-input", {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "X-Pi-Stream-Id": this._streamId,
            "Content-Type": "application/octet-stream" },
          body: chunk, signal: this._abort.signal, cache: "no-store", credentials: "same-origin"
        }), new Promise((_, reject) => {
          deadline = setTimeout(() => reject(new Error("stream_input_timeout")), 5000);
        })]).finally(() => clearTimeout(deadline));
        if (this.readyState !== 1) return;
        if (response.status !== 204) throw new Error("stream_input_failed");
        this._queuedBytes -= chunk.byteLength;
        ui.stage.dataset.httpInputPosts = String(Number(ui.stage.dataset.httpInputPosts) + 1);
      }
    } catch {
      if (this.readyState === 1) this._fail();
    } finally {
      this._flushing = false;
      if (this.readyState === 1 && this._queue.length) this._scheduleFlush();
    }
  }

  _bestEffortClose() {
    if (!this._streamId) return;
    const streamId = this._streamId;
    this._streamId = null;
    void fetch("/api/stream-close", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ generation: this.generation, stream_id: streamId }),
      cache: "no-store", credentials: "same-origin", keepalive: true
    }).catch(() => {});
  }

  _fail() {
    if (this.readyState >= 2) return;
    this.onerror?.({ type: "error" });
    this.close(false);
  }

  close(clean = true) {
    if (this.readyState >= 2) return;
    this.readyState = 2;
    this._abort.abort();
    if (this._reader) void this._reader.cancel().catch(() => {});
    this._queue = [];
    this._queuedBytes = 0;
    this.ticket = null;
    this._bestEffortClose();
    this.readyState = 3;
    if (!this._closeNotified) {
      this._closeNotified = true;
      queueMicrotask(() => this.onclose?.({ type: "close", code: clean ? 1000 : 1006, wasClean: clean }));
    }
  }
}

function currentMode() { return ui.mode.value; }
function updateMode() {
  const passwordMode = currentMode() === "password";
  ui.credentialFields.hidden = !passwordMode;
  ui.portField.hidden = !passwordMode;
  ui.hostLabel.textContent = passwordMode ? "Pi address" : "SSH alias";
  ui.modeToggle.textContent = passwordMode ? "Use a saved SSH connection" : "Use username and password";
  ui.username.required = passwordMode;
  ui.port.required = passwordMode;
  if (!passwordMode) ui.password.value = "";
  setMessage(ui.connectionError, "");
}

function stopStream(closeViewer = true, preserveCanvas = false) {
  lifecycle += 1;
  pasteOperation += 1;
  pastePending = false;
  pointerHeld = false;
  pointerPosition = null;
  clearTimeout(handshakeTimer);
  handshakeTimer = 0;
  const old = rfb;
  const channel = streamChannel;
  rfb = null;
  streamChannel = null;
  streamGeneration = null;
  streamPending = false;
  if (old && closeViewer) old.disconnect();
  if (channel && channel.readyState < 2) channel.close();
  if (!preserveCanvas) ui.stage.replaceChildren();
  ui.placeholder.hidden = false;
}

function renderState(next) {
  if (!next || typeof next !== "object") return;
  const previous = state;
  state = next;
  if (!next.connected) {
    if (previous?.connected || rfb || streamPending) stopStream();
    streamFailed = false;
    ui.password.value = "";
    ui.loginView.hidden = false;
    ui.viewerView.hidden = true;
    ui.disconnect.disabled = true;
    if (!ui.host.value && next.host) ui.host.value = next.host;
    return;
  }
  if (previous?.connected && previous.generation !== next.generation) {
    stopStream();
    streamFailed = true;
    setViewerError("The desktop session changed. Disconnect and reconnect to continue.");
  }
  ui.loginView.hidden = true;
  ui.viewerView.hidden = false;
  ui.connectedHost.textContent = next.host || "Pi desktop";
  ui.disconnect.disabled = connectionPending;
  ui.dot.classList.toggle("connected", !!rfb && !streamFailed);
  if (!rfb && !streamFailed) {
    ui.status.textContent = "Connecting…";
    ui.screenMessage.textContent = "Connecting to desktop…";
  }
}

async function startStream() {
  ui.stage.dataset.documentHidden = String(document.hidden);
  if (document.hidden) { ui.stage.dataset.streamPhase = "hidden"; return; }
  if (!token || !state?.connected || connectionPending || streamPending || rfb || streamFailed) return;
  const generation = state.generation;
  const epoch = lifecycle;
  streamPending = true;
  ui.stage.dataset.streamPhase = "ticket";
  ui.stage.dataset.ticketReceived = "false";
  ui.stage.dataset.httpOpen = "false";
  ui.stage.dataset.httpChunks = "0";
  ui.stage.dataset.httpInputPosts = "0";
  ui.stage.dataset.rfbCreated = "false";
  ui.status.textContent = "Connecting…";
  ui.placeholder.hidden = false;
  const fail = (message, phase) => {
    if (epoch !== lifecycle || !state?.connected || state.generation !== generation || streamFailed) return;
    stopStream(true, true);
    streamFailed = true;
    ui.stage.dataset.streamPhase = phase;
    ui.status.textContent = "Unavailable";
    ui.screenMessage.textContent = "Desktop stream unavailable";
    setViewerError(message);
    ui.disconnect.focus();
  };
  handshakeTimer = setTimeout(() => fail("The desktop did not start in time. Disconnect and reconnect to try again.", "timeout"), 12000);
  try {
    const data = await api("/api/stream-ticket", { generation });
    if (epoch !== lifecycle || !state?.connected || state.generation !== generation || connectionPending) return;
    if (document.hidden) {
      clearTimeout(handshakeTimer);
      handshakeTimer = 0;
      ui.stage.dataset.streamPhase = "hidden";
      return;
    }
    if (!data.state?.connected || data.state.generation !== generation || typeof data.ticket !== "string" || !/^[A-Za-z0-9_-]{16,128}$/.test(data.ticket)) {
      throw { error: { message: "The desktop stream could not be opened. Disconnect and reconnect to try again." } };
    }
    ui.stage.dataset.ticketReceived = "true";
    const channel = new HttpRfbChannel(generation, data.ticket);
    streamChannel = channel;
    ui.stage.dataset.streamPhase = "http-opening";
    const viewer = new RFB(ui.stage, channel, { shared: true });
    rfb = viewer;
    ui.stage.dataset.rfbCreated = "true";
    streamGeneration = generation;
    viewer.scaleViewport = true;
    viewer.resizeSession = false;
    viewer.focusOnClick = true;
    viewer.viewOnly = false;
    // Raw frames exclude the pointer; noVNC moves the server's cursor locally.
    // Keep a visible fallback until the Pi supplies a nontransparent shape.
    viewer.showDotCursor = true;
    viewer.compressionLevel = 1;
    viewer.qualityLevel = 6;
    viewer.addEventListener("connect", () => {
      if (rfb !== viewer || streamGeneration !== generation || epoch !== lifecycle) return;
      clearTimeout(handshakeTimer);
      handshakeTimer = 0;
      ui.stage.dataset.streamPhase = "streaming";
      ui.placeholder.hidden = true;
      ui.status.textContent = "Streaming";
      ui.dot.classList.add("connected");
      setViewerError("");
    });
    viewer.addEventListener("disconnect", () => {
      if (rfb !== viewer || streamGeneration !== generation || epoch !== lifecycle) return;
      stopStream(false, true);
      streamFailed = true;
      ui.stage.dataset.streamPhase = "disconnected";
      ui.status.textContent = "Disconnected";
      ui.screenMessage.textContent = "Desktop stream ended";
      ui.dot.classList.remove("connected");
      setViewerError("The desktop stream ended. Disconnect and reconnect to continue.");
      ui.disconnect.focus();
    });
    viewer.addEventListener("securityfailure", () => {
      if (rfb === viewer) setViewerError("The desktop stream could not be verified. Disconnect and reconnect to continue.");
    });
    void channel.start();
  } catch (error) {
    fail(friendlyError(error, "The desktop stream could not be opened. Disconnect and reconnect to try again."), "error");
  } finally {
    if (epoch === lifecycle) streamPending = false;
  }
}

async function fetchState() {
  if (!token || connectionPending || heartbeatPending || document.hidden) return;
  const epoch = lifecycle;
  heartbeatPending = true;
  try {
    const data = await api("/api/state");
    if (epoch !== lifecycle || connectionPending) return;
    renderState(data.state || data);
    if (state?.connected) startStream();
  } catch (error) {
    if (epoch === lifecycle) setViewerError(friendlyError(error, "Could not check the connection."));
  } finally { heartbeatPending = false; }
}

function connectionDetails() {
  const host = ui.host.value.trim();
  const mode = currentMode();
  const port = Number(ui.port.value);
  if (!host) { ui.host.setAttribute("aria-invalid", "true"); ui.host.focus(); setMessage(ui.connectionError, "Enter your Pi address or SSH alias."); return null; }
  ui.host.removeAttribute("aria-invalid");
  if (mode === "password" && !ui.username.value.trim()) { ui.username.setAttribute("aria-invalid", "true"); ui.username.focus(); setMessage(ui.connectionError, "Enter your SSH username."); return null; }
  ui.username.removeAttribute("aria-invalid");
  if (mode === "password" && (!Number.isInteger(port) || port < 1 || port > 65535)) { ui.port.setAttribute("aria-invalid", "true"); ui.port.focus(); setMessage(ui.connectionError, "Enter an SSH port from 1 to 65535."); return null; }
  ui.port.removeAttribute("aria-invalid");
  setMessage(ui.connectionError, "");
  return mode === "existing"
    ? { mode, host, deploy: ui.deploy.checked }
    : { mode, host, username: ui.username.value.trim(), port, password: ui.password.value, deploy: ui.deploy.checked };
}

async function connect(details) {
  stopStream();
  streamFailed = false;
  connectionPending = true;
  ui.connect.disabled = true;
  ui.connect.textContent = "Connecting…";
  setMessage(ui.connectionError, "");
  try {
    const data = await api("/api/connect", details);
    pendingTrust = null;
    ui.password.value = "";
    renderState(data.state);
    setViewerError("");
  } catch (error) {
    if (error.error?.code === "host_key_confirmation_required" && error.host_key) {
      const fingerprint = error.host_key.fingerprint;
      if (typeof fingerprint !== "string" || !/^SHA256:[A-Za-z0-9+/]{43}=?$/.test(fingerprint)) {
        ui.password.value = "";
        setMessage(ui.connectionError, "The Pi fingerprint could not be verified. Connection stopped.");
      } else {
        pendingTrust = { details, fingerprint };
        ui.keyHost.textContent = details.host;
        ui.keyAlgorithm.textContent = String(error.host_key.algorithm || "Unknown");
        ui.keyFingerprint.textContent = fingerprint;
        ui.dialog.showModal();
        ui.cancel.focus();
      }
    } else {
      pendingTrust = null;
      ui.password.value = "";
      setMessage(ui.connectionError, friendlyError(error, "Could not connect. Check the address and SSH details."));
      if (error.state) renderState(error.state);
    }
  } finally {
    connectionPending = false;
    ui.connect.disabled = false;
    ui.connect.textContent = "Connect";
    ui.disconnect.disabled = !state?.connected;
    if (state?.connected) startStream();
  }
}

function cancelTrust() {
  pendingTrust = null;
  ui.password.value = "";
  if (ui.dialog.open) ui.dialog.close();
  ui.connect.focus();
}

async function disconnect() {
  if (!state?.connected || connectionPending) return;
  cancelPendingPaste();
  connectionPending = true;
  if (rfb) {
    releaseHeldRemoteKeys(rfb);
    releaseHeldRemotePointer(rfb);
    rfb.viewOnly = true;
    rfb.blur();
  }
  ui.disconnect.disabled = true;
  ui.status.textContent = "Disconnecting…";
  try {
    const data = await api("/api/disconnect", {});
    renderState(data.state);
    ui.password.value = "";
    setViewerError("");
    ui.host.focus();
  } catch (error) {
    if (error.state) renderState(error.state);
    if (state?.connected) {
      ui.status.textContent = "Unavailable";
      setViewerError(friendlyError(error, "Could not disconnect. Try again."));
    }
  } finally {
    connectionPending = false;
    ui.disconnect.disabled = !state?.connected;
  }
}

ui.modeToggle.addEventListener("click", () => {
  ui.mode.value = currentMode() === "password" ? "existing" : "password";
  updateMode();
  ui.host.focus();
});
ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!connectionPending) { const details = connectionDetails(); if (details) connect(details); }
});
ui.trust.addEventListener("click", () => {
  if (!pendingTrust) return;
  const details = { ...pendingTrust.details, expected_fingerprint: pendingTrust.fingerprint };
  ui.dialog.close();
  pendingTrust = null;
  connect(details);
});
ui.cancel.addEventListener("click", cancelTrust);
ui.dialog.addEventListener("cancel", (event) => { event.preventDefault(); cancelTrust(); });
ui.disconnect.addEventListener("click", disconnect);
ui.stage.addEventListener("focus", () => {
  if (!rfb || streamFailed || connectionPending || pastePending || document.hidden) return;
  rfb.viewOnly = false;
  rfb.focus({ preventScroll: true });
});
ui.stage.addEventListener("pointerdown", () => {
  if (rfb && !streamFailed && !connectionPending && !pastePending && !document.hidden) rfb.viewOnly = false;
}, true);

function streamHasFocus() {
  return !!(rfb && !streamFailed && state?.connected && !connectionPending && !document.hidden
    && ui.stage.contains(document.activeElement));
}

function releaseHeldRemoteKeys(viewer) {
  if (!viewer || viewer.viewOnly) return;
  // Pinned noVNC listens for window blur and sends every held key-up then.
  // Its viewOnly setter suppresses those key-ups, so blur must come first.
  window.dispatchEvent(new Event("blur"));
}

function releaseHeldRemotePointer(viewer) {
  if (!viewer || viewer.viewOnly || !pointerHeld) return;
  const canvas = ui.stage.querySelector("canvas");
  if (!canvas || !pointerPosition) { pointerHeld = false; return; }
  const event = new MouseEvent("mouseup", {
    bubbles: true, cancelable: true, buttons: 0,
    clientX: pointerPosition.x, clientY: pointerPosition.y
  });
  // noVNC's capture proxy forwards window mouseup to its canvas and releases
  // capture. A direct canvas event can leave the capture overlay behind.
  if (document.captureElement === canvas) window.dispatchEvent(event);
  else canvas.dispatchEvent(event);
  pointerHeld = false;
}

function cancelPendingPaste() {
  if (!pastePending) return;
  pasteOperation += 1;
  pastePending = false;
}

ui.stage.addEventListener("mousedown", (event) => {
  if (!rfb || rfb.viewOnly || streamFailed || event.target?.tagName !== "CANVAS") return;
  pointerHeld = !!event.buttons;
  pointerPosition = { x: event.clientX, y: event.clientY };
}, true);
ui.stage.addEventListener("mousemove", (event) => {
  if (pointerHeld) pointerPosition = { x: event.clientX, y: event.clientY };
}, true);
window.addEventListener("mousemove", (event) => {
  if (pointerHeld) pointerPosition = { x: event.clientX, y: event.clientY };
}, true);
window.addEventListener("mouseup", (event) => {
  pointerHeld = !!event.buttons;
  if (pointerHeld) pointerPosition = { x: event.clientX, y: event.clientY };
}, true);
window.addEventListener("blur", () => releaseHeldRemotePointer(rfb));
window.addEventListener("blur", cancelPendingPaste);
document.addEventListener("pointerdown", cancelPendingPaste, true);
document.addEventListener("wheel", cancelPendingPaste, true);

function sendPastedText(text, viewer, generation, epoch) {
  if (viewer !== rfb || generation !== state?.generation || epoch !== lifecycle || !streamHasFocus()) return;
  if (typeof text !== "string" || !text || text.length > 4096) {
    setViewerError("Paste text must be between 1 and 4096 characters.");
    return;
  }
  const normalized = text.replace(/\r\n?/g, "\n");
  if (/[\u0000-\u0008\u000b-\u000c\u000e-\u001f\u007f]/.test(normalized)) {
    setViewerError("That text contains a control character that cannot be pasted here.");
    return;
  }
  const keysyms = [];
  let hasNonAscii = false;
  for (const char of normalized) {
    const codePoint = char.codePointAt(0);
    if (codePoint >= 0xd800 && codePoint <= 0xdfff) {
      setViewerError("That text contains a character that cannot be pasted here.");
      return;
    }
    if (codePoint > 0x7f) hasNonAscii = true;
    keysyms.push(char === "\n" ? 0xff0d : char === "\t" ? 0xff09 : codePoint);
  }
  if (hasNonAscii) {
    setViewerError("Paste supports plain ASCII text in this viewer. Nothing was sent.");
    return;
  }
  try {
    // A physical modifier can still be held when clipboard read completes.
    releaseHeldRemoteKeys(viewer);
    releaseHeldRemotePointer(viewer);
    viewer.viewOnly = true;
    viewer.viewOnly = false;
    for (const keysym of keysyms) viewer.sendKey(keysym, null);
    setViewerError("");
  } catch {
    setViewerError("Paste may have stopped partway through. Check the Pi before trying again.");
  }
}

// noVNC owns ordinary keyboard, pointer, wheel and composition input.
// Browser paste is explicitly converted to RFB key events, without changing
// either clipboard or keeping a second copy of its text.
document.addEventListener("paste", (event) => {
  if (!streamHasFocus()) return;
  event.preventDefault();
  if (pastePending) return;
  sendPastedText(event.clipboardData?.getData("text/plain"), rfb, state.generation, lifecycle);
}, true);

// F6 offers a keyboard route back to the one local control without sending it to the Pi.
document.addEventListener("keydown", (event) => {
  if (pastePending && !((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "v")) {
    cancelPendingPaste();
  }
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "v" && streamHasFocus()) {
    event.preventDefault();
    event.stopPropagation();
    if (pastePending) return;
    const viewer = rfb;
    const generation = state.generation;
    const epoch = lifecycle;
    const operation = ++pasteOperation;
    pastePending = true;
    let read;
    try { read = navigator.clipboard.readText(); }
    catch {
      pastePending = false;
      setViewerError("Clipboard access was blocked. Allow it and try pasting again, or type directly.");
      return;
    }
    Promise.resolve(read).then(
      (text) => { if (operation === pasteOperation) sendPastedText(text, viewer, generation, epoch); },
      () => { if (operation === pasteOperation && viewer === rfb) setViewerError("Clipboard access was blocked. Allow it and try pasting again, or type directly."); }
    ).finally(() => {
      if (operation !== pasteOperation) return;
      pastePending = false;
      if (viewer === rfb && !streamFailed && !connectionPending && !document.hidden && ui.stage.contains(document.activeElement)) viewer.viewOnly = false;
    });
    return;
  }
  if (event.key !== "F6" || ui.viewerView.hidden) return;
  event.preventDefault();
  event.stopPropagation();
  if (rfb) { releaseHeldRemoteKeys(rfb); releaseHeldRemotePointer(rfb); rfb.viewOnly = true; rfb.blur(); }
  ui.disconnect.focus();
}, true);
document.addEventListener("visibilitychange", () => {
  ui.stage.dataset.documentHidden = String(document.hidden);
  if (document.hidden) cancelPendingPaste();
  if (document.hidden && rfb) { releaseHeldRemoteKeys(rfb); releaseHeldRemotePointer(rfb); rfb.viewOnly = true; rfb.blur(); }
  else fetchState();
});
window.addEventListener("pagehide", () => { stopStream(); streamFailed = true; });
setInterval(fetchState, 10000);

if (!token) {
  ui.form.hidden = true;
  ui.modeToggle.hidden = true;
  setViewerError("Reopen this viewer from Pi Desktop Bridge to get a fresh local link.");
} else {
  updateMode();
  fetchState();
}
