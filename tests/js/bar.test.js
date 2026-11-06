import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./dom.js";
import { loadCard } from "./load.js";

const { powerPlan, appsView, volumeView, VolumeSender } = loadCard();

// ---- pure ---------------------------------------------------------------------------------------------------------

test("power plan: wake when off or asleep, confirm before turning off an awake device, nothing when unknown", () => {
  assert.deepEqual(powerPlan("off"), { action: "wake", confirm: false, label: "Turn on" });
  assert.deepEqual(powerPlan("standby"), { action: "wake", confirm: false, label: "Turn on" });
  for (const state of ["idle", "playing", "paused", "on"]) assert.deepEqual(powerPlan(state), { action: "off", confirm: true, label: "Turn off" }, state);
  for (const state of ["unavailable", "unknown", undefined, null]) assert.equal(powerPlan(state).action, null, String(state));
});

test("apps view: the sources and the current one", () => {
  assert.deepEqual(appsView({ source_list: ["Steam", "Jellyfin"], source: "Jellyfin", app_name: "Jellyfin TV" }), { sources: ["Steam", "Jellyfin"], current: "Jellyfin" });
  assert.deepEqual(appsView({ source_list: ["Steam"], source: null }), { sources: ["Steam"], current: null });
  assert.deepEqual(appsView(undefined), { sources: [], current: null });
  assert.deepEqual(appsView({ source_list: [1, "A"] }).sources, ["A"]);
});

// ---- the element ----------------------------------------------------------------------------------------------------

const ENTITIES = {
  "media_player.box": { entity_id: "media_player.box", device_id: "d1", platform: "piston" },
};

function setup({ mp = "idle", attrs = {}, config = {}, entities = ENTITIES, callService } = {}) {
  const dom = installDom();
  loadCard();
  const card = new dom.defined["piston-remote-card"]();
  const sent = [], calls = [], subscribed = [];
  card.isConnected = true;
  card.connectedCallback();
  card.setConfig({ device_id: "d1", ...config });
  const hass = {
    connection: { sendMessage: (m) => sent.push(m), subscribeMessage: (cb) => { subscribed.push(cb); return Promise.resolve(() => {}); } },
    entities,
    states: { "media_player.box": { state: mp, attributes: { source_list: ["Steam", "Jellyfin", "YouTube"], source: "Jellyfin", app_name: "Jellyfin", is_volume_muted: false, ...attrs } } },
    callService: callService || (async (domain, service, data) => { calls.push([domain, service, data]); }),
  };
  card.hass = hass;
  const q = (sel) => card.shadowRoot.querySelector(sel);
  const settle = () => new Promise((r) => setImmediate(r));
  const setMp = (state, a = {}) => { hass.states["media_player.box"] = { state, attributes: { ...hass.states["media_player.box"].attributes, ...a } }; card.hass = { ...hass }; };
  const texts = () => card._el.dialog.descendants().map((n) => n.textContent).filter(Boolean);
  const button = (text) => card._el.dialog.descendants().find((n) => n.tagName === "BUTTON" && n.textContent === text);
  return { dom, card, hass, sent, calls, q, settle, setMp, texts, button, status: async (s) => { await settle(); subscribed[0]({ status: s }); } };
}

test("power while off sends Wake on LAN at once, no question", async () => {
  const t = setup({ mp: "off" });
  try {
    await t.status("device_off");
    assert.equal(t.card._el.powerText.textContent, "Turn on");
    assert.equal(t.card._el.power.disabled, false);
    t.q("#power").fire("click");
    await t.settle();
    assert.deepEqual(t.calls, [["media_player", "turn_on", { entity_id: "media_player.box" }]]);
    assert.equal(t.card._el.overlay.classList.contains("show"), false);
  } finally { t.dom.uninstall(); }
});

test("power while on asks first; Cancel does nothing, Confirm turns off", async () => {
  const t = setup();
  try {
    await t.status("ready");
    assert.equal(t.card._el.powerText.textContent, "Turn off");
    t.q("#power").fire("click");
    await t.settle();
    assert.deepEqual(t.calls, []);                                         // nothing before the answer
    assert.equal(t.card._el.overlay.classList.contains("show"), true);
    assert.ok(t.texts().includes("Turn off the device?"));
    t.button("Cancel").fire("click");
    await t.settle();
    assert.deepEqual(t.calls, []);
    assert.equal(t.card._el.overlay.classList.contains("show"), false);
    t.q("#power").fire("click");
    t.button("Turn off").fire("click");
    await t.settle();
    assert.deepEqual(t.calls, [["media_player", "turn_off", { entity_id: "media_player.box" }]]);
    assert.equal(t.card._el.overlay.classList.contains("show"), false);
  } finally { t.dom.uninstall(); }
});

test("tapping outside the dialog closes it", async () => {
  const t = setup();
  try {
    await t.status("ready");
    t.q("#power").fire("click");
    t.card._el.overlay.fire("click", { target: t.card._el.overlay });
    assert.equal(t.card._el.overlay.classList.contains("show"), false);
    assert.deepEqual(t.calls, []);
  } finally { t.dom.uninstall(); }
});

test("the app picker lists the sources, shows the current one and launches the chosen one", async () => {
  const t = setup();
  try {
    await t.status("ready");
    const apps = t.card._el.apps;
    assert.deepEqual(apps.children.map((o) => o.textContent), ["Steam", "Jellyfin", "YouTube"]);
    assert.equal(apps.value, "Jellyfin");
    assert.equal(apps.disabled, false);
    apps.value = "YouTube";
    apps.fire("change");
    await t.settle();
    assert.deepEqual(t.calls, [["media_player", "select_source", { entity_id: "media_player.box", source: "YouTube" }]]);
  } finally { t.dom.uninstall(); }
});

test("the picker is disabled while the device is off, and the list is not rebuilt on every state push", async () => {
  const t = setup();
  try {
    await t.status("ready");
    const first = t.card._el.apps.children;
    t.card.hass = { ...t.hass };
    assert.equal(t.card._el.apps.children, first);                          // same nodes: an open picker would not close
    t.setMp("off", { source: null });
    assert.equal(t.card._el.apps.disabled, true);
    assert.equal(t.card._el.apps.children[0].textContent, "Choose an app");
  } finally { t.dom.uninstall(); }
});

test("volume: up and down call the service, hold repeats every 180 ms after 400 ms, release stops", async () => {
  const t = setup();
  try {
    await t.status("ready");
    const up = t.q('[data-vol="up"]');
    up.fire("pointerdown");
    t.dom.advance(390);
    await t.settle();
    assert.equal(t.calls.length, 1);
    t.dom.advance(10 + 180 * 3);                                           // the hold starts at 400 ms, then every 180 ms
    await t.settle();
    assert.ok(t.calls.length >= 4);
    assert.ok(t.calls.every((c) => c[0] === "media_player" && c[1] === "volume_up" && c[2].entity_id === "media_player.box"));
    up.fire("pointerup");
    const n = t.calls.length;
    t.dom.advance(2000);
    await t.settle();
    assert.equal(t.calls.length, n);
    t.q('[data-vol="down"]').fire("pointerdown");
    t.q('[data-vol="down"]').fire("pointerup");
    await t.settle();
    assert.equal(t.calls[t.calls.length - 1][1], "volume_down");
  } finally { t.dom.uninstall(); }
});

test("mute toggles by the current state and does not repeat", async () => {
  const t = setup({ attrs: { is_volume_muted: true } });
  try {
    await t.status("ready");
    t.q('[data-vol="mute"]').fire("pointerdown");
    t.dom.advance(2000);
    await t.settle();
    assert.deepEqual(t.calls, [["media_player", "volume_mute", { entity_id: "media_player.box", is_volume_muted: false }]]);
  } finally { t.dom.uninstall(); }
});

test("volume repeat stops when the page goes to the background and volume is off while the device is off", async () => {
  const t = setup();
  try {
    await t.status("ready");
    t.q('[data-vol="down"]').fire("pointerdown");
    t.dom.advance(600);
    t.dom.document.visibilityState = "hidden";
    t.dom.document.fire("visibilitychange");
    await t.settle();
    const n = t.calls.length;
    t.dom.advance(2000);
    await t.settle();
    assert.equal(t.calls.length, n);
    assert.equal(t.dom.timers.size, 0);
  } finally { t.dom.uninstall(); }
  const off = setup({ mp: "off" });
  try {
    await off.status("device_off");
    assert.equal(off.q("#vol").classList.contains("off"), true);
    off.q('[data-vol="up"]').fire("pointerdown");
    await off.settle();
    assert.deepEqual(off.calls, []);
  } finally { off.dom.uninstall(); }
});

test("a failing service shows its message on the card", async () => {
  const t = setup({ mp: "off", callService: async () => { throw new Error("The device's MAC address is not known yet."); } });
  try {
    await t.status("device_off");
    t.q("#power").fire("click");
    await t.settle();
    assert.equal(t.card._el.toast.textContent, "The device's MAC address is not known yet.");
  } finally { t.dom.uninstall(); }
});

test("the show_* options hide their parts", async () => {
  const t = setup({ config: { show_power: false, show_apps: false, show_volume: false, show_keyboard: false } });
  try {
    await t.status("ready");
    const el = t.card._el;
    assert.deepEqual([el.bar.hidden, el.vol.hidden, el.kbBtn.hidden, el.kb.hidden], [true, true, true, true]);
  } finally { t.dom.uninstall(); }
  const partial = setup({ config: { show_apps: false } });
  try {
    await partial.status("ready");
    assert.deepEqual([partial.card._el.bar.hidden, partial.card._el.apps.hidden, partial.card._el.power.hidden], [false, true, false]);
  } finally { partial.dom.uninstall(); }
});

test("in a preview nothing is called", async () => {
  const dom = installDom();
  try {
    loadCard();
    const card = new dom.defined["piston-remote-card"]();
    card.preview = true; card.isConnected = true;
    const calls = [];
    card.setConfig({ device_id: "d1" });
    card.hass = { connection: { sendMessage() {}, subscribeMessage() { throw new Error("no"); } }, entities: ENTITIES,
      states: { "media_player.box": { state: "off", attributes: {} } }, callService: async (...a) => { calls.push(a); } };
    card._call("media_player", "turn_on", {});
    assert.deepEqual(calls, []);
  } finally { dom.uninstall(); }
});

test("there is no kebab menu and no quit pill any more", async () => {
  const t = setup();
  try {
    await t.status("ready");
    const html = t.card.shadowRoot.innerHTML;
    assert.doesNotMatch(html, /id="more"|id="quit"|Restart|Quit/);
    assert.equal(t.card._el.quit, undefined);
  } finally { t.dom.uninstall(); }
});

test("a long press on the power button does nothing special", async () => {
  const t = setup();
  try {
    await t.status("ready");
    t.q("#power").fire("pointerdown");
    t.dom.advance(1500);
    t.q("#power").fire("pointerup");
    assert.equal(t.card._el.overlay.classList.contains("show"), false);
  } finally { t.dom.uninstall(); }
});

test("choosing Steam in the picker closes the running app", async () => {
  const t = setup();
  try {
    await t.status("ready");
    t.card._el.apps.value = "Steam";
    t.card._el.apps.fire("change");
    await t.settle();
    assert.deepEqual(t.calls, [["media_player", "select_source", { entity_id: "media_player.box", source: "Steam" }]]);
  } finally { t.dom.uninstall(); }
});

test("with only Steam to pick, the list says where to add apps", async () => {
  const t = setup({ attrs: { source_list: ["Steam"], source: "Steam" } });
  try {
    await t.status("ready");
    const options = t.card._el.apps.children;
    assert.deepEqual(options.map((o) => o.textContent), ["Steam", "Add apps in the Piston panel to list them here"]);
    assert.equal(options[1].disabled, true);
    t.card._el.apps.value = "";
    t.card._el.apps.fire("change");                       // the hint is not a choice
    await t.settle();
    assert.deepEqual(t.calls, []);
  } finally { t.dom.uninstall(); }
});

test("the options that are switched off are really hidden: the stylesheet cannot override the hidden attribute", () => {
  const t = setup();
  try {
    assert.match(t.card.shadowRoot.innerHTML, /\[hidden\] \{ display: none !important; \}/);
  } finally { t.dom.uninstall(); }
});

test("state pushes that change nothing the card shows do no work", async () => {
  const t = setup();
  try {
    await t.status("ready");
    let renders = 0;
    const original = t.card._update.bind(t.card);
    t.card._update = () => { renders++; original(); };
    for (let i = 0; i < 50; i++) t.card.hass = { ...t.hass };         // a new hass object, the same entities and media player
    assert.equal(renders, 0);
    t.setMp("playing");                                                // the media player changed: one render
    assert.equal(renders, 1);
    t.card.hass = { ...t.hass, entities: { ...ENTITIES } };            // the registry changed: it is resolved again
    assert.equal(renders, 2);
    assert.equal(t.card._hass.entities !== ENTITIES, true);
  } finally { t.dom.uninstall(); }
});

test("the latest hass is always the one used for service calls, even when nothing was re-rendered", async () => {
  const t = setup();
  try {
    await t.status("ready");
    const calls = [];
    t.card.hass = { ...t.hass, callService: async (...a) => { calls.push(a); } };
    t.q('[data-vol="up"]').fire("pointerdown");
    t.q('[data-vol="up"]').fire("pointerup");
    await t.settle();
    assert.equal(calls.length, 1);
  } finally { t.dom.uninstall(); }
});

// ---- volume: a slider for system audio, buttons for CEC ------------------------------------------------------------------

test("volume view: system audio can set a level, CEC only steps", () => {
  assert.deepEqual(volumeView({ supported_features: 4 | 8 | 1, volume_level: 0.42, is_volume_muted: false }), { mode: "level", percent: 42, muted: false });
  assert.deepEqual(volumeView({ supported_features: 4, volume_level: 1, is_volume_muted: true }), { mode: "level", percent: 100, muted: true });
  assert.equal(volumeView({ supported_features: 8, volume_level: null }).mode, "steps");              // CEC: no VOLUME_SET, no level
  assert.equal(volumeView({ supported_features: 4, volume_level: null }).mode, "steps");              // system audio, level not known yet
  assert.equal(volumeView({ supported_features: 8, volume_level: 0.5 }).mode, "steps");               // a level it cannot set
  assert.equal(volumeView(undefined).mode, "steps");
  assert.equal(volumeView({ supported_features: 4, volume_level: 7 }).percent, 100);                  // clamped
  assert.equal(volumeView({ supported_features: 4, volume_level: -1 }).percent, 0);
  assert.equal(volumeView({ supported_features: 4, volume_level: 0.005 }).percent, 1);
});

test("the volume sender passes one value per interval and hands out the held-back newest one", () => {
  const v = new VolumeSender(100);
  assert.equal(v.push(10, 1000), 10);
  assert.equal(v.push(11, 1030), null);
  assert.equal(v.push(12, 1060), null);
  assert.equal(v.flush(1100), 12);                       // the newest one wins
  assert.equal(v.flush(1100), null);
  assert.equal(v.push(13, 1150), null);                  // too soon after the flush
  assert.equal(v.push(14, 1250), 14);
});

const SYSTEM = { supported_features: 4 | 8 | 1, volume_level: 0.3, is_volume_muted: false };
const CEC = { supported_features: 8 | 1, volume_level: null, is_volume_muted: false };

test("system audio shows a slider with the current level instead of the step buttons", async () => {
  const t = setup({ attrs: SYSTEM });
  try {
    await t.status("ready");
    const el = t.card._el;
    assert.deepEqual([el.volLevel.hidden, el.vol.hidden], [false, true]);
    assert.equal(el.volRange.value, "30");
    assert.equal(el.volText.textContent, "30%");
    assert.equal(el.volMuteIcon.getAttribute("icon"), "mdi:volume-high");
  } finally { t.dom.uninstall(); }
});

test("CEC keeps the three step buttons", async () => {
  const t = setup({ attrs: CEC });
  try {
    await t.status("ready");
    assert.deepEqual([t.card._el.volLevel.hidden, t.card._el.vol.hidden], [true, false]);
  } finally { t.dom.uninstall(); }
});

test("the card follows a change of mode (the device switched between system volume and CEC)", async () => {
  const t = setup({ attrs: CEC });
  try {
    await t.status("ready");
    t.setMp("idle", SYSTEM);
    assert.deepEqual([t.card._el.volLevel.hidden, t.card._el.vol.hidden], [false, true]);
    t.setMp("idle", CEC);
    assert.deepEqual([t.card._el.volLevel.hidden, t.card._el.vol.hidden], [true, false]);
  } finally { t.dom.uninstall(); }
});

test("dragging the slider sets the volume, at most every 100 ms, and the final position always arrives", async () => {
  const t = setup({ attrs: SYSTEM });
  try {
    await t.status("ready");
    const range = t.card._el.volRange;
    range.fire("pointerdown");
    const drag = (v) => { range.value = String(v); range.fire("input"); };
    drag(31); drag(32); drag(33); drag(34);               // four inputs within one instant
    await t.settle();
    assert.deepEqual(t.calls, [["media_player", "volume_set", { entity_id: "media_player.box", volume_level: 0.31 }]]);
    t.dom.advance(120);                                    // the trailing timer sends the newest held value
    await t.settle();
    assert.equal(t.calls.length, 2);
    assert.deepEqual(t.calls[1][2], { entity_id: "media_player.box", volume_level: 0.34 });
    drag(40);
    range.fire("change");                                  // released
    await t.settle();
    assert.deepEqual(t.calls[t.calls.length - 1][2], { entity_id: "media_player.box", volume_level: 0.4 });
    assert.equal(t.card._el.volText.textContent, "40%");
  } finally { t.dom.uninstall(); }
});

test("the slider is not moved by state pushes while it is being dragged, and follows them afterwards", async () => {
  const t = setup({ attrs: SYSTEM });
  try {
    await t.status("ready");
    const range = t.card._el.volRange;
    range.fire("pointerdown");
    range.value = "55"; range.fire("input");
    t.setMp("idle", { ...SYSTEM, volume_level: 0.3 });     // an old level echoes back
    assert.equal(range.value, "55");
    range.fire("change");
    t.setMp("idle", { ...SYSTEM, volume_level: 0.62 });
    assert.equal(range.value, "62");
    assert.equal(t.card._el.volText.textContent, "62%");
  } finally { t.dom.uninstall(); }
});

test("the slider's mute button toggles by the current state and shows it", async () => {
  const t = setup({ attrs: { ...SYSTEM, is_volume_muted: true } });
  try {
    await t.status("ready");
    assert.equal(t.card._el.volMuteIcon.getAttribute("icon"), "mdi:volume-off");
    t.card._el.volMute.fire("click");
    await t.settle();
    assert.deepEqual(t.calls, [["media_player", "volume_mute", { entity_id: "media_player.box", is_volume_muted: false }]]);
  } finally { t.dom.uninstall(); }
});

test("the slider does nothing while the device is off and a pending drag is dropped in the background", async () => {
  const off = setup({ mp: "off", attrs: SYSTEM });
  try {
    await off.status("device_off");
    assert.equal(off.card._el.volLevel.classList.contains("off"), true);
    off.card._el.volRange.value = "50"; off.card._el.volRange.fire("input"); off.card._el.volRange.fire("change");
    off.card._el.volMute.fire("click");
    await off.settle();
    assert.deepEqual(off.calls, []);
  } finally { off.dom.uninstall(); }
  const t = setup({ attrs: SYSTEM });
  try {
    await t.status("ready");
    const range = t.card._el.volRange;
    range.fire("pointerdown");
    range.value = "20"; range.fire("input");
    range.value = "21"; range.fire("input");              // held back for the trailing timer
    t.dom.document.visibilityState = "hidden";
    t.dom.document.fire("visibilitychange");
    await t.settle();
    const n = t.calls.length;                              // the first value went out at once; the held-back one must not follow
    t.dom.advance(500);
    await t.settle();
    assert.equal(t.calls.length, n);
    assert.equal(t.dom.timers.size, 0);
  } finally { t.dom.uninstall(); }
});

test("show_volume off hides both the buttons and the slider", async () => {
  for (const attrs of [SYSTEM, CEC]) {
    const t = setup({ attrs, config: { show_volume: false } });
    try {
      await t.status("ready");
      assert.deepEqual([t.card._el.volLevel.hidden, t.card._el.vol.hidden], [true, true]);
    } finally { t.dom.uninstall(); }
  }
});
