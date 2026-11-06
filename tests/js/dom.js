// A tiny fake DOM, just enough to run the card element without a browser: elements parsed from the card's template (by tag,
// id, class and data attributes), event listeners that tests can fire, and a manual animation-frame queue.
export class FakeElement {
  constructor(tag = "div", attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs; this.id = attrs.id || ""; this.listeners = {};
    this.children = []; this.style = { props: {}, setProperty(k, v) { this.props[k] = v; }, removeProperty(k) { delete this.props[k]; } }; this.dataset = {};
    for (const [k, v] of Object.entries(attrs)) if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
    const classes = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
    this.classList = { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); } };
    this.classes = classes; this.textContent = ""; this.hidden = false; this.value = ""; this.offsetWidth = 1;
    this._attrs = {};
  }
  append(...nodes) { for (const n of nodes) this.children.push(n); }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  set className(v) { this.classes.clear(); for (const c of String(v).split(/\s+/).filter(Boolean)) this.classes.add(c); }
  /** Every descendant, depth first (created elements, not the parsed template). */
  descendants() { return this.children.flatMap((c) => [c, ...(c.descendants ? c.descendants() : [])]); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  fire(type, event = {}) {
    const e = { preventDefault() { this.prevented = true; }, stopPropagation() {}, target: this, ...event };
    for (const fn of this.listeners[type] || []) fn(e);
    return e;
  }
  dispatchEvent(e) { (this.dispatched = this.dispatched || []).push(e); return true; }
  setAttribute(k, v) { this._attrs[k] = v; }
  getAttribute(k) { return this._attrs[k]; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 300, height: 300, right: 300, bottom: 300 }; }
  focus() { this.focused = true; } blur() { this.focused = false; }
  setPointerCapture() {} closest() { return this; }
  querySelector(sel) { return this._root ? this._root.find(sel, this)[0] || null : null; }
  querySelectorAll(sel) { return this._root ? this._root.find(sel, this) : []; }
}

class Root {
  constructor() { this.all = []; this._html = ""; }
  set innerHTML(html) {
    this._html = html; this.all = [];
    for (const m of html.matchAll(/<([a-z][\w-]*)([^>]*)>/gi)) {
      const attrs = {};
      for (const a of m[2].matchAll(/([\w-]+)="([^"]*)"/g)) attrs[a[1]] = a[2];
      if (/^(style|svg|path|circle|rect|g|span|b|i|div|button|ha-card|textarea|select|option|ha-form|input|label|ul|li|p|h\d)$/i.test(m[1]) || true) {
        const el = new FakeElement(m[1], attrs); el._root = this; this.all.push(el);
      }
    }
  }
  get innerHTML() { return this._html; }
  find(sel, scope) {
    const parts = sel.split(",").map((p) => p.trim());
    return this.all.filter((el) => parts.some((p) => match(el, p)));
  }
  querySelector(sel) { return this.find(sel)[0] || null; }
  querySelectorAll(sel) { return this.find(sel); }
}

function match(el, sel) {
  // supports: #id, .class, [data-x], [data-x="v"], tag, tag.class, :not(...) is not needed
  let m;
  if ((m = sel.match(/^#([\w-]+)$/))) return el.id === m[1];
  if ((m = sel.match(/^\.([\w-]+)$/))) return el.classes.has(m[1]);
  if ((m = sel.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/))) return m[1] in el.attrs && (m[2] === undefined || el.attrs[m[1]] === m[2]);
  if ((m = sel.match(/^([a-z][\w-]*)$/i))) return el.tagName === m[1].toUpperCase();
  if ((m = sel.match(/^([a-z][\w-]*)\.([\w-]+)$/i))) return el.tagName === m[1].toUpperCase() && el.classes.has(m[2]);
  return false;
}

export function installDom() {
  const defined = {}, frames = [], docListeners = {};
  class HTMLElement extends FakeElement {
    constructor() { super("custom"); this.isConnected = false; }
    attachShadow() { this.shadowRoot = new Root(); return this.shadowRoot; }
  }
  const document = {
    visibilityState: "visible",
    addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener(t, fn) { docListeners[t] = (docListeners[t] || []).filter((f) => f !== fn); },
    fire(t) { for (const fn of docListeners[t] || []) fn({}); },
    listenerCount: (t) => (docListeners[t] || []).length,
    createElement: (tag) => new FakeElement(tag), activeElement: null,
  };
  const g = globalThis;
  const saved = {};
  const set = (k, v) => { saved[k] = Object.getOwnPropertyDescriptor(g, k); Object.defineProperty(g, k, { value: v, configurable: true, writable: true }); };
  set("HTMLElement", HTMLElement);
  set("customElements", { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } });
  set("document", document);
  set("CustomEvent", class CustomEvent { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } });
  set("window", { customCards: [] });
  // fake timers: nothing runs until the test calls advance(ms)
  let clock = 0, nextTimer = 1;
  const timers = new Map();
  set("setTimeout", (fn, ms = 0) => { const id = nextTimer++; timers.set(id, { fn, at: clock + ms, every: 0 }); return id; });
  set("setInterval", (fn, ms = 0) => { const id = nextTimer++; timers.set(id, { fn, at: clock + ms, every: Math.max(1, ms) }); return id; });
  set("clearTimeout", (id) => { timers.delete(id); });
  set("clearInterval", (id) => { timers.delete(id); });
  set("requestAnimationFrame", (fn) => { frames.push(fn); return frames.length; });
  set("cancelAnimationFrame", (id) => { if (frames[id - 1]) frames[id - 1] = null; });
  return {
    defined, document, window: g.window, frames,
    /** Run the queued animation frames (once). */
    frame(t = 0) { const f = frames.splice(0); for (const fn of f) if (fn) fn(t); },
    timers,
    /** Move the fake clock forward, running the timers that come due in order. */
    advance(ms) {
      const end = clock + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [id, t] = due;
        clock = t.at;
        if (t.every) t.at += t.every; else timers.delete(id);
        t.fn();
      }
      clock = end;
    },
    uninstall() { for (const [k, d] of Object.entries(saved)) { if (d) Object.defineProperty(g, k, d); else delete g[k]; } },
  };
}
