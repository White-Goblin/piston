import test from "node:test";
import assert from "node:assert/strict";
import { loadCard } from "./load.js";

const { deriveState, MoveBatcher, resolveEntities, statusFromError, StatusFeed, clampSpeed } = loadCard();

test("each state-table row enables the right controls and shows its banner", () => {
  const rows = {
    ready: [null, true, true, true],
    desktop: ["Desktop mode", true, false, true],
    device_off: ["Device is off", false, false, true],
    unreachable: ["Can't reach the device", false, false, true],
    no_capability: ["Update the Piston plugin", false, false, true],
    disabled: ["Remote input is turned off on the device", false, false, true],
    error: ["The device could not start remote input", false, false, true],
    unauthorized: ["Only administrators can use remote input", false, false, true],
  };
  for (const [status, [banner, input, shell, power]] of Object.entries(rows)) {
    const s = deriveState({ status, mpState: "idle" });
    assert.equal(s.banner, banner, status);
    assert.deepEqual([s.input, s.shell, s.power], [input, shell, power], status);
  }
});

test("skew, unresolved devices and connecting", () => {
  assert.match(deriveState({ status: "skew", mpState: "idle" }).banner, /versions do not match/);
  for (const status of ["no_device", "not_found"]) {
    const s = deriveState({ status, mpState: "idle" });
    assert.equal(s.banner, "Choose a Piston device");
    assert.deepEqual([s.input, s.shell, s.power, s.apps, s.volume], [false, false, false, false, false]);
  }
  const connecting = deriveState({ status: "connecting", mpState: "idle" });
  assert.deepEqual([connecting.banner, connecting.input, connecting.power], [null, false, true]);
});

test("apps and volume follow the media player", () => {
  for (const mp of ["idle", "playing", "paused", "on"]) {
    const s = deriveState({ status: "ready", mpState: mp });
    assert.equal(s.apps && s.volume, true, mp);
  }
  for (const mp of ["off", "standby", "unavailable", "unknown", undefined, null]) {
    const s = deriveState({ status: "device_off", mpState: mp });
    assert.equal(s.apps || s.volume, false, String(mp));
  }
});

test("a preview sends nothing and shows a banner", () => {
  const s = deriveState({ status: "ready", mpState: "idle", preview: true });
  assert.deepEqual([s.kind, s.input, s.shell, s.power, s.apps, s.volume], ["preview", false, false, false, false, false]);
});

test("the power button is off while the media player is unavailable and the status is still connecting", () => {
  assert.equal(deriveState({ status: "connecting", mpState: "unavailable" }).power, false);
});

test("moves are summed and scaled by the speed, one event per take", () => {
  const b = new MoveBatcher(2);
  assert.equal(b.take(), null);
  b.add(1.234, -2);
  b.add(0.5, 1);
  assert.equal(b.pending, true);
  assert.deepEqual(b.take(), { k: "move", dx: 3.47, dy: -2 });
  assert.equal(b.pending, false);
  assert.equal(b.take(), null);
});

test("speed is clamped to a sane range and defaults to 1", () => {
  assert.equal(clampSpeed(undefined), 1);
  assert.equal(clampSpeed("x"), 1);
  assert.equal(clampSpeed(0), 1);
  assert.equal(clampSpeed(-3), 1);
  assert.equal(clampSpeed(100), 4);
  assert.equal(clampSpeed(0.01), 0.25);
  assert.equal(clampSpeed(1.5), 1.5);
});

test("the media player of the device is found by device id and platform", () => {
  const hass = { entities: {
    "media_player.other": { entity_id: "media_player.other", device_id: "d2", platform: "piston" },
    "media_player.tv": { entity_id: "media_player.tv", device_id: "d1", platform: "samsung" },
    "button.box_restart_steam": { entity_id: "button.box_restart_steam", device_id: "d1", platform: "piston" },
    "media_player.box": { entity_id: "media_player.box", device_id: "d1", platform: "piston" },
  } };
  assert.deepEqual(resolveEntities(hass, "d1"), { mediaPlayer: "media_player.box" });
  assert.deepEqual(resolveEntities(hass, "d3"), { mediaPlayer: null });
});

test("entity resolution never throws on missing data", () => {
  for (const hass of [undefined, null, {}, { entities: null }, { entities: { a: null } }]) {
    assert.deepEqual(resolveEntities(hass, "d1"), { mediaPlayer: null });
  }
});

test("subscribe errors map to terminal statuses or a retry", () => {
  assert.equal(statusFromError({ code: "unauthorized" }), "unauthorized");
  assert.equal(statusFromError({ code: "not_found" }), "not_found");
  assert.equal(statusFromError({ code: "unknown_error" }), null);
  assert.equal(statusFromError(undefined), null);
});

function harness(subscribeImpl) {
  const timers = [];
  const log = { statuses: [], subs: 0, unsubs: 0 };
  const feed = new StatusFeed({
    subscribe: (id, cb) => { log.subs++; return subscribeImpl(id, cb, log); },
    onStatus: (s) => log.statuses.push(s),
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimer: (h) => { if (h) timers[h - 1].cancelled = true; },
  });
  return { feed, log, timers, flush: () => new Promise((r) => setImmediate(r)) };
}

test("the feed subscribes once, relays statuses and unsubscribes on stop", async () => {
  const h = harness((id, cb, log) => { cb("ready"); return Promise.resolve(() => { log.unsubs++; }); });
  h.feed.start("d1");
  h.feed.start("d1");                       // already running: nothing new
  await h.flush();
  assert.deepEqual([h.log.subs, h.log.statuses], [1, ["ready"]]);
  h.feed.stop();
  assert.equal(h.log.unsubs, 1);
  h.feed.stop();                            // harmless
  assert.equal(h.log.unsubs, 1);
});

test("an unauthorized or unknown device is final: no retry", async () => {
  const h = harness(() => Promise.reject({ code: "unauthorized" }));
  h.feed.start("d1");
  await h.flush();
  assert.deepEqual([h.log.statuses, h.timers.length], [["unauthorized"], 0]);
});

test("other failures retry with a growing delay capped at 30 seconds", async () => {
  const h = harness(() => Promise.reject(new Error("down")));
  h.feed.start("d1");
  const delays = [];
  for (let i = 0; i < 8; i++) {
    await h.flush();
    const t = h.timers[h.timers.length - 1];
    delays.push(t.ms);
    t.fn();
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
});

test("stopping cancels a pending retry and an unsubscribe that arrives late is called", async () => {
  const h = harness(() => Promise.reject(new Error("down")));
  h.feed.start("d1");
  await h.flush();
  h.feed.stop();
  assert.equal(h.timers[0].cancelled, true);
  let late = 0;
  let resolve;
  const slow = harness(() => new Promise((r) => { resolve = r; }));
  slow.feed.start("d1");
  await slow.flush();                       // the subscribe call is in flight
  slow.feed.stop();
  resolve(() => { late++; });
  await slow.flush();
  assert.equal(late, 1);
  assert.deepEqual(slow.log.statuses, []);
});

test("starting for another device switches the subscription", async () => {
  const h = harness((id, cb, log) => Promise.resolve(() => { log.unsubs++; }));
  h.feed.start("d1");
  await h.flush();
  h.feed.start("d2");
  await h.flush();
  assert.deepEqual([h.log.subs, h.log.unsubs], [2, 1]);
  h.feed.start("");
  assert.equal(h.log.unsubs, 2);
});

test("statuses from a replaced subscription are ignored", async () => {
  let first;
  const h = harness((id, cb) => { if (id === "d1") first = cb; return Promise.resolve(() => {}); });
  h.feed.start("d1");
  await h.flush();
  h.feed.start("d2");
  first("ready");
  assert.deepEqual(h.log.statuses, []);
});
