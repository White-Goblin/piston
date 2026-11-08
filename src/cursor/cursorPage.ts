// The cursor page is inlined (data: URL) so the overlay never depends on an HTTP load that could show a browser error
// page. It long-polls the plugin's loopback server for pointer positions (the route answers with CORS headers because a
// data: page has no origin of its own); the request simply fails and is retried while the plugin is not answering.
const PAGE = (feedUrl: string) => `<!doctype html><html><head><meta charset="utf-8"><title>cursor</title>
<style>html,body{margin:0;background:transparent;overflow:hidden}canvas{position:fixed;inset:0;width:100vw;height:100vh}</style>
</head><body><canvas id="c"></canvas><script>const FEED_URL=${JSON.stringify(feedUrl)};
(() => {
  const IDLE = 1500, TRAIL = 380, BADGE_MS = 1100, BADGE_OUT = 220, BADGE_BACK = 280, CIRCLE_D = 44;
  const cv = document.getElementById('c'), ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  function size() { cv.width = innerWidth * dpr; cv.height = innerHeight * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); }
  size(); addEventListener('resize', size);
  let trail = [], cur = null, last = 0, raf = 0, badge = null;
  function pill(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
  // A seek label: the cursor circle stretches away from the pointer (left for a seek back, right for forward), becomes a pill with
  // the seconds, and after a moment flows back into the circle. p runs 0 (the plain circle) to 1 (the label, off to the side).
  const ease = (t) => t * t * (3 - 2 * t);
  function badgeP(now) {
    const left = badge.until - now;
    if (left <= 0) { badge = null; return 0; }
    return ease(Math.min(1, (now - badge.start) / BADGE_OUT)) * ease(Math.min(1, left / BADGE_BACK));
  }
  function drawFlow(p, fade) {
    ctx.font = '700 24px system-ui, sans-serif'; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
    const full = Math.max(CIRCLE_D, ctx.measureText(badge.text).width + 28), w = CIRCLE_D + (full - CIRCLE_D) * p;
    const cx = Math.min(innerWidth - w / 2 - 8, Math.max(w / 2 + 8, cur.x + badge.dir * (full / 2 + 34) * p)), cy = cur.y;
    ctx.fillStyle = 'rgba(255,255,255,' + (0.32 + 0.6 * p) + ')';
    ctx.strokeStyle = 'rgba(90,165,255,.9)'; ctx.lineWidth = 2.5; ctx.lineCap = 'round';
    ctx.globalAlpha = fade;
    if (p > 0 && p < 1) {                       // the neck that stretches between the pointer and the label and thins as it leaves
      ctx.beginPath(); ctx.moveTo(cur.x, cur.y); ctx.lineTo(cx, cy);
      ctx.lineWidth = CIRCLE_D * (1 - p); ctx.strokeStyle = 'rgba(255,255,255,' + 0.3 * (1 - p) + ')'; ctx.stroke();
      ctx.lineWidth = 2.5; ctx.strokeStyle = 'rgba(90,165,255,.9)';
    }
    pill(cx - w / 2, cy - CIRCLE_D / 2, w, CIRCLE_D, CIRCLE_D / 2); ctx.fill(); ctx.stroke();
    ctx.globalAlpha = fade * p * p; ctx.fillStyle = '#10151d'; ctx.fillText(badge.text, cx, cy + 1);
    ctx.globalAlpha = 1; ctx.textAlign = 'start';
  }
  function frame(now) {
    raf = 0;
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    if (!cur || now - last > IDLE) return;
    trail = trail.filter((p) => now - p.t < TRAIL);
    ctx.lineCap = 'round';
    for (let i = 1; i < trail.length; i++) {
      const a = trail[i - 1], b = trail[i], life = 1 - (now - b.t) / TRAIL;
      ctx.strokeStyle = "rgba(120,185,255," + 0.45 * life + ")";
      ctx.lineWidth = Math.max(2, b.w * life);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    const fade = Math.min(1, (IDLE - (now - last)) / 400);
    const p = badge ? badgeP(now) : 0;
    if (badge) drawFlow(p, fade);                // the circle itself becomes the label and flows back
    else {
      ctx.globalAlpha = fade;
      ctx.beginPath(); ctx.arc(cur.x, cur.y, cur.d ? 17 : 22, 0, Math.PI * 2);
      ctx.fillStyle = cur.d ? 'rgba(255,255,255,.55)' : 'rgba(255,255,255,.32)'; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = 'rgba(90,165,255,.9)'; ctx.stroke();
      ctx.globalAlpha = 1;
    }
    raf = requestAnimationFrame(frame);
  }
  function show(m) {
    const now = performance.now();
    cur = m; last = now;
    trail.push({ x: m.x, y: m.y, t: now, w: Math.min(18, 4 + m.s * 0.6) });
    if (trail.length > 60) trail.shift();
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function showBadge(text, ts) {
    const left = BADGE_MS - (Date.now() - ts);   // the plugin runs on this machine: its wall clock is ours
    if (!(left > 0)) return;
    const now = performance.now(), dir = text[0] === '-' ? -1 : 1;
    // a label that follows another one in the same direction keeps flowing; a new direction or a new run starts from the circle
    badge = { text, dir, start: badge && badge.dir === dir ? badge.start : now, until: now + left };
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function hide() { cur = null; trail = []; badge = null; }
  // a backlog from before this page loaded: only the newest position, and the newest badge (which expires by itself)
  function newest(items) {
    let pos = null, tag = null;
    for (const it of items) { if (it.badge) tag = it; else pos = it; }
    return [pos, tag].filter(Boolean);
  }
  async function poll() {
    let after = 0, first = true;
    for (;;) {
      try {
        const r = await fetch(FEED_URL + "&after=" + after, { cache: "no-store" });
        if (!r.ok) throw new Error(String(r.status));
        const d = await r.json();
        if (d.reset) { after = 0; first = true; continue; }
        after = d.seq;
        // the first answer is a backlog of what happened before this page loaded: only its newest position is current
        const items = first ? newest(d.items) : d.items;
        first = false;
        for (const it of items) { if (it.stop) hide(); else if (it.badge) showBadge(it.badge, it.ts); else show(it); }
      } catch (e) {
        await new Promise((ok) => setTimeout(ok, 1000));
      }
    }
  }
  poll();
})();
</script></body></html>`;

export const cursorDataUrl = (feedUrl: string) => "data:text/html;charset=utf-8," + encodeURIComponent(PAGE(feedUrl));
