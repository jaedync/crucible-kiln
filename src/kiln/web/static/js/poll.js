// Loads a REST view while someone can see it: the tab is visible and the panel is on screen or near it.
// Every load is a query on the machine that runs the engine, where CPU competes with the engine, so a hidden or off-screen panel asks
// for nothing; one that comes back into view loads at once if its data is older than its period.
// poke() asks for a load soon (say, after a request ended), never sooner than minGapMs after the last.

// Wrapped: a browser refuses its timer functions called as methods of another object (Illegal invocation).
const BROWSER = () => ({
  doc: document, now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (id) => clearTimeout(id),
  setInterval: (fn, ms) => setInterval(fn, ms),
  Observer: typeof IntersectionObserver === 'function' ? IntersectionObserver : null,
});

// pollWhileSeen(el, load, { periodMs, minGapMs, nearPx }) -> { poke(delayMs), now() }
export function pollWhileSeen(el, load, { periodMs, minGapMs = 0, nearPx = 400 } = {}, env = BROWSER()) {
  let last = -Infinity;
  let seen = true;
  let running = false;
  let stale = false;  // a load was asked for while hidden or running: the next chance takes it
  let soon = null;
  const visible = () => seen && !env.doc.hidden;

  async function run() {
    env.clearTimeout(soon);
    soon = null;
    if (!visible() && last !== -Infinity) {  // hidden since the load was asked for: when seen again
      stale = true;
      return;
    }
    if (running) {
      stale = true;
      return;
    }
    running = true;
    stale = false;
    last = env.now();
    try {
      await load();
    } catch (err) {
      console.error('kiln: a panel load failed', err);  // the panels show their own errors; keep polling
    } finally {
      running = false;
    }
    if (stale) check();
  }

  function check() {
    if (visible() && (stale || env.now() - last >= periodMs)) poke();
  }

  function poke(delayMs = 0) {
    if (!visible()) {
      stale = true;
      return;
    }
    if (soon !== null) return;
    soon = env.setTimeout(run, Math.max(0, delayMs, last + minGapMs - env.now()));
  }

  if (env.Observer) {
    new env.Observer((entries) => {
      seen = entries[entries.length - 1].isIntersecting;
      check();
    }, { rootMargin: `${nearPx}px 0px` }).observe(el);
  }
  env.doc.addEventListener('visibilitychange', check);
  env.setInterval(check, Math.min(periodMs, 5000));
  run();
  return { poke, now: run };
}
