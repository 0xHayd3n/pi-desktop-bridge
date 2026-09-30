"use strict";

// Exercise the shipped UI lifecycle against fake browser and bridge boundaries.
// This check never contacts a Pi and never carries credentials.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor() {
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  dispatch(name, values = {}) {
    const listener = this.listeners.get(name);
    assert.ok(listener, `missing ${name} listener`);
    return listener({ preventDefault() {}, stopPropagation() {}, ...values });
  }
  dispatchEvent(event) { return this.dispatch(event.type, event); }
  replaceChildren() { this.children = []; }
  querySelector(selector) { return selector === "canvas" ? this.children[0] || null : null; }
  contains(node) { return this.children.includes(node); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { focused = this; document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
}
let focused;
const document = {
  hidden: false, activeElement: null, listeners: new Map(), getElementById: element,
  addEventListener(name, listener) { this.listeners.set(name, listener); },
  dispatch(name, values = {}) { this.listeners.get(name)({ preventDefault() {}, stopPropagation() {}, ...values }); }
};
const window = {
  listeners: new Map(),
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(listener);
  },
  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    if (event.type === "mouseup" && document.captureElement) document.captureElement.dispatch("mouseup", event);
  }
};
class Event { constructor(type) { this.type = type; } }
class MouseEvent extends Event {
  constructor(type, values) { super(type); Object.assign(this, values); }
}
const viewers = [];
class FakeRFB {
  constructor(stage, channel, options) {
    this.listeners = new Map();
    this.stage = stage;
    this.channel = channel;
    this.options = options;
    this.closed = false;
    this.blurred = false;
    this.keys = [];
    this.held = new Map();
    this.mouseMasks = [];
    this._viewOnly = false;
    this.canvas = new Element();
    this.canvas.tagName = "CANVAS";
    this.canvas.addEventListener("mouseup", (event) => { if (!this.viewOnly) this.mouseMasks.push(event.buttons || 0); document.captureElement = null; });
    stage.children.push(this.canvas);
    viewers.push(this);
    FakeRFB.messages.clientEncodings(this, [16, -312, -313, -307, 0]);
    channel.onopen = () => {};
    channel.onclose = () => this.dispatch("disconnect");
    channel.onerror = () => {};
    channel.onmessage = (event) => { this.lastMessage = event.data; };
    window.addEventListener("blur", () => {
      if (this.closed) return;
      for (const [code, keysym] of this.held) this.sendKey(keysym, code, false);
    });
  }
  get viewOnly() { return this._viewOnly; }
  set viewOnly(value) {
    this._viewOnly = !!value;
    if (value) this.held.clear(); // Mirrors pinned noVNC's suppressed key-ups on viewOnly=true.
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  dispatch(name) { if (name === "disconnect") this.closed = true; this.listeners.get(name)?.(); }
  disconnect() { this.closed = true; this.dispatch("disconnect"); }
  blur() { this.blurred = true; }
  focus() { this.canvas.focus(); }
  sendKey(keysym, code, down) {
    if (this.viewOnly) return;
    this.keys.push([keysym, code, down]);
    if (down === true) this.held.set(code, keysym);
    if (down === false) this.held.delete(code);
  }
}
FakeRFB.messages = {
  clientEncodings(sock, offered) {
    const wire = new Uint8Array(4 + offered.length * 4);
    const view = new DataView(wire.buffer);
    wire[0] = 2;
    view.setUint16(2, offered.length);
    offered.forEach((value, index) => view.setInt32(4 + index * 4, value));
    sock.encodingWire = wire;
  }
};

const requests = [];
let serverState = { connected: false, host: "pi-desktop", generation: 0 };
let pendingTicket;
let pendingOpen;
let pendingInput;
let pendingDisconnect;
let streamRead;
let clipboardRead = () => Promise.resolve("Ω\n");
const timers = new Map();
let nextTimer = 1;
function fireTimers(delay) {
  for (const [id, timer] of [...timers]) {
    if (timer.delay === delay) { timers.delete(id); timer.callback(); }
  }
}
const fetch = async (url, options = {}) => {
  requests.push({ url, options });
  if (url === "/api/state") return { ok: true, json: async () => serverState };
  if (url === "/api/connect") {
    serverState = { connected: true, host: "pi-desktop", generation: serverState.generation + 1 };
    return { ok: true, json: async () => ({ state: serverState }) };
  }
  if (url === "/api/stream-ticket") {
    if (pendingTicket) return pendingTicket;
    return { ok: true, json: async () => ({ state: serverState, ticket: "Valid_ticket_value_123456789" }) };
  }
  if (url === "/api/stream-open") {
    if (pendingOpen) return pendingOpen;
    return { ok: true, headers: { get(name) { return name.toLowerCase() === "content-type" ? "application/octet-stream" : "A".repeat(43); } },
      body: { getReader() { return { read() { return new Promise((resolve) => { streamRead = resolve; }); }, cancel: async () => {} }; }, cancel: async () => {} } };
  }
  if (url === "/api/stream-input") {
    if (pendingInput) return pendingInput;
    return { status: 204 };
  }
  if (url === "/api/stream-close") return { status: 204 };
  if (url === "/api/disconnect") {
    if (pendingDisconnect) return pendingDisconnect;
    serverState = { connected: false, host: "pi-desktop", generation: serverState.generation + 1 };
    return { ok: true, json: async () => ({ state: serverState }) };
  }
  throw new Error(`Unexpected request: ${url}`);
};

const sandbox = vm.createContext({
  RFB: FakeRFB, encodings: { pseudoEncodingFence: -312, pseudoEncodingContinuousUpdates: -313 },
  initLogging(level) { assert.equal(level, "none"); },
  document, window, Event, MouseEvent, URL, URLSearchParams, AbortController, queueMicrotask,
  navigator: { clipboard: { readText() { return clipboardRead(); } } },
  location: { hash: "#token=browser-capability", pathname: "/", search: "", href: "http://127.0.0.1:50897/", protocol: "http:" },
  history: { replaceState() {} }, fetch, setInterval() { return 0; },
  setTimeout(callback, delay) { const id = nextTimer++; timers.set(id, { callback, delay }); return id; },
  clearTimeout(id) { timers.delete(id); }
});
const appPath = path.join(__dirname, "..", "src", "pi_desktop_bridge", "web", "app.js");
const htmlPath = path.join(__dirname, "..", "src", "pi_desktop_bridge", "web", "index.html");
const source = fs.readFileSync(appPath, "utf8").replace(/^import .*;\r?\n/gm, "");
vm.runInContext(source + "\nglobalThis.testUI = { connect, disconnect, fetchState, startStream, HttpRfbChannel };", sandbox, { filename: appPath });
const ui = sandbox.testUI;
async function settle() { for (let i = 0; i < 12; i += 1) await Promise.resolve(); }
function count(url) { return requests.filter((request) => request.url === url).length; }

(async () => {
  await settle();
  assert.equal(element("host").value, "pi-desktop", "default host should populate the login");
  assert.equal(element("viewer-view").hidden, true);
  assert.equal(/Type text|text-panel|desktop-image|pause-button|refresh-button/.test(fs.readFileSync(htmlPath, "utf8")), false,
    "stream view must have no text popup or polling controls");

  let resolveTicket;
  pendingTicket = new Promise((resolve) => { resolveTicket = resolve; });
  await ui.connect({ mode: "existing", host: "pi-desktop", deploy: false });
  assert.equal(count("/api/stream-ticket"), 1);
  assert.equal(element("viewer-view").hidden, false);
  await ui.disconnect();
  resolveTicket({ ok: true, json: async () => ({ state: { connected: true, generation: 1 }, ticket: "Stale_ticket_value_123456789" }) });
  await settle();
  assert.equal(viewers.length, 0, "ticket delivered after disconnect must not open a stream");
  assert.equal(element("viewer-view").hidden, true);

  pendingTicket = null;
  await ui.connect({ mode: "existing", host: "pi-desktop", deploy: false });
  await settle();
  assert.equal(viewers.length, 1, "one viewer should mount after a valid ticket");
  const viewer = viewers[0];
  const channel = viewer.channel;
  const encodingWire = new DataView(viewer.encodingWire.buffer);
  assert.equal(viewer.encodingWire[0], 2, "the RFB SetEncodings message should be sent");
  assert.equal(encodingWire.getUint16(2), 4);
  assert.deepEqual(Array.from({ length: 4 }, (_, index) => encodingWire.getInt32(4 + index * 4)),
    [16, -313, -307, 0], "only Fence must be removed; ContinuousUpdates and encoding order remain");
  assert.equal(viewer.options.shared, true);
  assert.equal(channel.protocol, "binary");
  assert.equal(channel.readyState, 1, "HTTP RFB channel should open before reading bytes");
  assert.equal(count("/api/stream-open"), 1);
  assert.equal(requests.find((request) => request.url === "/api/stream-open").options.body.includes("Valid_ticket_value_123456789"), true);
  assert.equal(element("screen-stage").dataset.streamPhase, "rfb-handshake");
  assert.equal(element("screen-stage").dataset.rfbCreated, "true");
  assert.equal(element("screen-stage").dataset.httpOpen, "true");
  streamRead({ done: false, value: new Uint8Array([82, 70, 66]) });
  await settle();
  assert.deepEqual(Array.from(new Uint8Array(viewer.lastMessage)), [82, 70, 66]);
  assert.equal(viewer.scaleViewport, true);
  assert.equal(viewer.focusOnClick, true);
  assert.equal(viewer.viewOnly, false, "noVNC should deliver direct keyboard and pointer input");
  assert.equal(count("/api/frame"), 0);
  assert.equal(count("/api/action"), 0);
  viewer.dispatch("connect");
  assert.equal(element("connection-status").textContent, "Streaming");
  assert.equal(element("screen-stage").dataset.streamPhase, "streaming");
  document.hidden = false;
  element("screen-stage").dispatch("focus");
  assert.equal(document.activeElement, viewer.canvas, "keyboard focus on stream stage should enter the canvas");
  viewer.canvas.focus();
  document.dispatch("paste", { clipboardData: { getData() { return "AΩ\n"; } } });
  assert.equal(viewer.keys.length, 0, "non-ASCII browser paste must reject the whole text before sending input");
  assert.equal(element("viewer-error-connected").textContent, "Paste supports plain ASCII text in this viewer. Nothing was sent.");
  document.dispatch("paste", { clipboardData: { getData() { return "A\tB\n"; } } });
  assert.deepEqual(viewer.keys.map(([keysym]) => keysym), [65, 0xff09, 66, 0xff0d],
    "focused ASCII paste should include tabs and newlines");
  viewer.keys = [];
  viewer.sendKey(0xffe3, "ControlLeft", true);
  clipboardRead = () => Promise.resolve("café ✓");
  document.dispatch("keydown", { key: "v", ctrlKey: true });
  await settle();
  assert.deepEqual(viewer.keys.map(([keysym, , down]) => [keysym, down]),
    [[0xffe3, true]], "Unicode Ctrl+V must not send text or release a modifier before validation");
  assert.equal(element("viewer-error-connected").textContent, "Paste supports plain ASCII text in this viewer. Nothing was sent.");
  viewer.sendKey(0xffe3, "ControlLeft", false);
  viewer.keys = [];
  viewer.sendKey(0xffe3, "ControlLeft", true);
  clipboardRead = () => Promise.resolve("Hi\n");
  document.dispatch("keydown", { key: "v", ctrlKey: true });
  await settle();
  assert.deepEqual(viewer.keys.map(([keysym, , down]) => [keysym, down]),
    [[0xffe3, true], [0xffe3, false], [72, undefined], [105, undefined], [0xff0d, undefined]],
    "Ctrl must be released on the wire before valid ASCII paste keys");
  viewer.sendKey(0xffe1, "ShiftLeft", true);
  document.captureElement = viewer.canvas;
  element("screen-stage").dispatch("mousedown", { target: viewer.canvas, buttons: 1, clientX: 80, clientY: 55 });
  viewer.mouseMasks.push(1);
  document.dispatch("keydown", { key: "F6" });
  assert.equal(viewer.blurred, true, "F6 should release remote keyboard focus");
  assert.deepEqual(viewer.keys.at(-1), [0xffe1, "ShiftLeft", false], "F6 must release held keys before view-only mode");
  assert.deepEqual(viewer.mouseMasks.slice(-2), [1, 0], "F6 must release held pointer button on the wire");
  assert.equal(focused, element("disconnect-button"), "F6 should focus the local control");
  element("screen-stage").dispatch("focus");
  viewer.sendKey(0xffe9, "AltLeft", true);
  document.captureElement = viewer.canvas;
  element("screen-stage").dispatch("mousedown", { target: viewer.canvas, buttons: 1, clientX: 100, clientY: 70 });
  viewer.mouseMasks.push(1);
  viewer.blurred = false;
  document.hidden = true;
  document.dispatch("visibilitychange");
  assert.equal(viewer.blurred, true, "hidden tab should release remote keyboard focus");
  assert.deepEqual(viewer.keys.at(-1), [0xffe9, "AltLeft", false], "hidden tab must release held keys on the wire");
  assert.deepEqual(viewer.mouseMasks.slice(-2), [1, 0], "hidden tab must release held pointer button on the wire");
  document.hidden = false;
  element("screen-stage").dispatch("focus");
  document.captureElement = viewer.canvas;
  element("screen-stage").dispatch("mousedown", { target: viewer.canvas, buttons: 3, clientX: 110, clientY: 75 });
  viewer.mouseMasks.push(3);
  window.dispatchEvent(new MouseEvent("mouseup", { buttons: 1, clientX: 110, clientY: 75 }));
  document.hidden = true;
  document.dispatch("visibilitychange");
  assert.deepEqual(viewer.mouseMasks.slice(-2), [1, 0], "remaining button must be released after a partial mouseup");
  const beforeHidden = count("/api/state");
  await ui.fetchState();
  assert.equal(count("/api/state"), beforeHidden, "hidden tab should not heartbeat");
  document.hidden = false;
  element("screen-stage").dispatch("focus");
  document.captureElement = viewer.canvas;
  element("screen-stage").dispatch("mousedown", { target: viewer.canvas, buttons: 1, clientX: 120, clientY: 80 });
  viewer.mouseMasks.push(1);
  window.dispatchEvent(new Event("blur"));
  assert.deepEqual(viewer.mouseMasks.slice(-2), [1, 0], "visible window blur must release a held pointer");

  element("screen-stage").dispatch("pointerdown");
  viewer.canvas.focus();
  let resolveClipboard;
  clipboardRead = () => new Promise((resolve) => { resolveClipboard = resolve; });
  document.dispatch("keydown", { key: "v", ctrlKey: true });
  const beforeStalePaste = viewer.keys.length;
  document.dispatch("keydown", { key: "F6" });
  let resolveDisconnect;
  pendingDisconnect = new Promise((resolve) => { resolveDisconnect = resolve; });
  const disconnectDone = ui.disconnect();
  await settle();
  assert.equal(viewer.channel.readyState, 1, "disconnect must retain the stream until the server releases its lease");
  element("screen-stage").dispatch("focus");
  element("screen-stage").dispatch("pointerdown");
  assert.equal(viewer.viewOnly, true, "input must stay suspended during disconnect");
  serverState = { connected: false, host: "pi-desktop", generation: serverState.generation + 1 };
  pendingDisconnect = null;
  resolveDisconnect({ ok: true, json: async () => ({ state: serverState }) });
  await disconnectDone;
  assert.equal(viewer.channel.readyState, 3, "completed disconnect should close the local stream");
  clipboardRead = () => Promise.resolve("B");
  await ui.connect({ mode: "existing", host: "pi-desktop", deploy: false });
  await settle();
  const secondViewer = viewers[1];
  secondViewer.dispatch("connect");
  secondViewer.canvas.focus();
  document.dispatch("keydown", { key: "v", ctrlKey: true });
  await settle();
  assert.deepEqual(secondViewer.keys.map(([keysym]) => keysym), [66],
    "new session should paste while an obsolete clipboard read remains pending");
  const newErrorBeforeStaleRead = element("viewer-error-connected").textContent;
  resolveClipboard("Ω");
  await settle();
  assert.equal(viewer.keys.length, beforeStalePaste, "clipboard read after disconnect must not send text");
  assert.equal(secondViewer.keys.length, 1, "old clipboard text must not leak into new session");
  assert.equal(element("viewer-error-connected").textContent, newErrorBeforeStaleRead,
    "obsolete Unicode paste must not write an error into a new session");

  let resolveInterruptedPaste;
  clipboardRead = () => new Promise((resolve) => { resolveInterruptedPaste = resolve; });
  document.dispatch("keydown", { key: "v", ctrlKey: true });
  document.dispatch("pointerdown", { target: secondViewer.canvas });
  resolveInterruptedPaste("C");
  await settle();
  assert.equal(secondViewer.keys.length, 1, "pointer input during clipboard read must cancel old paste");

  secondViewer.dispatch("disconnect");
  assert.equal(secondViewer.closed, true);
  assert.equal(element("connection-status").textContent, "Disconnected");
  const beforeRetry = count("/api/stream-ticket");
  await ui.fetchState();
  await settle();
  assert.equal(count("/api/stream-ticket"), beforeRetry, "lost stream must not automatically reconnect");
  await ui.disconnect();
  assert.equal(element("viewer-view").hidden, true);
  await ui.connect({ mode: "existing", host: "pi-desktop", deploy: false });
  await settle();
  const pendingViewer = viewers[2];
  const pendingChannel = pendingViewer.channel;
  fireTimers(12000);
  assert.equal(element("connection-status").textContent, "Unavailable", "stalled handshake must end visibly");
  assert.equal(element("screen-stage").dataset.streamPhase, "timeout");
  assert.equal(pendingChannel.readyState, 3, "stalled channel must be closed");
  assert.equal(pendingViewer.closed, true);
  assert.equal(element("screen-stage").children.length, 1, "diagnostic failure should preserve the startup canvas");
  const beforeTimeoutRetry = count("/api/stream-ticket");
  await ui.fetchState();
  assert.equal(count("/api/stream-ticket"), beforeTimeoutRetry, "timeout must not reconnect automatically");
  await ui.disconnect();
  assert.equal(requests.every(({ url }) => url !== "/api/frame" && url !== "/api/action"), true);
  await ui.connect({ mode: "existing", host: "pi-desktop", deploy: false });
  await settle();
  const disconnectViewer = viewers[3];
  disconnectViewer.dispatch("connect");
  disconnectViewer.canvas.focus();
  let resolveDuringDisconnect;
  clipboardRead = () => new Promise((resolve) => { resolveDuringDisconnect = resolve; });
  document.dispatch("keydown", { key: "v", ctrlKey: true });
  let finishDisconnect;
  pendingDisconnect = new Promise((resolve) => { finishDisconnect = resolve; });
  const delayedDisconnect = ui.disconnect();
  await settle();
  disconnectViewer.canvas.focus();
  resolveDuringDisconnect("D");
  await settle();
  assert.equal(disconnectViewer.viewOnly, true, "finished clipboard read must not re-enable input during disconnect");
  assert.equal(disconnectViewer.keys.length, 0, "clipboard text must not send while disconnect is pending");
  serverState = { connected: false, host: "pi-desktop", generation: serverState.generation + 1 };
  pendingDisconnect = null;
  finishDisconnect({ ok: true, json: async () => ({ state: serverState }) });
  await delayedDisconnect;
  // The input request owns a copied buffer and serializes subsequent sends.
  const directChannel = new ui.HttpRfbChannel(7, "Ticket_123456789012345678901234");
  directChannel.onopen = () => {};
  directChannel.onmessage = () => {};
  directChannel.onclose = () => {};
  void directChannel.start();
  await settle();
  let resolveInput;
  pendingInput = new Promise((resolve) => { resolveInput = resolve; });
  const original = new Uint8Array([1, 2, 3]);
  directChannel.send(original);
  original.fill(9);
  await settle();
  const inputRequests = () => requests.filter((request) => request.url === "/api/stream-input");
  assert.deepEqual(Array.from(inputRequests().at(-1).options.body), [1, 2, 3], "send must copy noVNC's reused buffer");
  const beforeQueued = inputRequests().length;
  directChannel.send(new Uint8Array([4]));
  await settle();
  assert.equal(inputRequests().length, beforeQueued, "later bytes must wait for the first input acknowledgement");
  pendingInput = null;
  resolveInput({ status: 204 });
  await settle();
  assert.equal(inputRequests().length, beforeQueued + 1);
  assert.deepEqual(Array.from(inputRequests().at(-1).options.body), [4], "ordered input should flush after acknowledgement");
  directChannel.close();
  assert.equal(directChannel.readyState, 3);
  assert.equal(count("/api/stream-close") > 0, true);

  const stuckChannel = new ui.HttpRfbChannel(8, "Ticket_123456789012345678901234");
  stuckChannel.onopen = () => {};
  stuckChannel.onmessage = () => {};
  stuckChannel.onclose = () => {};
  void stuckChannel.start();
  await settle();
  let resolveStuck;
  pendingInput = new Promise((resolve) => { resolveStuck = resolve; });
  stuckChannel.send(new Uint8Array([10]));
  await settle();
  stuckChannel.send(new Uint8Array([11]));
  const beforeDeadline = inputRequests().length;
  fireTimers(5000);
  await settle();
  assert.equal(stuckChannel.readyState, 3, "stalled input acknowledgement must close the stream");
  pendingInput = null;
  resolveStuck({ status: 204 });
  await settle();
  assert.equal(inputRequests().length, beforeDeadline, "late acknowledgement must never flush queued input");
  console.log("UI stream checks passed (ticket race, HTTP channel, key and pointer release, paste, timeout, explicit reconnect).");
})().catch((error) => { console.error(error); process.exitCode = 1; });
