import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./dom.js";
import { loadCard } from "./load.js";

const { editorSchema, editorData, editorConfig, EDITOR_LABELS } = loadCard();

const ENTITIES = {
  "media_player.box": { entity_id: "media_player.box", device_id: "d1", platform: "piston" },
};

function setup({ mp = "idle", subscribe, config = { device_id: "d1" }, hass: hassExtra } = {}) {
  const dom = installDom();
  loadCard();
  const card = new dom.defined["piston-remote-card"]();
  const subscribed = [];
  card.isConnected = true;
  card.connectedCallback();
  card.setConfig(config);
  card.hass = hassExtra || {
    connection: { sendMessage() {}, subscribeMessage: subscribe || ((cb) => { subscribed.push(cb); return Promise.resolve(() => {}); }) },
    entities: ENTITIES,
    states: { "media_player.box": { state: mp, attributes: { source_list: ["Steam"], source: "Steam" } } },
    callService: async () => {},
  };
  const settle = () => new Promise((r) => setImmediate(r));
  return { dom, card, subscribed, settle, status: async (s) => { await settle(); subscribed[0]({ status: s }); } };
}

// The state table of the feature spec: what is enabled and which banner shows.
const ROWS = [
  // status, media player, banner, pad+wheel+Back+keyboard, Steam/QAM tiles, power
  ["ready", "idle", null, true, true, true],
  ["desktop", "idle", "Desktop mode", true, false, true],
  ["device_off", "off", "Device is off", false, false, true],
  ["unreachable", "idle", "Can't reach the device", false, false, true],
  ["no_capability", "idle", "Update the Piston plugin", false, false, true],
  ["disabled", "idle", "Remote input is turned off on the device", false, false, true],
  ["error", "idle", "The device could not start remote input", false, false, true],
  ["skew", "idle", "The Piston plugin and integration versions do not match", false, false, true],
];

for (const [status, mp, banner, input, shell, power] of ROWS) {
  test(`state row: ${status}`, async () => {
    const t = setup({ mp });
    try {
      await t.status(status);
      const el = t.card._el, q = (sel) => t.card.shadowRoot.querySelector(sel);
      assert.equal(el.badge.classList.contains("show"), banner !== null);
      assert.equal(el.badgeText.textContent, banner || "");
      assert.equal(el.pad.classList.contains("disabled"), !input);
      assert.equal(el.wheel.classList.contains("off"), !input);
      assert.equal(el.esc.classList.contains("off"), !input);
      assert.equal(el.kbBtn.classList.contains("off"), !input);
      assert.equal(q('[data-act="steam"]').classList.contains("off"), !shell);
      assert.equal(q('[data-act="qam"]').classList.contains("off"), !shell);
      assert.equal(el.power.disabled, !power);
    } finally { t.dom.uninstall(); }
  });
}

test("state row: a non-administrator is told so, and the rest is disabled but power still works", async () => {
  const t = setup({ mp: "idle", subscribe: () => Promise.reject({ code: "unauthorized", message: "remote input needs an administrator" }) });
  try {
    await t.settle(); await t.settle();
    assert.equal(t.card._el.badgeText.textContent, "Only administrators can use remote input");
    assert.equal(t.card._el.pad.classList.contains("disabled"), true);
    assert.equal(t.card._el.power.disabled, false);
  } finally { t.dom.uninstall(); }
});

test("a status change from disabled back to ready re-enables the controls and clears the banner", async () => {
  const t = setup();
  try {
    await t.status("disabled");
    assert.equal(t.card._el.pad.classList.contains("disabled"), true);
    t.subscribed[0]({ status: "ready" });
    assert.equal(t.card._el.pad.classList.contains("disabled"), false);
    assert.equal(t.card._el.badge.classList.contains("show"), false);
  } finally { t.dom.uninstall(); }
});

test("a card without a device asks for one and disables everything", async () => {
  const t = setup({ config: {} });
  try {
    await t.settle();
    assert.equal(t.card._el.badgeText.textContent, "Choose a Piston device");
    assert.equal(t.card._el.pad.classList.contains("disabled"), true);
    assert.equal(t.card._el.power.disabled, true);
    assert.equal(t.card._el.apps.disabled, true);
    assert.equal(t.card._el.vol.classList.contains("off"), true);
    assert.equal(t.subscribed.length, 0);
  } finally { t.dom.uninstall(); }
});

test("a device that is not found says so the same way", async () => {
  const t = setup({ subscribe: () => Promise.reject({ code: "not_found" }) });
  try {
    await t.settle(); await t.settle();
    assert.equal(t.card._el.badgeText.textContent, "Choose a Piston device");
  } finally { t.dom.uninstall(); }
});

test("the card never throws when entities, states, services or the connection are missing", async () => {
  for (const hass of [
    {}, { entities: {} }, { entities: null, states: null }, { connection: {} }, { connection: { sendMessage() {} } },
    { connection: { subscribeMessage: () => Promise.reject(new Error("offline")) }, entities: ENTITIES },
    { entities: ENTITIES, states: {} }, { entities: ENTITIES, states: { "media_player.box": {} } },
  ]) {
    const t = setup({ hass });
    try {
      await t.settle();
      t.card.send([{ k: "release" }]);
      t.card._call("media_player", "turn_on", {});
      t.card._el.power.fire("click");
      t.card._el.apps.fire("change");
      t.card.shadowRoot.querySelector('[data-vol="up"]').fire("pointerdown");
      t.card.shadowRoot.querySelector('[data-vol="up"]').fire("pointerup");
      t.dom.frame();
      t.card.disconnectedCallback();
    } finally { t.dom.uninstall(); }
  }
});

test("setConfig rejects a non-object configuration like other cards do", () => {
  const t = setup();
  try {
    assert.throws(() => t.card.setConfig(null));
    assert.throws(() => t.card.setConfig("x"));
  } finally { t.dom.uninstall(); }
});

test("the stub config picks the first Piston device and is empty without one", () => {
  const dom = installDom();
  try {
    loadCard();
    const Card = dom.defined["piston-remote-card"];
    assert.deepEqual(Card.getStubConfig({ entities: { a: { platform: "hue", device_id: "x" }, b: { platform: "piston", device_id: "d9" } } }), { device_id: "d9" });
    assert.deepEqual(Card.getStubConfig({ entities: {} }), { device_id: "" });
    assert.deepEqual(Card.getStubConfig(undefined), { device_id: "" });
    assert.ok(Card.getConfigElement());
  } finally { dom.uninstall(); }
});

// ---- the editor -------------------------------------------------------------------------------------------------------

test("the editor form has a Piston device picker, a speed slider and the show options", () => {
  const schema = editorSchema();
  assert.deepEqual(schema[0], { name: "device_id", required: true, selector: { device: { integration: "piston" } } });
  assert.deepEqual(schema.map((f) => f.name), ["device_id", "speed", "pad_height", "gap_pad_volume", "gap_volume_buttons", "show_power", "show_apps", "show_volume", "show_keyboard", "show_transport"]);
  assert.deepEqual(schema[1].selector.number, { min: 0.25, max: 4, step: 0.05, mode: "slider" });
  for (const field of schema) assert.ok(EDITOR_LABELS[field.name], field.name);
});

test("the editor shows the defaults under the configured values", () => {
  assert.deepEqual(editorData({ device_id: "d1", speed: 2 }), { speed: 2, pad_height: 34, gap_pad_volume: 0, gap_volume_buttons: 0, show_power: true, show_apps: true, show_volume: true, show_keyboard: true, show_transport: true, device_id: "d1" });
  assert.equal(editorData(undefined).speed, 1);
});

test("the saved config keeps the card type, takes the form's values and drops empty ones", () => {
  assert.deepEqual(editorConfig({ type: "custom:piston-remote-card", device_id: "d1" }, { device_id: "d2", show_power: false, speed: undefined }),
    { type: "custom:piston-remote-card", device_id: "d2", show_power: false });
  assert.deepEqual(editorConfig({ type: "x", device_id: "d1" }, { device_id: "" }), { type: "x" });
});

test("the editor element reports a changed value as a config-changed event and passes hass to the form", () => {
  const dom = installDom();
  try {
    loadCard();
    const editor = new dom.defined["piston-remote-card-editor"]();
    editor.hass = { marker: 1 };
    editor.setConfig({ type: "custom:piston-remote-card", device_id: "d1" });
    const form = editor.shadowRoot.querySelector("ha-form");
    assert.deepEqual(form.hass, { marker: 1 });
    assert.equal(form.schema.length, 10);
    assert.equal(form.computeLabel({ name: "speed" }), "Pointer speed");
    assert.equal(form.data.device_id, "d1");
    form.fire("value-changed", { detail: { value: { device_id: "d1", speed: 1.5, show_apps: false } } });
    const [event] = editor.dispatched;
    assert.equal(event.type, "config-changed");
    assert.deepEqual(event.detail.config, { type: "custom:piston-remote-card", device_id: "d1", speed: 1.5, show_apps: false });
    assert.equal(event.bubbles && event.composed, true);
    editor.setConfig(event.detail.config);
    assert.equal(editor.shadowRoot.querySelector("ha-form"), form);          // the same form: no flicker on every change
    assert.equal(form.data.speed, 1.5);
  } finally { dom.uninstall(); }
});
