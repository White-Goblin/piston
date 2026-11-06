import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./dom.js";
import { loadCard } from "./load.js";

const { layoutFrom, CONFIG_DEFAULTS, editorSchema, editorData, editorConfig, EDITOR_LABELS } = loadCard();

test("no layout settings means the default size and no extra space", () => {
  assert.deepEqual(layoutFrom({}), { padHeight: null, gapPadVolume: 0, gapVolumeButtons: 0 });
  assert.deepEqual(layoutFrom(undefined), { padHeight: null, gapPadVolume: 0, gapVolumeButtons: 0 });
});

test("the touchpad height and the two gaps are taken from the config and kept within their ranges", () => {
  assert.deepEqual(layoutFrom({ pad_height: 50, gap_pad_volume: 12, gap_volume_buttons: 40 }), { padHeight: 50, gapPadVolume: 12, gapVolumeButtons: 40 });
  assert.equal(layoutFrom({ pad_height: 5 }).padHeight, 15);
  assert.equal(layoutFrom({ pad_height: 500 }).padHeight, 80);
  assert.equal(layoutFrom({ gap_pad_volume: -10 }).gapPadVolume, 0);
  assert.equal(layoutFrom({ gap_volume_buttons: 9999 }).gapVolumeButtons, 160);
});

test("nonsense values fall back to the defaults instead of breaking the layout", () => {
  for (const bad of ["x", NaN, Infinity, {}, [], null, undefined, ""]) {
    assert.deepEqual(layoutFrom({ pad_height: bad, gap_pad_volume: bad, gap_volume_buttons: bad }), { padHeight: null, gapPadVolume: 0, gapVolumeButtons: 0 }, String(bad));
  }
  assert.equal(layoutFrom({ pad_height: "45" }).padHeight, 45);                // numbers typed as text in YAML still work
});

test("the editor has a slider for the touchpad height and one for each gap, with units", () => {
  const byName = Object.fromEntries(editorSchema().map((f) => [f.name, f]));
  assert.deepEqual(byName.pad_height.selector.number, { min: 15, max: 80, step: 1, mode: "slider", unit_of_measurement: "% of screen" });
  assert.equal(byName.gap_pad_volume.selector.number.mode, "slider");
  assert.equal(byName.gap_volume_buttons.selector.number.unit_of_measurement, "px");
  for (const field of editorSchema()) assert.ok(EDITOR_LABELS[field.name], field.name);
  assert.match(EDITOR_LABELS.gap_pad_volume, /touchpad and the volume/);
  assert.match(EDITOR_LABELS.gap_volume_buttons, /volume and the buttons/);
});

test("the editor shows the defaults but does not write them back, so the YAML stays short", () => {
  assert.deepEqual(editorData({ device_id: "d1" }), { ...CONFIG_DEFAULTS, device_id: "d1" });
  const everything = { ...CONFIG_DEFAULTS, device_id: "d1" };                      // what the form reports after any change
  assert.deepEqual(editorConfig({ type: "custom:piston-remote-card" }, everything), { type: "custom:piston-remote-card", device_id: "d1" });
  assert.deepEqual(editorConfig({ type: "t", device_id: "d1" }, { ...everything, pad_height: 50, gap_volume_buttons: 24, speed: 1.5, show_power: false }),
    { type: "t", device_id: "d1", pad_height: 50, gap_volume_buttons: 24, speed: 1.5, show_power: false });
  assert.deepEqual(editorConfig({ type: "t", device_id: "d1", pad_height: 50 }, { ...everything, pad_height: 34 }), { type: "t", device_id: "d1" });   // back to the default removes the key
});

// ---- the element ------------------------------------------------------------------------------------------------------

const SYSTEM = { supported_features: 4 | 8 | 1, volume_level: 0.3, is_volume_muted: false };

function setup(config, attrs = {}) {
  const dom = installDom();
  loadCard();
  const card = new dom.defined["piston-remote-card"]();
  card.isConnected = true;
  card.connectedCallback();
  card.setConfig({ device_id: "d1", ...config });
  card.hass = {
    connection: { sendMessage() {}, subscribeMessage: () => Promise.resolve(() => {}) },
    entities: { "media_player.box": { entity_id: "media_player.box", device_id: "d1", platform: "piston" } },
    states: { "media_player.box": { state: "idle", attributes: { source_list: ["Steam"], source: "Steam", ...attrs } } },
  };
  return { dom, card, root: card._el.root.style.props, el: card._el };
}

test("the default card keeps the default touchpad limits and adds no space", () => {
  const t = setup({});
  try {
    assert.deepEqual(t.root, {});
    assert.deepEqual([t.el.vol.style.marginTop, t.el.dock.style.marginTop], ["0px", "0px"]);
  } finally { t.dom.uninstall(); }
});

test("a touchpad height sets it in screen percent and lifts the default minimum and maximum", () => {
  const t = setup({ pad_height: 50 });
  try {
    assert.deepEqual(t.root, { "--pr-pad-height": "50vh", "--pr-pad-min": "120px", "--pr-pad-max": "none" });
    t.card.setConfig({ device_id: "d1" });                                           // back to the default
    assert.deepEqual(t.root, {});
  } finally { t.dom.uninstall(); }
});

test("the space under the touchpad goes above the volume buttons, the other gap between the volume and the buttons", () => {
  const t = setup({ gap_pad_volume: 20, gap_volume_buttons: 44 });
  try {
    assert.deepEqual([t.el.vol.style.marginTop, t.el.volLevel.style.marginTop, t.el.dock.style.marginTop], ["20px", "0px", "44px"]);
  } finally { t.dom.uninstall(); }
});

test("with the volume slider the first gap sits above the slider instead", () => {
  const t = setup({ gap_pad_volume: 20, gap_volume_buttons: 44 }, SYSTEM);
  try {
    assert.deepEqual([t.el.vol.style.marginTop, t.el.volLevel.style.marginTop, t.el.dock.style.marginTop], ["0px", "20px", "44px"]);
  } finally { t.dom.uninstall(); }
});

test("without the volume control the first gap sits above the buttons and the second one is not used", () => {
  const t = setup({ gap_pad_volume: 20, gap_volume_buttons: 44, show_volume: false });
  try {
    assert.equal(t.el.dock.style.marginTop, "20px");
  } finally { t.dom.uninstall(); }
});

test("the gap follows the volume control when the device switches between system volume and CEC", () => {
  const t = setup({ gap_pad_volume: 20 });
  try {
    assert.equal(t.el.vol.style.marginTop, "20px");
    t.card.hass = { ...t.card._hass, states: { "media_player.box": { state: "idle", attributes: { source_list: ["Steam"], source: "Steam", ...SYSTEM } } } };
    assert.deepEqual([t.el.vol.style.marginTop, t.el.volLevel.style.marginTop], ["0px", "20px"]);
  } finally { t.dom.uninstall(); }
});

test("nonsense layout values in the config leave the card at its defaults", () => {
  const t = setup({ pad_height: "huge", gap_pad_volume: "lots", gap_volume_buttons: -5 });
  try {
    assert.deepEqual(t.root, {});
    assert.deepEqual([t.el.vol.style.marginTop, t.el.dock.style.marginTop], ["0px", "0px"]);
  } finally { t.dom.uninstall(); }
});
