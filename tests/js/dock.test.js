import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./dom.js";
import { loadCard } from "./load.js";

const { WheelGesture, TextSync, flickFrom } = loadCard();

// ---- the wheel's decisions ------------------------------------------------------------------------------------------

const wheel = (key = "Up") => {
  const w = new WheelGesture();
  w.down({ id: 1, key, center: [100, 100], point: [100, 60], size: 200 });      // pressed on the top wedge
  return w;
};

test("a tap on a wedge sends its arrow on release", () => {
  const w = wheel("Left");
  assert.equal(w.active, true);
  assert.deepEqual(w.up(1, false, 100), [{ type: "key", key: "Left" }]);
  assert.equal(w.active, false);
});

test("a cancelled press sends nothing", () => {
  assert.deepEqual(wheel().up(1, true, 100), []);
});

test("holding repeats: the hold sends the arrow once and the release adds nothing", () => {
  const w = wheel();
  assert.deepEqual(w.hold(), [{ type: "key", key: "Up" }]);
  assert.equal(w.repeating, true);
  assert.deepEqual(w.up(1, false, 500), []);
});

test("a second finger while one is down is ignored", () => {
  const w = wheel();
  w.down({ id: 2, key: "Down", center: [100, 100], point: [100, 140], size: 200 });
  assert.deepEqual(w.up(2, false, 10), []);
  assert.equal(w.active, true);
});

test("moving the thumb around the ring spins: the arrow is dropped, clockwise is a negative dy", () => {
  const w = wheel();
  // clockwise from the top: (100,60) -> (140,100)
  const steps = [];
  for (let deg = 0; deg <= 60; deg += 10) {
    const a = (-90 + deg) * Math.PI / 180;
    steps.push(...w.move(1, [100 + 40 * Math.cos(a), 100 + 40 * Math.sin(a)], deg * 10));
  }
  assert.equal(steps.filter((s) => s.type === "spin_start").length, 1);
  const scrolls = steps.filter((s) => s.type === "scroll");
  assert.ok(scrolls.length >= 3);
  assert.ok(scrolls.every((s) => s.dy < 0));
  assert.deepEqual(w.hold(), []);                                // spinning: no repeat
  assert.equal(w.repeating, false);
  const end = w.up(1, false, 620);
  assert.ok(end.length === 0 || end[0].type === "flick");        // never an arrow after a spin
});

test("counter-clockwise scrolls the other way", () => {
  const w = wheel();
  const out = [];
  for (let deg = 0; deg >= -60; deg -= 10) {
    const a = (-90 + deg) * Math.PI / 180;
    out.push(...w.move(1, [100 + 40 * Math.cos(a), 100 + 40 * Math.sin(a)], -deg * 10));
  }
  assert.ok(out.filter((s) => s.type === "scroll").every((s) => s.dy > 0));
});

test("movement near the hub is ignored (the angle is noise there)", () => {
  const w = wheel();
  assert.deepEqual(w.move(1, [102, 101], 10), []);
});

test("the wheel scrolls linearly: no momentum after the thumb lifts, however fast the spin", () => {
  const fast = wheel();
  let t = 0;
  const seen = [];
  for (let deg = 0; deg <= 160; deg += 20) { t += 16; const a = (-90 + deg) * Math.PI / 180; seen.push(...fast.move(1, [100 + 40 * Math.cos(a), 100 + 40 * Math.sin(a)], t)); }
  assert.ok(seen.some((s) => s.type === "scroll"));
  assert.deepEqual(fast.up(1, false, t + 4), []);
  assert.ok(!seen.some((s) => s.type === "flick"));
});

test("the angle wraps correctly across the +-180 degree line", () => {
  const w = new WheelGesture();
  w.down({ id: 1, key: "Left", center: [100, 100], point: [60, 101], size: 200 });
  const a1 = (176 * Math.PI) / 180, a2 = (-176 * Math.PI) / 180;
  const o1 = w.move(1, [100 + 40 * Math.cos(a1), 100 + 40 * Math.sin(a1)], 10);
  const o2 = w.move(1, [100 + 40 * Math.cos(a2), 100 + 40 * Math.sin(a2)], 20);
  for (const s of [...o1, ...o2]) if (s.type === "scroll") assert.ok(Math.abs(s.dy) < 20, "no jump of 2*pi");
});

test("flickFrom needs two recent samples and enough speed", () => {
  assert.equal(flickFrom([[0, 5, 0]], 10), null);
  assert.equal(flickFrom([[0, 50, 0], [10, 50, 0]], 400), null);          // too old
  assert.deepEqual(flickFrom([[0, 16, 0], [16, 16, 0]], 20), { vy: 1.6, vx: 0 });
});

// ---- typing ---------------------------------------------------------------------------------------------------------

test("typed text becomes one text event; the sentinel space is never sent", () => {
  const t = new TextSync();
  assert.deepEqual(t.diff(" hello", false), { events: [{ k: "text", s: "hello" }], dropped: 0, value: null });
});

test("editing sends backspaces for what was removed and then the new text (autocorrect, swipe typing)", () => {
  const t = new TextSync();
  t.diff(" teh", false);
  assert.deepEqual(t.diff(" the", false).events, [{ k: "key", key: "Backspace" }, { k: "key", key: "Backspace" }, { k: "text", s: "he" }]);
  assert.deepEqual(t.diff(" the ", false).events, [{ k: "text", s: " " }]);
});

test("a backspace on the sentinel sends one backspace", () => {
  const t = new TextSync();
  assert.deepEqual(t.diff("", false), { events: [{ k: "key", key: "Backspace" }], dropped: 0, value: " " });
});

test("characters the device cannot type are dropped, counted, and never backspaced", () => {
  const t = new TextSync();
  const r = t.diff(" aéb€", false);
  assert.deepEqual([r.events, r.dropped], [[{ k: "text", s: "ab" }], 2]);
  // deleting "€" and "b": only "b" was typed on the device
  assert.deepEqual(t.diff(" aé", false).events, [{ k: "key", key: "Backspace" }]);
  assert.deepEqual(t.diff(" a", false).events, []);                       // deleting "é" must not delete the device's "a"
});

test("emoji and other astral characters count as one dropped character", () => {
  const t = new TextSync();
  const r = t.diff(" a😀", false);
  assert.deepEqual([r.events, r.dropped], [[{ k: "text", s: "a" }], 1]);
});

test("long text is split into events of at most 256 characters", () => {
  const t = new TextSync();
  const r = t.diff(" " + "a".repeat(600), false);
  assert.deepEqual(r.events.map((e) => e.s.length), [256, 256, 88]);
});

test("the mirror starts over when it grows long or is emptied, but not while composing", () => {
  const t = new TextSync();
  assert.equal(t.diff(" " + "a".repeat(130), false).value, " ");
  assert.equal(t.diff(" abc", true).value, null);                          // composing: leave the field alone
  assert.equal(t.diff(" " + "a".repeat(130), true).value, null);
});

test("tabs and newlines in pasted text are typable, other control characters are not", () => {
  const t = new TextSync();
  const r = t.diff(" a\tb\nc\u0007", false);
  assert.deepEqual([r.events, r.dropped], [[{ k: "text", s: "a\tb\nc" }], 1]);
});

// ---- the element ------------------------------------------------------------------------------------------------------

function setup(statusAfter = "ready") {
  const dom = installDom();
  loadCard();
  const card = new dom.defined["piston-remote-card"]();
  const sent = [], subscribed = [];
  card.isConnected = true;
  card.connectedCallback();
  card.setConfig({ device_id: "d1" });
  card.hass = {
    connection: { sendMessage: (m) => sent.push(m),
      subscribeMessage: (cb) => { subscribed.push(cb); return Promise.resolve(() => {}); } },
    entities: {}, states: {},
  };
  const events = () => sent.flatMap((m) => m.events);
  return { dom, card, sent, events, subscribed, status: async (s) => { await new Promise((r) => setImmediate(r)); subscribed[0]({ status: s }); } };
}

const ptr = (card, type, x, y, id = 1) => card._el.wheel.fire(type, {
  pointerId: id, clientX: x, clientY: y, target: { closest: () => card._wedges[0] },
});

test("tiles send actions and Back / Enter send keys; a disabled device sends nothing", async () => {
  const t = setup();
  try {
    await t.status("ready");
    const q = (sel) => t.card.shadowRoot.querySelector(sel);
    q('[data-act="steam"]').fire("click");
    q('[data-act="qam"]').fire("click");
    q("#esc").fire("pointerdown");
    q("#hub").fire("pointerdown");
    t.dom.frame();
    assert.deepEqual(t.events(), [{ k: "action", a: "steam" }, { k: "action", a: "qam" }, { k: "key", key: "Escape" }, { k: "key", key: "Enter" }]);
    t.sent.length = 0;
    await t.status("desktop");                           // pointer and keys work, the Steam tiles do not
    q('[data-act="steam"]').fire("click");
    q("#esc").fire("pointerdown");
    t.dom.frame();
    assert.deepEqual(t.events(), [{ k: "key", key: "Escape" }]);
    t.sent.length = 0;
    await t.status("device_off");
    q("#esc").fire("pointerdown");
    q("#hub").fire("pointerdown");
    t.dom.frame();
    assert.deepEqual(t.events(), []);
    assert.equal(q("#esc").classList.contains("off"), true);
    assert.equal(q("#wheel").classList.contains("off"), true);
    assert.equal(q('[data-act="steam"]').classList.contains("off"), true);
  } finally { t.dom.uninstall(); }
});

function wedgePress(t, key = "Up") {
  const wheelEl = t.card._el.wheel;
  const wedge = { dataset: { key }, classList: { add() {}, remove() {} }, closest() { return this; } };
  wheelEl.fire("pointerdown", { pointerId: 1, clientX: 150, clientY: 110, target: wedge });
  return wedge;
}

test("a wheel tap sends the arrow on release", async () => {
  const t = setup();
  try {
    await t.status("ready");
    wedgePress(t, "Left");
    t.card._el.wheel.fire("pointerup", { pointerId: 1, clientX: 150, clientY: 110 });
    t.dom.frame();
    assert.deepEqual(t.events(), [{ k: "key", key: "Left" }]);
  } finally { t.dom.uninstall(); }
});

test("holding the wheel repeats the arrow every 80 ms after 350 ms, and releasing stops it", async () => {
  const t = setup();
  try {
    await t.status("ready");
    wedgePress(t, "Down");
    t.dom.advance(340);
    t.dom.frame();
    assert.deepEqual(t.events(), []);
    t.dom.advance(20);                                   // 360 ms: the hold fires
    t.dom.advance(240);                                  // 3 repeats
    t.dom.frame();
    assert.equal(t.events().length, 4);
    assert.ok(t.events().every((e) => e.key === "Down"));
    t.card._el.wheel.fire("pointerup", { pointerId: 1, clientX: 150, clientY: 110 });
    t.sent.length = 0;
    t.dom.advance(1000);
    t.dom.frame();
    assert.deepEqual(t.events(), []);                    // nothing after the release, not even the arrow
    assert.equal(t.dom.timers.size, 0);
  } finally { t.dom.uninstall(); }
});

test("all repeat timers stop when the page goes to the background", async () => {
  const t = setup();
  try {
    await t.status("ready");
    wedgePress(t, "Right");
    t.dom.advance(500);
    assert.ok(t.dom.timers.size > 0);                    // the repeat interval is running
    t.dom.document.visibilityState = "hidden";
    t.dom.document.fire("visibilitychange");
    t.sent.length = 0;
    t.dom.advance(2000);
    t.dom.frame();
    assert.deepEqual(t.events(), []);
    assert.equal(t.dom.timers.size, 0);
  } finally { t.dom.uninstall(); }
});

test("repeat timers also stop when the card is removed and when the device goes away", async () => {
  const t = setup();
  try {
    await t.status("ready");
    wedgePress(t, "Right");
    t.dom.advance(500);
    t.card.disconnectedCallback();
    t.sent.length = 0;
    t.dom.advance(2000);
    t.dom.frame();
    assert.deepEqual(t.events(), []);
  } finally { t.dom.uninstall(); }
  const t2 = setup();
  try {
    await t2.status("ready");
    wedgePress(t2, "Right");
    t2.dom.advance(500);
    t2.dom.frame();
    t2.subscribed[0]({ status: "device_off" });
    t2.sent.length = 0;
    t2.dom.advance(2000);
    t2.dom.frame();
    assert.deepEqual(t2.events(), []);
  } finally { t2.dom.uninstall(); }
});

test("typing: text, Enter and Backspace go out as events and a notice shows for characters that cannot be typed", async () => {
  const t = setup();
  try {
    await t.status("ready");
    const kb = t.card._el.kb;
    kb.fire("focus");
    assert.equal(t.card._el.kbBtn.classList.contains("on"), true);
    kb.value = " hi é";
    kb.fire("input", { inputType: "insertText" });
    const enter = kb.fire("keydown", { key: "Enter" });
    kb.fire("beforeinput", { inputType: "insertLineBreak" });
    kb.value = " hi";
    kb.fire("input", { inputType: "deleteContentBackward" });
    t.dom.frame();
    assert.equal(enter.prevented, true);
    assert.deepEqual(t.events(), [{ k: "text", s: "hi " }, { k: "key", key: "Enter" }, { k: "key", key: "Enter" }, { k: "key", key: "Backspace" }]);   // the typed space before the dropped é goes too
    assert.match(t.card._el.toast.textContent, /can't be typed/);
    assert.equal(t.card._el.toast.classList.contains("show"), true);
    t.dom.advance(3000);
    assert.equal(t.card._el.toast.classList.contains("show"), false);
    kb.fire("blur");
    assert.equal(t.card._el.kbBtn.classList.contains("on"), false);
  } finally { t.dom.uninstall(); }
});

test("typing is ignored while the device cannot take input", async () => {
  const t = setup();
  try {
    await t.status("device_off");
    t.card._el.kb.value = " hi";
    t.card._el.kb.fire("input", { inputType: "insertText" });
    t.dom.frame();
    assert.deepEqual(t.events(), []);
  } finally { t.dom.uninstall(); }
});

test("the keyboard button focuses the hidden field and a second press closes it", async () => {
  const t = setup();
  try {
    await t.status("ready");
    t.card._el.kbBtn.fire("click");
    assert.equal(t.card._el.kb.focused, true);
    t.card._el.kb.fire("focus");
    t.card._el.kbBtn.fire("click");
    assert.equal(t.card._el.kb.focused, false);
  } finally { t.dom.uninstall(); }
});

test("the bottom tiles use Home Assistant's own icons (the Steam logo glyph, tune for Quick Access, a keyboard) and Back sends Escape", () => {
  const t = setup();
  try {
    const html = t.card.shadowRoot.innerHTML;
    assert.match(html, /<ha-icon icon="mdi:steam"><\/ha-icon>Steam/);
    assert.match(html, /data-act="qam"[^>]*><ha-icon icon="mdi:tune-variant"><\/ha-icon>Quick/);
    assert.match(html, /id="kbBtn"[^>]*><ha-icon icon="mdi:keyboard-outline">/);
    assert.match(html, /id="esc" aria-label="Back"><ha-icon icon="mdi:arrow-left"><\/ha-icon>Back/);
    assert.doesNotMatch(html, />Esc</);
  } finally { t.dom.uninstall(); }
});
