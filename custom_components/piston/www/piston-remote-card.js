/* Piston remote card: a touchpad, arrow wheel, buttons, typing, volume, power and app picker for a SteamOS device.
 *
 * Shipped inside the Piston integration and added to the dashboard by it (no resource to add by hand). Plain JavaScript, no
 * build step. Input travels card -> Home Assistant websocket (`piston/input/send`, no reply) -> integration -> device.
 * The pure logic at the top has no DOM and is tested with `node --test tests/js`. */
(function () {
  "use strict";

  const DOMAIN = "piston";
  const SEND = "piston/input/send";
  const UNSET = {};   // "no media player state seen yet" (not the same as an unknown state)
  const SUBSCRIBE = "piston/input/subscribe";

  // ------------------------------------------------------------------------------------------------------------------
  // Pure logic
  // ------------------------------------------------------------------------------------------------------------------

  /** What the card may do now. `status` is what the subscription reported (or "connecting"/"unauthorized"/"not_found"/
   * "no_device" from the card itself), `mpState` the media player's state, `preview` whether it is a card editor preview.
   * input: pad, wheel, Back (Esc) and typing. shell: the Steam menu and Quick Access tiles. power: the power button.
   * apps/volume follow the media player. */
  function deriveState({ status, mpState, preview }) {
    const MP_OFF = ["off", "standby", "unavailable", "unknown", undefined, null];
    const awake = !MP_OFF.includes(mpState);
    const base = (kind, banner, input, shell, power) => ({
      kind, banner, input, shell, power,
      apps: awake && status !== "no_device" && status !== "not_found", volume: awake && status !== "no_device" && status !== "not_found",
    });
    if (preview) return { ...base("preview", "Preview", false, false, false), apps: false, volume: false };
    switch (status) {
      case "ready": return base("ready", null, true, true, true);
      case "desktop": return base("desktop", "Desktop mode", true, false, true);
      case "device_off": return base("device_off", "Device is off", false, false, true);
      case "unreachable": return base("unreachable", "Can't reach the device", false, false, true);
      case "no_capability": return base("no_capability", "Update the Piston plugin", false, false, true);
      case "disabled": return base("disabled", "Remote input is turned off on the device", false, false, true);
      case "error": return base("error", "The device could not start remote input", false, false, true);
      case "skew": return base("skew", "The Piston plugin and integration versions do not match", false, false, true);
      case "unauthorized": return base("unauthorized", "Only administrators can use remote input", false, false, true);
      case "not_found":
      case "no_device": return base("no_device", "Choose a Piston device", false, false, false);
      default: return base("connecting", null, false, false, mpState !== "unavailable");
    }
  }

  /** Sums finger travel and hands it out as one move event per animation frame (scaled by the card's speed setting). */
  class MoveBatcher {
    constructor(speed) { this.speed = clampSpeed(speed); this.dx = 0; this.dy = 0; }
    add(dx, dy) { this.dx += dx; this.dy += dy; }
    get pending() { return this.dx !== 0 || this.dy !== 0; }
    take() {
      if (!this.pending) return null;
      const event = { k: "move", dx: round2(this.dx * this.speed), dy: round2(this.dy * this.speed) };
      this.dx = this.dy = 0;
      return event;
    }
  }
  const round2 = (n) => Math.round(n * 100) / 100;
  function clampSpeed(speed) {
    const n = Number(speed);
    return Number.isFinite(n) && n > 0 ? Math.min(4, Math.max(0.25, n)) : 1;
  }

  /** Events waiting to be sent in the next frame: moves are summed (and scaled by the speed), everything else keeps its order,
   * and a pending move always goes out before the event that follows it (a click lands where the finger moved to). */
  class Outbox {
    constructor(speed) { this.batcher = new MoveBatcher(speed); this.events = []; }
    addMove(dx, dy) { this.batcher.add(dx, dy); }
    add(event) { this._flushMove(); this.events.push(event); }
    get empty() { return !this.batcher.pending && this.events.length === 0; }
    take() { this._flushMove(); const out = this.events; this.events = []; return out; }
    _flushMove() { const move = this.batcher.take(); if (move) this.events.push(move); }
  }

  /** Finger travel (px) becomes wheel notches: one per STEP px, the remainder carried. */
  const SCROLL_STEP = 14;
  class ScrollAccumulator {
    constructor(step = SCROLL_STEP) { this.step = step; this.y = 0; this.x = 0; }
    add(dy, dx) {
      this.y += dy; this.x += dx;
      const v = Math.trunc(this.y / this.step), h = Math.trunc(this.x / this.step);
      if (!v && !h) return null;
      this.y -= v * this.step; this.x -= h * this.step;
      return { k: "scroll", dx: h, dy: v };
    }
    reset() { this.y = this.x = 0; }
  }

  /** The velocity (px per ms, capped) of the last 100 ms of [time, dy, dx] samples, or null when it was too slow for a flick. */
  function flickFrom(samples, now) {
    const recent = samples.filter((s) => now - s[0] <= FLICK_WINDOW);
    if (recent.length < 2) return null;
    const span = Math.max(16, now - recent[0][0]);
    const cap = (v) => Math.max(-FLICK_MAX, Math.min(FLICK_MAX, v));
    const vy = cap(recent.reduce((a, s) => a + s[1], 0) / span), vx = cap(recent.reduce((a, s) => a + s[2], 0) / span);
    return Math.hypot(vy, vx) < FLICK_MIN ? null : { vy, vx };
  }

  /** A flick keeps scrolling and slows down exponentially, like a phone list. Velocities are px per ms. */
  const FLICK_TAU = 350, FLICK_MIN = 0.25, FLICK_MAX = 5, FLICK_WINDOW = 100;
  class Inertia {
    constructor(vy, vx) { this.vy = vy; this.vx = vx; }
    /** Advance by dt ms: the finger-equivalent travel in px, and whether it is finished. */
    step(dt) {
      const d = Math.min(dt, 32), k = Math.exp(-d / FLICK_TAU);
      this.vy *= k; this.vx *= k;
      return { dy: this.vy * d, dx: this.vx * d, done: Math.hypot(this.vy, this.vx) <= 0.03 };
    }
  }

  /** The touchpad's gesture rules (ported from the standalone page). Feed it the touch points ([[x, y], ...]) and the time
   * in ms; it answers with actions: {type: "move", dx, dy}, {type: "scroll", dy, dx} (px), {type: "click", button, x, y} and
   * {type: "flick", vy, vx}. One finger drags the pointer, two fingers scroll, a short tap clicks (two fingers: right click). */
  class PadGesture {
    constructor() { this.g = null; this.samples = []; }
    get active() { return this.g !== null; }
    start(points, now) {
      this.samples = [];
      if (!this.g) this.g = { start: now, n: points.length, moved: false, last: points };
      else { this.g.n = Math.max(this.g.n, points.length); this.g.last = points; if (this.g.n > 1) this.g.moved = false; }
      return [];
    }
    move(points, now) {
      const g = this.g;
      if (!g) return [];
      const cur = points, out = [];
      if (cur.length === 1 && g.n === 1) {
        const dx = cur[0][0] - g.last[0][0], dy = cur[0][1] - g.last[0][1];
        if (!g.moved && Math.hypot(dx, dy) < 2 && now - g.start < 200) return out;   // a tap's jitter is not a drag
        g.moved = true;
        out.push({ type: "move", dx, dy });
      } else if (cur.length >= 2 && g.last.length >= 2) {
        const dy = (cur[0][1] + cur[1][1]) / 2 - (g.last[0][1] + g.last[1][1]) / 2;
        const dx = (cur[0][0] + cur[1][0]) / 2 - (g.last[0][0] + g.last[1][0]) / 2;
        if (Math.abs(dy) + Math.abs(dx) > 0.5) g.moved = true;
        out.push({ type: "scroll", dy, dx });
        this.samples.push([now, dy, dx]);
        while (this.samples.length && now - this.samples[0][0] > FLICK_WINDOW) this.samples.shift();
      }
      g.last = cur;
      return out;
    }
    /** `remaining` is how many fingers are still down; `lifted` the point of the finger that just left. */
    end(remaining, now, lifted) {
      const g = this.g;
      if (!g || remaining > 0) return [];
      const out = [];
      if (g.n >= 2 && g.moved) {
        const flick = this._flick(now);
        if (flick) out.push(flick);
      }
      if (!g.moved && now - g.start < 300) out.push({ type: "click", button: g.n >= 2 ? "r" : "l", x: lifted ? lifted[0] : 0, y: lifted ? lifted[1] : 0 });
      this.g = null;
      return out;
    }
    cancel() { this.g = null; this.samples = []; }
    _flick(now) {
      const flick = flickFrom(this.samples, now);
      this.samples = [];
      return flick ? { type: "flick", ...flick } : null;
    }
  }

  /** The arrow wheel: a tap on a wedge sends its arrow on release, holding repeats it, and moving the thumb around the ring
   * scrolls instead (clockwise = down), linearly: the scroll follows the thumb and stops when it stops. The card runs the
   * timers; this class decides. Actions: {type: "key", key}, {type: "spin_start"} and {type: "scroll", dy, angle}. */
  const WHEEL_HOLD_MS = 350, WHEEL_REPEAT_MS = 80, WHEEL_SPIN_PX = 12, WHEEL_PX_PER_RAD = 70;
  class WheelGesture {
    constructor() { this.wp = null; }
    get active() { return this.wp !== null; }
    /** `info`: {id, key, center: [x, y], point: [x, y], size}. */
    down(info) {
      if (this.wp) return [];
      const [cx, cy] = info.center, [px, py] = info.point;
      this.wp = { id: info.id, key: info.key, c: info.center, a: Math.atan2(py - cy, px - cx), r0: Math.max(20, Math.hypot(px - cx, py - cy)),
                  size: info.size, moved: 0, spin: false, fired: false };
      return [];
    }
    /** The hold timer fired: start repeating the arrow (unless the thumb is spinning). */
    hold() {
      const wp = this.wp;
      if (!wp || wp.spin) return [];
      wp.fired = true;
      return [{ type: "key", key: wp.key }];
    }
    get repeating() { return !!this.wp && this.wp.fired && !this.wp.spin; }
    move(id, point, now) {
      const wp = this.wp;
      if (!wp || id !== wp.id) return [];
      const [cx, cy] = wp.c;
      if (Math.hypot(point[0] - cx, point[1] - cy) < wp.size * 0.16) return [];   // the angle is noise near the hub
      const a = Math.atan2(point[1] - cy, point[0] - cx);
      let d = a - wp.a; wp.a = a;
      if (d > Math.PI) d -= 2 * Math.PI; else if (d < -Math.PI) d += 2 * Math.PI;
      wp.moved += d;
      const out = [];
      if (!wp.spin && Math.abs(wp.moved) * wp.r0 > WHEEL_SPIN_PX) { wp.spin = true; out.push({ type: "spin_start" }); }
      if (wp.spin) out.push({ type: "scroll", dy: -d * WHEEL_PX_PER_RAD, angle: a });   // linear: the scroll follows the thumb, no momentum
      return out;
    }
    up(id, cancelled, now) {
      const wp = this.wp;
      if (!wp || id !== wp.id) return [];
      this.wp = null;
      if (wp.spin) return [];
      return !wp.fired && !cancelled ? [{ type: "key", key: wp.key }] : [];
    }
    cancel() { this.wp = null; }
  }

  /** What the phone keyboard has typed, mirrored into minimal edits (so swipe typing, autocorrect and dictation work): the
   * hidden textarea always starts with one space so a backspace on an empty field is still seen. Only ASCII reaches the
   * device (it has no key for anything else): other characters are dropped, counted, and never backspaced. */
  const SENTINEL = " ";
  const MAX_TEXT = 256;
  const typable = (ch) => ch === "\n" || ch === "\t" || (ch >= " " && ch <= "~");
  class TextSync {
    constructor() { this.sent = SENTINEL; }
    reset() { this.sent = SENTINEL; return SENTINEL; }
    /** `value` is the textarea's value now. Returns {events, dropped, value}: `value` is what to put back in the textarea
     * (a fresh sentinel when it grew long or was emptied and no composition is running), or null to leave it. */
    diff(value, composing) {
      const sent = this.sent;
      let i = 0;
      const n = Math.min(value.length, sent.length);
      while (i < n && value[i] === sent[i]) i++;
      const events = [];
      const removed = Array.from(sent.slice(i)).filter(typable).length;
      for (let k = 0; k < removed; k++) events.push({ k: "key", key: "Backspace" });
      const added = Array.from(value.slice(i));
      const text = added.filter(typable).join("");
      const dropped = added.length - text.length;
      for (let p = 0; p < text.length; p += MAX_TEXT) events.push({ k: "text", s: text.slice(p, p + MAX_TEXT) });
      this.sent = value;
      let reset = null;
      if (!composing && (value.length === 0 || value.length > 120)) reset = this.reset();
      return { events, dropped, value: reset };
    }
  }

  /** What the power button does for a media player state: wake a device that is off or asleep (no question asked), turn off one
   * that is awake (after a confirmation), nothing when the state is unknown or the entity is unavailable. */
  function powerPlan(mpState) {
    if (mpState === "off" || mpState === "standby") return { action: "wake", confirm: false, label: "Turn on" };
    if (mpState === "unavailable" || mpState === "unknown" || mpState === undefined || mpState === null) return { action: null, confirm: false, label: "Turn on" };
    return { action: "off", confirm: true, label: "Turn off" };
  }

  /** The app picker's view of the media player: the sources (the offered apps, Steam first) and the current one. */
  const HOME_SOURCE = "Steam";
  function appsView(attrs) {
    const a = attrs || {};
    const sources = Array.isArray(a.source_list) ? a.source_list.filter((x) => typeof x === "string") : [];
    const current = typeof a.source === "string" ? a.source : null;
    return { sources, current };
  }

  /** How the device's volume is controlled. With system audio the media player has a level (0-1) and can set it, so the card
   * shows a slider; with HDMI CEC there is only up, down and an assumed mute, so it shows buttons. */
  const VOLUME_SET_FEATURE = 4;   // MediaPlayerEntityFeature.VOLUME_SET
  function volumeView(attrs) {
    const a = attrs || {};
    const level = typeof a.volume_level === "number" && Number.isFinite(a.volume_level) ? Math.min(1, Math.max(0, a.volume_level)) : null;
    const canSet = Number.isInteger(a.supported_features) && (a.supported_features & VOLUME_SET_FEATURE) !== 0;
    const mode = canSet && level !== null ? "level" : "steps";
    return { mode, percent: level === null ? 0 : Math.round(level * 100), muted: a.is_volume_muted === true };
  }

  /** Sends at most one volume value per interval while a slider is dragged; the newest value that was held back is handed out by
   * `flush()` (a trailing timer and the release use it), so the last position always arrives. */
  const VOLUME_INTERVAL_MS = 100;
  class VolumeSender {
    constructor(interval = VOLUME_INTERVAL_MS) { this.interval = interval; this.last = -Infinity; this.pending = null; }
    push(value, now) {
      if (now - this.last >= this.interval) { this.last = now; this.pending = null; return value; }
      this.pending = value;
      return null;
    }
    flush(now) {
      const value = this.pending;
      this.pending = null;
      if (value !== null) this.last = now;
      return value;
    }
  }

  /** The card editor's form (Home Assistant's `ha-form`): a Piston device picker, the pointer speed, the layout sliders and the
   * parts to show. */
  const EDITOR_LABELS = {
    device_id: "Device", speed: "Pointer speed", pad_height: "Touchpad height", gap_pad_volume: "Space between the touchpad and the volume",
    gap_volume_buttons: "Space between the volume and the buttons", show_power: "Show the power button", show_apps: "Show the app picker",
    show_volume: "Show the volume control", show_keyboard: "Show the keyboard button",
  };
  /** What a card without these keys does. `pad_height` is a percentage of the screen height (the touchpad then also loses its
   * default minimum and maximum size); the gaps are extra pixels of empty space. */
  const CONFIG_DEFAULTS = { speed: 1, pad_height: 34, gap_pad_volume: 0, gap_volume_buttons: 0, show_power: true, show_apps: true, show_volume: true, show_keyboard: true };
  const PAD_HEIGHT_RANGE = [15, 80], GAP_RANGE = [0, 160];
  const inRange = (value, [low, high], fallback) => {
    const usable = (typeof value === "number") || (typeof value === "string" && value.trim() !== "");
    const n = usable ? Number(value) : NaN;
    return Number.isFinite(n) ? Math.min(high, Math.max(low, n)) : fallback;
  };
  /** The layout the config asks for: `padHeight` is null for the default size (kept within its default limits), else a number of
   * percent of the screen height; the gaps are pixels, never negative. */
  function layoutFrom(config) {
    const c = config || {};
    return {
      padHeight: inRange(c.pad_height, PAD_HEIGHT_RANGE, null),
      gapPadVolume: inRange(c.gap_pad_volume, GAP_RANGE, 0),
      gapVolumeButtons: inRange(c.gap_volume_buttons, GAP_RANGE, 0),
    };
  }
  function editorSchema() {
    return [
      { name: "device_id", required: true, selector: { device: { integration: DOMAIN } } },
      { name: "speed", selector: { number: { min: 0.25, max: 4, step: 0.05, mode: "slider" } } },
      { name: "pad_height", selector: { number: { min: PAD_HEIGHT_RANGE[0], max: PAD_HEIGHT_RANGE[1], step: 1, mode: "slider", unit_of_measurement: "% of screen" } } },
      { name: "gap_pad_volume", selector: { number: { min: GAP_RANGE[0], max: GAP_RANGE[1], step: 2, mode: "slider", unit_of_measurement: "px" } } },
      { name: "gap_volume_buttons", selector: { number: { min: GAP_RANGE[0], max: GAP_RANGE[1], step: 2, mode: "slider", unit_of_measurement: "px" } } },
      { name: "show_power", selector: { boolean: {} } },
      { name: "show_apps", selector: { boolean: {} } },
      { name: "show_volume", selector: { boolean: {} } },
      { name: "show_keyboard", selector: { boolean: {} } },
    ];
  }
  /** The values the form shows: the card's defaults under what the user configured. */
  function editorData(config) {
    return { ...CONFIG_DEFAULTS, ...(config || {}) };
  }
  /** The config to save from what the form reports: the `type` is kept, and values that are just the default are not written (the
   * form reports every field), so the card's YAML stays short. */
  function editorConfig(previous, value) {
    const out = { ...(previous || {}), ...(value || {}) };
    for (const key of Object.keys(out)) {
      if (out[key] === undefined || out[key] === null || out[key] === "" || (key in CONFIG_DEFAULTS && out[key] === CONFIG_DEFAULTS[key])) delete out[key];
    }
    return out;
  }

  /** The media player of one Piston device from the frontend's entity registry (`hass.entities`), or null. */
  function resolveEntities(hass, deviceId) {
    const out = { mediaPlayer: null };
    const entities = (hass && hass.entities) || {};
    for (const id of Object.keys(entities)) {
      const e = entities[id];
      if (!e || e.device_id !== deviceId || e.platform !== DOMAIN) continue;
      const entityId = e.entity_id || id;
      if (entityId.startsWith("media_player.")) { out.mediaPlayer = entityId; break; }
    }
    return out;
  }

  /** The status a failed subscribe means (an error object from Home Assistant's websocket), or null for "try again". */
  function statusFromError(error) {
    const code = error && error.code;
    if (code === "unauthorized") return "unauthorized";
    if (code === "not_found") return "not_found";
    return null;
  }

  /** The status subscription with its lifecycle: start/stop, no retry for answers that will not change, backoff otherwise.
   * `subscribe(deviceId, callback)` returns a promise of an unsubscribe function (Home Assistant resubscribes by itself after
   * a reconnect, so the backoff is only for the first attempt). Timers are injectable for tests. */
  class StatusFeed {
    constructor({ subscribe, onStatus, setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (handle) => clearTimeout(handle) }) {   // wrapped: a browser refuses clearTimeout called as a method
      this.subscribe = subscribe; this.onStatus = onStatus; this.setTimer = setTimer; this.clearTimer = clearTimer;
      this.deviceId = null; this.unsub = null; this.timer = null; this.attempt = 0; this.generation = 0;
    }
    start(deviceId) {
      if (!deviceId) { this.stop(); return; }
      if (this.deviceId === deviceId && (this.unsub || this.timer || this.pending)) return;
      this.stop();
      this.deviceId = deviceId;
      this._try(++this.generation);
    }
    stop() {
      this.generation++;
      this.clearTimer(this.timer); this.timer = null; this.pending = false;
      const unsub = this.unsub; this.unsub = null; this.deviceId = null; this.attempt = 0;
      if (unsub) { try { unsub(); } catch (e) { /* the connection is already gone */ } }
    }
    _try(generation) {
      this.pending = true;
      Promise.resolve().then(() => this.subscribe(this.deviceId, (status) => { if (generation === this.generation) this.onStatus(status); }))
        .then((unsub) => {
          if (generation !== this.generation) { try { unsub(); } catch (e) { /* ignore */ } return; }
          this.pending = false; this.unsub = unsub; this.attempt = 0;
        }, (error) => {
          if (generation !== this.generation) return;
          this.pending = false;
          const status = statusFromError(error);
          if (status) { this.onStatus(status); return; }
          const delay = Math.min(30000, 1000 * 2 ** this.attempt++);
          this.timer = this.setTimer(() => { this.timer = null; if (generation === this.generation) this._try(generation); }, delay);
        });
    }
  }

  // ------------------------------------------------------------------------------------------------------------------
  // The card element
  // ------------------------------------------------------------------------------------------------------------------

  // Colors come from the Home Assistant theme (the documented CSS variables), with fallbacks for a dark look when a variable is
  // missing. Every control uses the same surface, so top and bottom match in any theme; surfaces are a tint of the text color over
  // whatever the card background is, which works in light and dark themes alike.
  const STYLES = `
    :host {
      display: block;
      --pr-text: var(--primary-text-color, #e6edf8);
      --pr-muted: var(--secondary-text-color, #8592a8);
      --pr-accent: var(--primary-color, #03a9f4);
      --pr-on-accent: var(--text-primary-color, #fff);
      --pr-danger: var(--error-color, #db4437);
      --pr-line: var(--divider-color, rgba(127,127,127,.35));
      --pr-radius: 16px;
      --pr-surface: rgba(127,127,127,.16);
      --pr-surface-pressed: rgba(127,127,127,.32);
      --pr-pad: rgba(127,127,127,.1);
      --pr-dot: rgba(127,127,127,.28);
    }
    @supports (color: color-mix(in srgb, red, blue)) {
      :host {
        --pr-surface: color-mix(in srgb, var(--pr-text) 9%, transparent);
        --pr-surface-pressed: color-mix(in srgb, var(--pr-accent) 30%, transparent);
        --pr-pad: color-mix(in srgb, var(--pr-text) 5%, transparent);
        --pr-dot: color-mix(in srgb, var(--pr-text) 18%, transparent);
      }
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    [hidden] { display: none !important; }
    svg { display: block; fill: none; stroke: currentColor; stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; }
    button, select { font: inherit; color: var(--pr-text); touch-action: manipulation; -webkit-user-select: none; user-select: none; }
    #root { display: flex; flex-direction: column; padding: 12px; gap: 10px; color: var(--pr-text); }

    /* every control shares this surface */
    .ctl { border: 1px solid var(--pr-line); background: var(--pr-surface); color: var(--pr-text); cursor: pointer;
      transition: transform .08s ease, background .12s ease, border-color .12s ease; }
    .ctl.down { background: var(--pr-surface-pressed); border-color: var(--pr-accent); transform: scale(.97); }
    .ctl:disabled, .ctl.off { opacity: .4; pointer-events: none; }

    #bar { display: flex; align-items: center; gap: 8px; }
    #power { flex: none; display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 14px; border-radius: 14px;
      font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    #power ha-icon { --mdc-icon-size: 22px; }
    #apps { flex: 1; min-width: 0; height: 44px; padding: 0 10px; border-radius: 14px; font-size: 14px; }

    #pad { position: relative; height: var(--pr-pad-height, 34vh); min-height: var(--pr-pad-min, 220px); max-height: var(--pr-pad-max, 380px); touch-action: none; user-select: none; -webkit-user-select: none;
      display: grid; place-items: center; text-align: center; border-radius: 24px; overflow: hidden;
      border: 1px solid var(--pr-line);
      background: radial-gradient(var(--pr-dot) 1px, transparent 1.3px) 0 0 / 20px 20px, var(--pr-pad); }
    #pad.disabled::before { content: ""; position: absolute; inset: 0; background: color-mix(in srgb, var(--card-background-color, #1c1c1c) 55%, transparent); pointer-events: none; z-index: 1; }
    #hint { padding: 0 14px; display: grid; gap: 12px; transition: opacity .6s; pointer-events: none; color: var(--pr-muted); font-size: 13px; }
    #hint div { display: flex; align-items: center; gap: 10px; justify-content: center; }
    #hint ha-icon { --mdc-icon-size: 20px; color: var(--pr-muted); flex: none; }
    #hint b { color: var(--pr-text); font-weight: 600; }
    #pad.disabled #hint { opacity: 0; }
    #badge { position: absolute; z-index: 2; bottom: 10px; left: 10px; display: none; align-items: center; gap: 8px; width: fit-content; max-width: calc(100% - 20px);
      padding: 6px 12px; border-radius: 999px; background: var(--card-background-color, #1c1c1c); border: 1px solid var(--pr-line);
      font-size: 12px; font-weight: 600; letter-spacing: .02em; color: var(--pr-danger); pointer-events: none; }
    #badge.show { display: flex; }
    #badge.info { color: var(--pr-text); }
    #badge i { width: 8px; height: 8px; border-radius: 50%; background: var(--pr-danger); flex: none; }
    #badge.info i { background: var(--pr-accent); }
    #ripple { position: absolute; width: 60px; height: 60px; margin: -30px 0 0 -30px; border-radius: 50%;
      border: 2px solid var(--pr-accent); background: color-mix(in srgb, var(--pr-accent) 15%, transparent); pointer-events: none; opacity: 0; }
    #ripple.go { animation: rp .38s ease-out; }
    @keyframes rp { from { opacity: .95; transform: scale(.4); } to { opacity: 0; transform: scale(1.25); } }
    #toast { position: absolute; z-index: 6; left: 50%; bottom: 54px; transform: translateX(-50%); max-width: 82%;
      padding: 8px 16px; border-radius: 999px; background: var(--card-background-color, #1c1c1c); border: 1px solid var(--pr-line); color: var(--pr-text);
      font-size: 13px; font-weight: 600; opacity: 0; transition: opacity .25s; pointer-events: none; box-shadow: 0 4px 14px rgba(0,0,0,.3); }
    #toast.show { opacity: 1; }

    #vol { display: flex; height: 46px; border-radius: var(--pr-radius); overflow: hidden; border: 1px solid var(--pr-line); background: var(--pr-surface); }
    #vol.off { opacity: .4; pointer-events: none; }
    #vol button { flex: 1; display: grid; place-items: center; border: 0; background: transparent; cursor: pointer; transition: background .1s ease; }
    #vol button + button { border-left: 1px solid var(--pr-line); }
    #vol button ha-icon { --mdc-icon-size: 26px; color: var(--pr-text); }
    #vol button.down { background: var(--pr-surface-pressed); }

    #volLevel { display: flex; align-items: center; gap: 10px; }
    #volLevel.off { opacity: .4; pointer-events: none; }
    #volMute { flex: none; width: 46px; height: 46px; display: grid; place-items: center; padding: 0; border-radius: var(--pr-radius); }
    #volMute ha-icon { --mdc-icon-size: 26px; }
    #volRange { flex: 1; min-width: 0; height: 46px; margin: 0; padding: 0 14px; border-radius: var(--pr-radius); accent-color: var(--pr-accent); touch-action: pan-y; }
    #volText { flex: none; min-width: 3.2em; text-align: right; font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--pr-muted); }

    /* dock: Steam and Quick Access on the left, the arrow wheel in the middle, Back and the keyboard on the right */
    #dock { display: grid; justify-content: center; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); column-gap: 12px; align-items: stretch; --dpad: min(210px, 42vw); }
    .zone { display: flex; flex-direction: column; gap: 8px; height: var(--dpad); min-width: 0; }
    .tile { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; padding: 0 8px; border-radius: var(--pr-radius);
      font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    .ctl ha-icon { --mdc-icon-size: 26px; color: var(--pr-accent); flex: none; }
    .kb ha-icon { --mdc-icon-size: 40px; }
    .kb.on { border-color: var(--pr-accent); box-shadow: inset 0 0 0 1px var(--pr-accent); }

    .dpad { position: relative; width: var(--dpad); height: var(--dpad); border-radius: 50%; overflow: hidden; touch-action: none;
      border: 1px solid var(--pr-line); background: var(--pr-surface); }
    .dpad.off { opacity: .4; pointer-events: none; }
    .spin { position: absolute; inset: 0; border-radius: 50%; pointer-events: none; opacity: 0; transition: opacity .18s ease;
      background: conic-gradient(from calc(var(--a, 0deg) - 55deg), transparent 0deg, var(--pr-accent) 55deg, transparent 110deg);
      -webkit-mask: radial-gradient(circle, transparent 62%, #000 64%, #000 98%, transparent 100%);
      mask: radial-gradient(circle, transparent 62%, #000 64%, #000 98%, transparent 100%); }
    .dpad.spinning .spin { opacity: .9; }
    .wedge { position: absolute; inset: 0; border: 0; padding: 0; background: transparent; cursor: pointer; color: var(--pr-text); transition: background .1s ease; }
    .wedge ha-icon { position: absolute; --mdc-icon-size: 34px; pointer-events: none; color: var(--pr-text); }
    .wedge.up { clip-path: polygon(50% 50%, 0 0, 100% 0); }
    .wedge.right { clip-path: polygon(50% 50%, 100% 0, 100% 100%); }
    .wedge.down { clip-path: polygon(50% 50%, 100% 100%, 0 100%); }
    .wedge.left { clip-path: polygon(50% 50%, 0 100%, 0 0); }
    .wedge.up ha-icon { top: 5%; left: 50%; margin-left: -17px; }
    .wedge.down ha-icon { bottom: 5%; left: 50%; margin-left: -17px; }
    .wedge.left ha-icon { left: 5%; top: 50%; margin-top: -17px; }
    .wedge.right ha-icon { right: 5%; top: 50%; margin-top: -17px; }
    .wedge.pressed { background: var(--pr-surface-pressed); }
    .cross { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; stroke: var(--pr-line); stroke-width: 1.5; }
    .hub { position: absolute; left: 50%; top: 50%; width: 38%; height: 38%; margin: -19% 0 0 -19%; padding: 0; border-radius: 50%; cursor: pointer;
      display: grid; place-items: center; color: var(--pr-on-accent); background: var(--pr-accent); border: 3px solid var(--card-background-color, transparent);
      transition: transform .08s ease, filter .08s ease; }
    .hub ha-icon { --mdc-icon-size: 30px; color: var(--pr-on-accent); }
    .hub.down { transform: scale(.93); filter: brightness(1.15); }
    #kb { position: absolute; left: 0; bottom: 0; width: 1px; height: 1px; opacity: 0; border: 0; padding: 0; resize: none; font-size: 16px; }

    #overlay { position: absolute; inset: 0; z-index: 20; display: none; place-items: center; background: color-mix(in srgb, var(--card-background-color, #000) 70%, transparent); padding: 16px; }
    #overlay.show { display: grid; }
    #dialog { width: min(340px, 100%); border-radius: 20px; padding: 18px; background: var(--card-background-color, #1c1c1c); color: var(--pr-text); border: 1px solid var(--pr-line);
      box-shadow: 0 16px 40px rgba(0,0,0,.4); display: grid; gap: 12px; }
    #dialog h3 { margin: 0; font-size: 17px; font-weight: 600; }
    #dialog p { margin: 0; font-size: 14px; color: var(--pr-muted); line-height: 1.4; }
    #dialog .row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 4px; }
    #dialog .row button { min-height: 44px; padding: 0 16px; border-radius: 12px; font-size: 14px; font-weight: 600; }
    #dialog .row .danger { background: var(--pr-danger); border-color: transparent; color: #fff; }

    /* landscape phones: the pad gets the height that is left, and the wheel and tiles shrink */
    @media (orientation: landscape) and (max-height: 560px) {
      #pad { height: var(--pr-pad-height, 46vh); min-height: 120px; }
      #dock { --dpad: min(160px, 34vh); }
    }
    @media (max-width: 360px) { .tile { gap: 4px; padding: 0 4px; font-size: 11px; letter-spacing: .05em; } .kb ha-icon { --mdc-icon-size: 34px; } #power { padding: 0 10px; } }
    @media (prefers-reduced-motion: reduce) { #ripple.go { animation-duration: .01s; } .ctl, .hub { transition: none; } }
  `;

  const HINT = `
    <div><ha-icon icon="mdi:cursor-move"></ha-icon><span><b>Drag</b> to move</span></div>
    <div><ha-icon icon="mdi:gesture-tap"></ha-icon><span><b>Tap</b> to click &middot; two fingers to right-click</span></div>
    <div><ha-icon icon="mdi:gesture-swipe-vertical"></ha-icon><span><b>Two fingers</b> to scroll or flick</span></div>
    <div><ha-icon icon="mdi:rotate-right"></ha-icon><span><b>Spin the wheel</b> to scroll</span></div>`;
  const TEMPLATE = `
    <ha-card>
      <div id="root">
        <div id="bar">
          <button class="ctl" id="power" aria-label="Power"><ha-icon icon="mdi:power"></ha-icon><span id="powerText">Turn on</span></button>
          <select class="ctl" id="apps" aria-label="App"></select>
        </div>
        <div id="pad"><div id="hint">${HINT}</div><div id="badge" role="status"><i></i><span id="badgeText"></span></div><div id="ripple"></div><div id="toast"></div></div>
        <div id="volLevel" hidden>
          <button class="ctl" id="volMute" aria-label="Mute"><ha-icon id="volMuteIcon" icon="mdi:volume-high"></ha-icon></button>
          <input class="ctl" id="volRange" type="range" min="0" max="100" step="1" value="0" aria-label="Volume">
          <span id="volText">0%</span>
        </div>
        <div id="vol">
          <button data-vol="down" aria-label="Volume down"><ha-icon icon="mdi:volume-minus"></ha-icon></button>
          <button data-vol="mute" aria-label="Mute"><ha-icon icon="mdi:volume-off"></ha-icon></button>
          <button data-vol="up" aria-label="Volume up"><ha-icon icon="mdi:volume-plus"></ha-icon></button>
        </div>
        <div id="dock">
          <div class="zone left">
            <button class="ctl tile" data-act="steam" aria-label="Steam menu"><ha-icon icon="mdi:steam"></ha-icon>Steam</button>
            <button class="ctl tile" data-act="qam" aria-label="Quick Access menu"><ha-icon icon="mdi:tune-variant"></ha-icon>Quick</button>
          </div>
          <div class="dpad" id="wheel">
            <button class="wedge up" data-key="Up" aria-label="Up"><ha-icon icon="mdi:chevron-up"></ha-icon></button>
            <button class="wedge right" data-key="Right" aria-label="Right"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
            <button class="wedge down" data-key="Down" aria-label="Down"><ha-icon icon="mdi:chevron-down"></ha-icon></button>
            <button class="wedge left" data-key="Left" aria-label="Left"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
            <div class="spin"></div>
            <svg class="cross" viewBox="0 0 100 100" preserveAspectRatio="none"><path d="M0 0L100 100M100 0L0 100"/></svg>
            <button class="hub" id="hub" aria-label="Enter"><ha-icon icon="mdi:keyboard-return"></ha-icon></button>
          </div>
          <div class="zone right">
            <button class="ctl tile" id="esc" aria-label="Back"><ha-icon icon="mdi:arrow-left"></ha-icon>Back</button>
            <button class="ctl tile kb" id="kbBtn" aria-label="Keyboard"><ha-icon icon="mdi:keyboard-outline"></ha-icon></button>
          </div>
        </div>
        <textarea id="kb" autocapitalize="sentences" autocomplete="off" autocorrect="on" spellcheck="true" aria-label="keyboard input"></textarea>
      </div>
      <div id="overlay"><div id="dialog" role="dialog" aria-modal="true"></div></div>
    </ha-card>`;

  if (typeof HTMLElement !== "undefined") {
    const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
    const buzz = (ms = 8) => { try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* not on iOS */ } };

    class PistonRemoteCard extends HTMLElement {
      constructor() {
        super();
        this._config = null; this._hass = null; this._status = "connecting"; this._ents = {};
        this._outbox = new Outbox(1); this._scroll = new ScrollAccumulator(); this._gesture = new PadGesture();
        this._raf = 0; this._inertia = null; this._inertiaRaf = 0; this._built = false;
        this._wheel = new WheelGesture(); this._wheelHold = 0; this._wheelRep = 0; this._wheelEl = null;
        this._text = new TextSync(); this._composing = false; this._toastTimer = 0;
        this._volHold = 0; this._volRep = 0; this._volBtn = null;
        this._sender = new VolumeSender(); this._volTimer = 0; this._volDragging = false;
        this._appsKey = null; this._dialogOpen = false; this._entsRef = null; this._mpRef = UNSET;
        this._onVisibility = () => { if (document.visibilityState !== "visible") this._stopAll(); };
        this._feed = new StatusFeed({
          subscribe: (deviceId, cb) => this._hass.connection.subscribeMessage(
            (msg) => cb(msg && msg.status), { type: SUBSCRIBE, device_id: deviceId }, { resubscribe: true }),
          onStatus: (status) => { this._status = status; this._update(); },
        });
      }
      static getConfigElement() { return document.createElement("piston-remote-card-editor"); }
      static getStubConfig(hass) {
        const entities = (hass && hass.entities) || {};
        const first = Object.values(entities).find((e) => e && e.platform === DOMAIN && e.device_id);
        return { device_id: first ? first.device_id : "" };
      }
      setConfig(config) {
        if (!config || typeof config !== "object") throw new Error("Invalid configuration");
        this._config = { speed: 1, show_power: true, show_apps: true, show_volume: true, show_keyboard: true, ...config };
        this._outbox = new Outbox(this._config.speed);
        this._entsRef = null; this._mpRef = UNSET;
        this._ents = resolveEntities(this._hass, this._config.device_id);
        this._attachFeed();
        this._update();
      }
      getCardSize() { return 8; }
      /** Home Assistant sets `hass` on every state change anywhere, many times a second: keep the object current but only
       * re-resolve and re-render when what the card shows (the entity registry or its media player) is a different object. */
      set hass(hass) {
        this._hass = hass;
        const entities = hass && hass.entities;
        if (entities !== this._entsRef) {
          this._entsRef = entities;
          this._ents = resolveEntities(hass, this._config && this._config.device_id);
          this._mpRef = UNSET;                           // the media player may be a different entity now
        }
        const id = this._ents.mediaPlayer;
        const mp = id && hass && hass.states ? hass.states[id] : undefined;
        if (mp === this._mpRef) { this._attachFeed(); return; }
        this._mpRef = mp;
        this._attachFeed();
        this._update();
      }
      connectedCallback() { document.addEventListener("visibilitychange", this._onVisibility); this._attachFeed(); this._update(); }
      disconnectedCallback() { document.removeEventListener("visibilitychange", this._onVisibility); this._stopAll(); this._feed.stop(); }

      _attachFeed() {
        if (!this.isConnected || !this._hass || !this._config || this.preview) return;
        if (!this._config.device_id) { this._feed.stop(); return; }
        this._feed.start(this._config.device_id);
      }
      get _mpState() {
        const id = this._ents.mediaPlayer;
        const state = id && this._hass && this._hass.states && this._hass.states[id];
        return state ? state.state : undefined;
      }
      get state() {
        const status = this._config && this._config.device_id ? this._status : "no_device";
        return deriveState({ status, mpState: this._mpState, preview: !!this.preview });
      }

      // --- sending ---------------------------------------------------------------------------------------------
      /** Send an event now (with any pending move before it); moves alone are summed and sent once per animation frame. */
      _queue(event) { this._outbox.add(event); this._flush(); }   // clicks, keys, scrolls go out at once; only moves wait for the frame
      _queueMove(dx, dy) { this._outbox.addMove(dx, dy); this._schedule(); }
      _schedule() { if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = 0; this._flush(); }); }
      _flush() {
        const events = this._outbox.take();
        for (let i = 0; i < events.length; i += 64) this.send(events.slice(i, i + 64));
      }
      send(events) {
        const connection = this._hass && this._hass.connection;
        if (!events.length || this.preview || !connection || typeof connection.sendMessage !== "function" || !this._config || !this._config.device_id) return;
        try { connection.sendMessage({ type: SEND, device_id: this._config.device_id, events }); } catch (e) { /* the socket is closing: the status feed tells the user */ }
      }
      /** Everything in flight stops: the card was hidden, removed or the app went to the background. */
      _stopAll() {
        if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; }
        this._outbox.take();
        this._stopInertia();
        this._gesture.cancel();
        this._scroll.reset();
        this._wheelDone();
        this._stopVolume();
        clearTimeout(this._volTimer); this._volTimer = 0; this._volDragging = false;
      }

      // --- building and updating the DOM -----------------------------------------------------------------------
      _build() {
        if (this._built) return;
        this._built = true;
        if (!this.shadowRoot) this.attachShadow({ mode: "open" });
        this.shadowRoot.innerHTML = `<style>${STYLES}</style>${TEMPLATE}`;
        const $ = (sel) => this.shadowRoot.querySelector(sel);
        this._el = { pad: $("#pad"), hint: $("#hint"), badge: $("#badge"), badgeText: $("#badgeText"), ripple: $("#ripple"), toast: $("#toast"), wheel: $("#wheel"),
          hub: $("#hub"), esc: $("#esc"), kbBtn: $("#kbBtn"), kb: $("#kb"), bar: $("#bar"), power: $("#power"), powerText: $("#powerText"),
          apps: $("#apps"), vol: $("#vol"), dock: $("#dock"), root: $("#root"), volLevel: $("#volLevel"), volMute: $("#volMute"), volMuteIcon: $("#volMuteIcon"), volRange: $("#volRange"), volText: $("#volText"), overlay: $("#overlay"), dialog: $("#dialog") };
        this._bindPad();
        this._bindDock();
        this._bindBar();
        this._bindVolume();
        this._bindLevel();
      }
      _update() {
        if (!this._config) return;
        this._build();
        const s = this.state, el = this._el;
        el.pad.classList.toggle("disabled", !s.input);
        el.badge.classList.toggle("show", !!s.banner);
        el.badge.classList.toggle("info", s.kind === "desktop" || s.kind === "preview");
        el.badgeText.textContent = s.banner || "";
        const cfg = this._config;
        const plan = powerPlan(this._mpState);
        el.bar.hidden = !(cfg.show_power || cfg.show_apps);
        el.power.hidden = !cfg.show_power;
        el.apps.hidden = !cfg.show_apps;
        const volume = volumeView(this._mpAttrs);
        el.vol.hidden = !cfg.show_volume || volume.mode !== "steps";
        el.volLevel.hidden = !cfg.show_volume || volume.mode !== "level";
        this._applyLayout(cfg);
        el.kbBtn.hidden = el.kb.hidden = !cfg.show_keyboard;
        el.power.disabled = !s.power || !plan.action || this._dialogOpen;
        el.powerText.textContent = plan.label;
        el.vol.classList.toggle("off", !s.volume);
        el.volLevel.classList.toggle("off", !s.volume);
        if (!this._volDragging) {                       // never fight the thumb while it is being dragged
          el.volRange.value = String(volume.percent);
          el.volText.textContent = volume.percent + "%";
        }
        el.volMuteIcon.setAttribute("icon", volume.muted ? "mdi:volume-off" : "mdi:volume-high");
        this._updateApps(s);
        for (const b of this.shadowRoot.querySelectorAll("[data-act]")) b.classList.toggle("off", !s.shell);
        for (const b of [el.esc, el.kbBtn]) b.classList.toggle("off", !s.input);
        el.wheel.classList.toggle("off", !s.input);
        if (!s.input) { this._stopInertia(); this._gesture.cancel(); this._wheelDone(); }
        if (!s.volume) this._stopVolume();
      }

      // --- power, apps, volume -----------------------------------------------------------------------------------
      /** Call a Home Assistant service; a failure shows its message on the card instead of failing silently. */
      _call(domain, service, data) {
        if (this.preview || !this._hass || typeof this._hass.callService !== "function") return;
        Promise.resolve().then(() => this._hass.callService(domain, service, data))
          .catch((e) => this._toast((e && e.message) || "That didn't work"));
      }
      _bindBar() {
        const el = this._el;
        el.power.addEventListener("click", () => {
          buzz();
          const plan = powerPlan(this._mpState);
          const entity = this._ents.mediaPlayer;
          if (!plan.action || !entity) return;
          if (plan.action === "wake") this._call("media_player", "turn_on", { entity_id: entity });
          else this._confirm({ title: "Turn off the device?", message: "It goes to sleep or shuts down, depending on the Piston option.",
            label: "Turn off", run: () => this._call("media_player", "turn_off", { entity_id: entity }) });
        });
        el.apps.addEventListener("change", () => {
          if (!this._ents.mediaPlayer || !el.apps.value) return;
          this._call("media_player", "select_source", { entity_id: this._ents.mediaPlayer, source: el.apps.value });
        });
        el.overlay.addEventListener("click", (e) => { if (e.target === el.overlay) this._closeDialog(); });
      }
      get _mpAttrs() {
        const id = this._ents.mediaPlayer;
        const state = id && this._hass && this._hass.states && this._hass.states[id];
        return state ? state.attributes : undefined;
      }
      _updateApps(s) {
        const el = this._el, view = appsView(this._mpAttrs);
        // rebuild the list only when it changed: replacing it would close the phone's open picker
        const key = view.sources.join("\n") + "|" + view.current;
        if (key !== this._appsKey) {
          this._appsKey = key;
          const option = (value, text, disabled) => { const o = document.createElement("option"); o.value = value; o.textContent = text; if (disabled) o.disabled = true; return o; };
          const options = view.sources.map((name) => option(name, name));
          if (view.current === null) options.unshift(option("", "Choose an app"));
          if (view.sources.length <= 1) options.push(option("", "Add apps in the Piston panel to list them here", true));
          el.apps.replaceChildren(...options);
          el.apps.value = view.current === null ? "" : view.current;
        }
        el.apps.disabled = !s.apps || view.sources.length === 0;
      }

      // the card's own confirmation dialog (an HA dialog is not a stable thing to depend on from a custom card)
      _confirm({ title, message, label, run }) {
        const node = (tag, text, cls, onClick) => {
          const n = document.createElement(tag);
          if (text) n.textContent = text;
          if (cls) n.className = cls;
          if (onClick) n.addEventListener("click", onClick);
          return n;
        };
        const row = node("div", "", "row");
        row.append(node("button", "Cancel", "ctl cancel", () => this._closeDialog()),
          node("button", label, "ctl danger confirm", () => { this._closeDialog(); run(); }));
        this._dialogOpen = true;
        this._el.dialog.replaceChildren(node("h3", title), node("p", message), row);
        this._el.overlay.classList.add("show");
        this._update();
      }
      _closeDialog() {
        this._dialogOpen = false;
        this._el.overlay.classList.remove("show");
        this._el.dialog.replaceChildren();
        this._update();
      }

      _bindVolume() {
        for (const b of this.shadowRoot.querySelectorAll("[data-vol]")) {
          const kind = b.dataset.vol;
          const fire = () => {
            const id = this._ents.mediaPlayer;
            if (!id || !this.state.volume) return;
            if (kind === "mute") {
              const attrs = this._mpAttrs || {};
              this._call("media_player", "volume_mute", { entity_id: id, is_volume_muted: !attrs.is_volume_muted });
            } else this._call("media_player", kind === "up" ? "volume_up" : "volume_down", { entity_id: id });
          };
          b.addEventListener("pointerdown", () => {
            buzz(); fire();
            this._stopVolume(); this._volBtn = b;
            b.classList.add("down");
            if (kind !== "mute") this._volHold = setTimeout(() => { this._volHold = 0; this._volRep = setInterval(fire, 180); }, 400);
          });
          for (const ev of ["pointerup", "pointercancel", "pointerleave"]) b.addEventListener(ev, () => this._stopVolume());
        }
      }
      _bindLevel() {
        const { volRange, volMute, volText } = this._el;
        const set = (percent) => {
          const id = this._ents.mediaPlayer;
          if (id && this.state.volume) this._call("media_player", "volume_set", { entity_id: id, volume_level: percent / 100 });
        };
        const value = () => Math.min(100, Math.max(0, Number(volRange.value) || 0));
        volRange.addEventListener("pointerdown", () => { this._volDragging = true; });
        volRange.addEventListener("input", () => {
          this._volDragging = true;
          volText.textContent = value() + "%";
          const now_ = now();
          const send = this._sender.push(value(), now_);
          if (send !== null) set(send);
          else if (!this._volTimer) this._volTimer = setTimeout(() => {
            this._volTimer = 0;
            const late = this._sender.flush(now());
            if (late !== null) set(late);
          }, VOLUME_INTERVAL_MS);
        });
        volRange.addEventListener("change", () => {      // released: the final position always goes out
          clearTimeout(this._volTimer); this._volTimer = 0;
          this._sender.flush(now());
          this._volDragging = false;
          set(value());
        });
        for (const ev of ["pointerup", "pointercancel"]) volRange.addEventListener(ev, () => { this._volDragging = false; });
        volMute.addEventListener("click", () => {
          buzz();
          const id = this._ents.mediaPlayer;
          if (!id || !this.state.volume) return;
          this._call("media_player", "volume_mute", { entity_id: id, is_volume_muted: !(this._mpAttrs || {}).is_volume_muted });
        });
      }
      _stopVolume() {
        clearTimeout(this._volHold); clearInterval(this._volRep); this._volHold = this._volRep = 0;
        if (this._volBtn) { this._volBtn.classList.remove("down"); this._volBtn = null; }
      }

      // --- the dock: tiles, the arrow wheel, typing -------------------------------------------------------------
      _bindDock() {
        const el = this._el, root = this.shadowRoot;
        const press = (b, fire) => b.addEventListener("pointerdown", () => { buzz(); fire(); });
        for (const b of root.querySelectorAll("[data-act]")) b.addEventListener("click", () => {
          if (!this.state.shell) return;
          buzz(); this._queue({ k: "action", a: b.dataset.act });
        });
        press(el.esc, () => { if (this.state.input) this._queue({ k: "key", key: "Escape" }); });
        press(el.hub, () => { if (this.state.input) this._queue({ k: "key", key: "Enter" }); });
        // pressed state as a class: reliable on iOS, where :active lags (the wedges handle their own)
        for (const b of [...root.querySelectorAll("button.ctl"), el.hub]) {
          const on = () => b.classList.add("down"), off = () => b.classList.remove("down");
          b.addEventListener("pointerdown", on);
          for (const ev of ["pointerup", "pointercancel", "pointerleave"]) b.addEventListener(ev, off);
        }
        // keep the keyboard open when pressing the other buttons
        for (const b of [...root.querySelectorAll(".tile"), el.hub, ...root.querySelectorAll(".wedge")]) {
          b.addEventListener("pointerdown", (e) => { if (this._kbOn && b !== el.kbBtn) e.preventDefault(); });
        }
        this._bindWheel();
        this._bindKeyboard();
      }
      _bindWheel() {
        const wheel = this._el.wheel;
        wheel.addEventListener("pointerdown", (e) => {
          const w = e.target.closest(".wedge");
          if (!w || this._wheel.active || !this.state.input) return;
          e.preventDefault(); this._stopInertia();
          wheel.setPointerCapture(e.pointerId);
          const r = wheel.getBoundingClientRect(), key = w.dataset.key;
          this._wheel.down({ id: e.pointerId, key, center: [r.left + r.width / 2, r.top + r.height / 2], point: [e.clientX, e.clientY], size: r.width });
          this._wheelEl = w; w.classList.add("pressed");
          this._wheelHold = setTimeout(() => {
            this._wheelHold = 0;
            this._applyWheel(this._wheel.hold());
            if (this._wheel.repeating) this._wheelRep = setInterval(() => this._applyWheel([{ type: "key", key }]), WHEEL_REPEAT_MS);
          }, WHEEL_HOLD_MS);
        });
        wheel.addEventListener("pointermove", (e) => this._applyWheel(this._wheel.move(e.pointerId, [e.clientX, e.clientY], now())));
        const up = (e) => { this._applyWheel(this._wheel.up(e.pointerId, e.type === "pointercancel", now())); this._wheelDone(); };
        wheel.addEventListener("pointerup", up);
        wheel.addEventListener("pointercancel", up);
      }
      _applyWheel(actions) {
        for (const a of actions) {
          if (a.type === "key") { buzz(); this._queue({ k: "key", key: a.key }); }
          else if (a.type === "spin_start") {
            clearTimeout(this._wheelHold); clearInterval(this._wheelRep); this._wheelHold = this._wheelRep = 0;
            if (this._wheelEl) this._wheelEl.classList.remove("pressed");
            this._el.wheel.classList.add("spinning"); this._el.hint.style.opacity = 0;
          } else if (a.type === "scroll") {
            this._el.wheel.style.setProperty("--a", (a.angle * 180 / Math.PI + 90) + "deg");
            this._scrollBy(a.dy, 0);
          } else if (a.type === "flick") this._startInertia(a.vy, a.vx);
        }
      }
      _wheelDone() {
        clearTimeout(this._wheelHold); clearInterval(this._wheelRep); this._wheelHold = this._wheelRep = 0;
        this._wheel.cancel();
        if (this._wheelEl) { this._wheelEl.classList.remove("pressed"); this._wheelEl = null; }
        if (this._el) this._el.wheel.classList.remove("spinning");
      }
      _bindKeyboard() {
        const { kb, kbBtn } = this._el;
        kb.value = SENTINEL;
        kbBtn.addEventListener("click", () => { if (!this.state.input) return; this._kbOn ? kb.blur() : kb.focus(); });
        kb.addEventListener("focus", () => { this._kbOn = true; kbBtn.classList.add("on"); });
        kb.addEventListener("blur", () => { this._kbOn = false; kbBtn.classList.remove("on"); });
        const sync = () => {
          if (!this.state.input) return;
          const r = this._text.diff(kb.value, this._composing);
          for (const ev of r.events) this._queue(ev);
          if (r.dropped) this._toast("Some characters can't be typed on the device");
          if (r.value !== null) kb.value = r.value;
        };
        kb.addEventListener("compositionstart", () => { this._composing = true; });
        kb.addEventListener("compositionend", () => { this._composing = false; sync(); });
        kb.addEventListener("input", (e) => { if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") return; sync(); });
        kb.addEventListener("beforeinput", (e) => {
          if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") {
            e.preventDefault();
            if (this.state.input) this._queue({ k: "key", key: "Enter" });
          }
        });
        kb.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); if (this.state.input) this._queue({ k: "key", key: "Enter" }); }
        });
      }
      _toast(text) {
        const t = this._el.toast;
        t.textContent = text; t.classList.add("show");
        clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(() => t.classList.remove("show"), 2200);
      }

      /** The layout settings: the touchpad's height and the extra empty space above and below the volume control. */
      _applyLayout(cfg) {
        const layout = layoutFrom(cfg), el = this._el, host = el.root.style;
        if (layout.padHeight === null) { host.removeProperty("--pr-pad-height"); host.removeProperty("--pr-pad-min"); host.removeProperty("--pr-pad-max"); }
        else { host.setProperty("--pr-pad-height", layout.padHeight + "vh"); host.setProperty("--pr-pad-min", "120px"); host.setProperty("--pr-pad-max", "none"); }
        const volumeShown = !!cfg.show_volume;
        const first = volumeShown ? el.vol.hidden ? el.volLevel : el.vol : el.dock;           // the block right under the touchpad
        for (const block of [el.vol, el.volLevel, el.dock]) block.style.marginTop = "0px";
        first.style.marginTop = layout.gapPadVolume + "px";
        if (volumeShown) el.dock.style.marginTop = layout.gapVolumeButtons + "px";
      }

      // --- the touchpad ----------------------------------------------------------------------------------------
      _bindPad() {
        const pad = this._el.pad;
        const points = (e) => Array.from(e.touches, (p) => [p.clientX, p.clientY]);
        const opts = { passive: false };
        pad.addEventListener("touchstart", (e) => {
          e.preventDefault();
          if (!this.state.input) return;
          this._stopInertia();
          this._el.hint.style.opacity = 0;
          this._gesture.start(points(e), now());
        }, opts);
        pad.addEventListener("touchmove", (e) => {
          e.preventDefault();
          if (!this.state.input) return;
          this._apply(this._gesture.move(points(e), now()));
        }, opts);
        pad.addEventListener("touchend", (e) => {
          e.preventDefault();
          if (!this._gesture.active) return;
          const p = e.changedTouches[0];
          this._apply(this._gesture.end(e.touches.length, now(), p ? [p.clientX, p.clientY] : null));
        }, opts);
        pad.addEventListener("touchcancel", () => this._gesture.cancel());
      }
      _apply(actions) {
        for (const a of actions) {
          if (a.type === "move") this._queueMove(a.dx, a.dy);
          else if (a.type === "scroll") this._scrollBy(a.dy, a.dx);
          else if (a.type === "click") { this._ping(a.x, a.y); this._queue({ k: "click", b: a.button }); }
          else if (a.type === "flick") this._startInertia(a.vy, a.vx);
        }
      }
      _scrollBy(dy, dx) {
        const event = this._scroll.add(dy, dx);
        if (event) { this._queue(event); buzz(3); }
      }
      _ping(x, y) {
        const r = this._el.pad.getBoundingClientRect(), ripple = this._el.ripple;
        ripple.style.left = (x - r.left) + "px"; ripple.style.top = (y - r.top) + "px";
        ripple.classList.remove("go"); void ripple.offsetWidth; ripple.classList.add("go");
      }
      _startInertia(vy, vx) {
        this._inertia = new Inertia(vy, vx);
        let last = now();
        const tick = (t) => {
          if (!this._inertia) return;
          const dt = now() - last; last = now();
          const step = this._inertia.step(dt);
          this._scrollBy(step.dy, step.dx);
          if (step.done) this._inertia = null; else this._inertiaRaf = requestAnimationFrame(tick);
        };
        this._inertiaRaf = requestAnimationFrame(tick);
      }
      _stopInertia() {
        if (this._inertiaRaf) cancelAnimationFrame(this._inertiaRaf);
        this._inertiaRaf = 0; this._inertia = null; this._scroll.reset();
      }
    }

    /** The visual editor shown in the dashboard's card picker. */
    class PistonRemoteCardEditor extends HTMLElement {
      constructor() { super(); this._config = {}; this._hass = null; this._form = null; }
      setConfig(config) { this._config = config || {}; this._render(); }
      set hass(hass) { this._hass = hass; if (this._form) this._form.hass = hass; }
      _render() {
        if (!this.shadowRoot) this.attachShadow({ mode: "open" });
        if (!this._form) {
          this.shadowRoot.innerHTML = "<ha-form></ha-form>";
          this._form = this.shadowRoot.querySelector("ha-form");
          this._form.schema = editorSchema();
          this._form.computeLabel = (schema) => EDITOR_LABELS[schema.name] || schema.name;
          this._form.addEventListener("value-changed", (e) => {
            this._config = editorConfig(this._config, e.detail && e.detail.value);
            this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config }, bubbles: true, composed: true }));
          });
          if (this._hass) this._form.hass = this._hass;
        }
        this._form.data = editorData(this._config);
      }
    }
    if (!customElements.get("piston-remote-card-editor")) customElements.define("piston-remote-card-editor", PistonRemoteCardEditor);
    if (!customElements.get("piston-remote-card")) customElements.define("piston-remote-card", PistonRemoteCard);
    window.customCards = window.customCards || [];
    if (!window.customCards.some((c) => c.type === "piston-remote-card")) {
      window.customCards.push({ type: "piston-remote-card", name: "Piston remote", description: "Touchpad, buttons, power and apps for a SteamOS device", preview: false });
    }
  }

  const api = { layoutFrom, CONFIG_DEFAULTS, volumeView, VolumeSender, editorSchema, editorData, editorConfig, EDITOR_LABELS, powerPlan, appsView, HOME_SOURCE, deriveState, MoveBatcher, Outbox, ScrollAccumulator, Inertia, PadGesture, WheelGesture, TextSync, flickFrom, resolveEntities, statusFromError, StatusFeed, clampSpeed };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
