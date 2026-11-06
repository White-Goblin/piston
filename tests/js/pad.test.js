import test from "node:test";
import assert from "node:assert/strict";
import { loadCard } from "./load.js";

const { Outbox, ScrollAccumulator, Inertia, PadGesture } = loadCard();

const run = (gesture, steps) => {
  const out = [];
  for (const [kind, ...args] of steps) out.push(...gesture[kind](...args));
  return out;
};

test("a short tap is a left click at the finger's position", () => {
  const g = new PadGesture();
  const out = run(g, [["start", [[10, 20]], 0], ["end", 0, 120, [10, 20]]]);
  assert.deepEqual(out, [{ type: "click", button: "l", x: 10, y: 20 }]);
  assert.equal(g.active, false);
});

test("a two-finger tap is a right click, and only when the last finger lifts", () => {
  const g = new PadGesture();
  g.start([[10, 20]], 0);
  g.start([[10, 20], [60, 22]], 10);
  assert.deepEqual(g.end(1, 100, [60, 22]), []);               // one finger still down
  assert.deepEqual(g.end(0, 110, [10, 20]), [{ type: "click", button: "r", x: 10, y: 20 }]);
});

test("a long press is not a click", () => {
  const g = new PadGesture();
  assert.deepEqual(run(g, [["start", [[10, 20]], 0], ["end", 0, 400, [10, 20]]]), []);
});

test("a tap's tiny jitter is not a drag, a real move is", () => {
  const g = new PadGesture();
  g.start([[10, 20]], 0);
  assert.deepEqual(g.move([[11, 20]], 20), []);                // under 2 px within 200 ms: ignored
  assert.deepEqual(g.move([[14, 22]], 40), [{ type: "move", dx: 4, dy: 2 }]);
  assert.deepEqual(g.move([[15, 22]], 60), [{ type: "move", dx: 1, dy: 0 }]);   // once dragging, every move counts
  assert.deepEqual(g.end(0, 80, [15, 22]), []);                // a drag never ends in a click
});

test("a slow small move after 200 ms is a drag", () => {
  const g = new PadGesture();
  g.start([[10, 20]], 0);
  assert.deepEqual(g.move([[11, 20]], 250), [{ type: "move", dx: 1, dy: 0 }]);
});

test("two fingers scroll by the average movement", () => {
  const g = new PadGesture();
  g.start([[10, 100], [50, 100]], 0);
  assert.deepEqual(g.move([[10, 110], [50, 112]], 16), [{ type: "scroll", dy: 11, dx: 0 }]);
  assert.deepEqual(g.move([[14, 110], [54, 112]], 32), [{ type: "scroll", dy: 0, dx: 4 }]);
});

test("adding a second finger turns a move into a scroll gesture", () => {
  const g = new PadGesture();
  g.start([[10, 20]], 0);
  g.move([[30, 20]], 300);
  g.start([[30, 20], [80, 20]], 310);                          // the second finger lands: no longer a one-finger drag
  assert.deepEqual(g.move([[30, 30], [80, 30]], 330), [{ type: "scroll", dy: 10, dx: 0 }]);
});

test("a quick two-finger swipe ends with a flick", () => {
  const g = new PadGesture();
  g.start([[10, 100], [50, 100]], 0);
  let t = 0;
  for (let i = 1; i <= 6; i++) { t += 16; g.move([[10, 100 + 12 * i], [50, 100 + 12 * i]], t); }
  const out = g.end(0, t + 4, [10, 172]);
  assert.equal(out.length, 1);
  assert.equal(out[0].type, "flick");
  assert.ok(out[0].vy > 0.25 && out[0].vy <= 5);
});

test("a slow two-finger drag does not flick, a velocity is capped", () => {
  const slow = new PadGesture();
  slow.start([[0, 0], [40, 0]], 0);
  for (let i = 1; i <= 6; i++) slow.move([[0, i], [40, i]], i * 16);
  assert.deepEqual(slow.end(0, 100, [0, 6]), []);
  const wild = new PadGesture();
  wild.start([[0, 0], [40, 0]], 0);
  for (let i = 1; i <= 4; i++) wild.move([[0, i * 400], [40, i * 400]], i * 16);
  assert.equal(wild.end(0, 70, [0, 1600])[0].vy, 5);
});

test("cancel forgets the gesture", () => {
  const g = new PadGesture();
  g.start([[1, 1]], 0);
  g.cancel();
  assert.equal(g.active, false);
  assert.deepEqual(g.move([[9, 9]], 10), []);
  assert.deepEqual(g.end(0, 20, null), []);
});

test("scroll steps: one notch per 14 px with the remainder carried", () => {
  const a = new ScrollAccumulator();
  assert.equal(a.add(10, 0), null);
  assert.deepEqual(a.add(10, 0), { k: "scroll", dx: 0, dy: 1 });            // 20 px: one notch, 6 left
  assert.equal(a.add(7, 0), null);                                            // 13
  assert.deepEqual(a.add(1, 0), { k: "scroll", dx: 0, dy: 1 });
  assert.deepEqual(a.add(-30, 15), { k: "scroll", dx: 1, dy: -2 });
  a.add(10, 10);
  a.reset();
  assert.equal(a.add(10, 10), null);
});

test("inertia slows down exponentially and finishes", () => {
  const i = new Inertia(2, 0);
  const first = i.step(16);
  assert.ok(first.dy > 0 && first.dy < 2 * 16);
  assert.equal(first.done, false);
  let steps = 0, last = first.dy;
  for (; steps < 1000; steps++) { const s = i.step(16); assert.ok(s.dy <= last); last = s.dy; if (s.done) break; }
  assert.ok(steps > 10 && steps < 200);
  assert.equal(new Inertia(0.01, 0).step(16).done, true);
});

test("a long frame advances at most 32 ms", () => {
  assert.equal(new Inertia(1, 0).step(500).dy, new Inertia(1, 0).step(32).dy);
});

test("moves are summed per frame and a click is ordered after the moves before it", () => {
  const o = new Outbox(1);
  assert.equal(o.empty, true);
  o.addMove(1, 1); o.addMove(2, 0);
  o.add({ k: "click", b: "l" });
  o.addMove(5, 5); o.addMove(1, 0);
  assert.equal(o.empty, false);
  assert.deepEqual(o.take(), [{ k: "move", dx: 3, dy: 1 }, { k: "click", b: "l" }, { k: "move", dx: 6, dy: 5 }]);
  assert.equal(o.empty, true);
  assert.deepEqual(o.take(), []);
});

test("the speed multiplies the moves but not scrolls", () => {
  const o = new Outbox(2);
  o.addMove(1, 1);
  o.add({ k: "scroll", dx: 0, dy: 1 });
  assert.deepEqual(o.take(), [{ k: "move", dx: 2, dy: 2 }, { k: "scroll", dx: 0, dy: 1 }]);
});
