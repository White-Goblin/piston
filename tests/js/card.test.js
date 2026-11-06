import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./dom.js";
import { loadCard } from "./load.js";

function setup(config = { device_id: "d1" }, hassExtra = {}) {
  const dom = installDom();
  loadCard();
  const Card = dom.defined["piston-remote-card"];
  const card = new Card();
  const sent = [], subscribed = [];
  const hass = {
    connection: {
      sendMessage: (m) => sent.push(m),
      subscribeMessage: (cb, msg, opts) => { subscribed.push({ cb, msg, opts, unsub: 0 }); const rec = subscribed[subscribed.length - 1]; return Promise.resolve(() => { rec.unsub++; }); },
    },
    entities: { "media_player.box": { entity_id: "media_player.box", device_id: "d1", platform: "piston" } },
    states: { "media_player.box": { state: "idle", attributes: {} } },
    ...hassExtra,
  };
  card.isConnected = true;
  card.connectedCallback();
  card.setConfig(config);
  card.hass = hass;
  const tick = () => new Promise((r) => setImmediate(r));
  const touch = (type, pts, changed) => card._el.pad.fire(type, {
    touches: pts.map(([clientX, clientY]) => ({ clientX, clientY })),
    changedTouches: (changed || pts).map(([clientX, clientY]) => ({ clientX, clientY })),
  });
  return { dom, card, hass, sent, subscribed, tick, touch };
}

test("the card registers itself once in the picker and as an element", () => {
  const dom = installDom();
  try {
    loadCard(); loadCard();
    assert.ok(dom.defined["piston-remote-card"]);
    assert.equal(dom.window.customCards.filter((c) => c.type === "piston-remote-card").length, 1);
  } finally { dom.uninstall(); }
});

test("connecting subscribes to the status stream of the configured device with resubscribe", async () => {
  const t = setup();
  try {
    await t.tick();
    assert.equal(t.subscribed.length, 1);
    assert.deepEqual(t.subscribed[0].msg, { type: "piston/input/subscribe", device_id: "d1" });
    assert.equal(t.subscribed[0].opts.resubscribe, true);
    assert.equal(t.card.state.kind, "connecting");
    t.subscribed[0].cb({ status: "ready" });
    assert.equal(t.card.state.kind, "ready");
  } finally { t.dom.uninstall(); }
});

test("leaving the page unsubscribes, which lets the integration release the device", async () => {
  const t = setup();
  try {
    await t.tick();
    t.card.disconnectedCallback();
    assert.equal(t.subscribed[0].unsub, 1);
    assert.equal(t.dom.document.listenerCount("visibilitychange"), 0);
  } finally { t.dom.uninstall(); }
});

test("without a device the card asks for one and subscribes to nothing", async () => {
  const t = setup({});
  try {
    await t.tick();
    assert.equal(t.subscribed.length, 0);
    assert.equal(t.card.state.banner, "Choose a Piston device");
    assert.equal(t.card._el.badge.classList.contains("show"), true);
    assert.equal(t.card._el.pad.classList.contains("disabled"), true);
  } finally { t.dom.uninstall(); }
});

test("a tap on the pad sends a click in the next frame", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.touch("touchstart", [[100, 100]]);
    t.touch("touchend", [], [[100, 100]]);
    assert.deepEqual(t.sent, [{ type: "piston/input/send", device_id: "d1", events: [{ k: "click", b: "l" }] }]);   // clicks go out at once
  } finally { t.dom.uninstall(); }
});

test("dragging sends one summed move per frame, scaled by the speed", async () => {
  const t = setup({ device_id: "d1", speed: 2 });
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.touch("touchstart", [[100, 100]]);
    t.touch("touchmove", [[110, 100]]);
    t.touch("touchmove", [[120, 105]]);
    t.dom.frame();
    assert.deepEqual(t.sent[0].events, [{ k: "move", dx: 40, dy: 10 }]);
  } finally { t.dom.uninstall(); }
});

test("scroll notches go out at once, not on the next frame, so a slow frame cannot choke them", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.touch("touchstart", [[10, 100], [50, 100]]);
    for (let i = 1; i <= 5; i++) t.touch("touchmove", [[10, 100 + 14 * i], [50, 100 + 14 * i]]);
    assert.equal(t.sent.length, 5);                    // one message per touch move, before any animation frame ran
    assert.ok(t.sent.every((m) => m.events.length === 1 && m.events[0].k === "scroll"));
  } finally { t.dom.uninstall(); }
});

test("a pending move goes out before the click that follows it", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.touch("touchstart", [[100, 100]]);
    t.touch("touchmove", [[130, 100]]);
    t.card._queue({ k: "click", b: "l" });
    assert.deepEqual(t.sent[0].events, [{ k: "move", dx: 30, dy: 0 }, { k: "click", b: "l" }]);
  } finally { t.dom.uninstall(); }
});

test("two fingers scroll in notches", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.touch("touchstart", [[10, 100], [50, 100]]);
    t.touch("touchmove", [[10, 130], [50, 130]]);
    t.dom.frame();
    assert.deepEqual(t.sent[0].events, [{ k: "scroll", dx: 0, dy: 2 }]);
  } finally { t.dom.uninstall(); }
});

test("a disabled pad sends nothing and still swallows the touch", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "device_off" });
    const e = t.touch("touchstart", [[100, 100]]);
    t.touch("touchmove", [[150, 100]]);
    t.dom.frame();
    assert.equal(e.prevented, true);
    assert.deepEqual(t.sent, []);
  } finally { t.dom.uninstall(); }
});

test("in a card preview nothing is sent or subscribed", async () => {
  const dom = installDom();
  try {
    loadCard();
    const card = new dom.defined["piston-remote-card"]();
    card.preview = true; card.isConnected = true;
    const sent = [];
    card.setConfig({ device_id: "d1" });
    card.hass = { connection: { sendMessage: (m) => sent.push(m), subscribeMessage: () => { throw new Error("no"); } }, entities: {}, states: {} };
    card.send([{ k: "release" }]);
    assert.deepEqual(sent, []);
    assert.equal(card.state.kind, "preview");
  } finally { dom.uninstall(); }
});

test("going to the background stops everything in flight", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.touch("touchstart", [[100, 100]]);
    t.touch("touchmove", [[140, 100]]);
    t.dom.document.visibilityState = "hidden";
    t.dom.document.fire("visibilitychange");
    t.dom.frame();
    assert.deepEqual(t.sent, []);                      // the pending move was dropped, not sent late
    assert.equal(t.card._gesture.active, false);
  } finally { t.dom.uninstall(); }
});

test("a flick keeps scrolling and a new touch stops it", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.touch("touchstart", [[10, 100], [50, 100]]);
    for (let i = 1; i <= 6; i++) t.touch("touchmove", [[10, 100 + 12 * i], [50, 100 + 12 * i]]);
    t.touch("touchend", [], [[10, 172]]);
    assert.ok(t.card._inertia, "a flick starts inertia");
    t.touch("touchstart", [[100, 100]]);
    assert.equal(t.card._inertia, null);
  } finally { t.dom.uninstall(); }
});

test("more than 64 queued events are split into several messages", async () => {
  const t = setup();
  try {
    await t.tick();
    t.subscribed[0].cb({ status: "ready" });
    t.card._outbox.events.push(...Array.from({ length: 70 }, () => ({ k: "key", key: "Enter" })));
    t.card._flush();
    assert.deepEqual(t.sent.map((m) => m.events.length), [64, 6]);
  } finally { t.dom.uninstall(); }
});
