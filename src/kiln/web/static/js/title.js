// The browser tab's title: while streams run, the decode speed first and the engine's slots in use, so a
// background tab shows throughput at a glance ("142 tok/s 3/4 +2 queued Kiln strix"). Idle: the page title.
// Spaces only, and no ≈ on an estimated rate: a narrow tab shows only the first few characters.
import * as f from './format.js';

// tabTitle(tick.live, base) -> the title to show
export function tabTitle(live, base) {
  const streams = (live && live.streams) || [];
  if (!streams.length) return base;
  const queued = streams.filter((st) => st.queued).length;
  const running = streams.length - queued;
  const tps = live.decode_tps || 0;
  const prefilling = tps < 1 && streams.some((st) => !st.queued && st.phase === 'prefill');
  const speed = prefilling ? 'prefill' : `${f.rate(tps)} tok/s`;
  const use = live.slots > 0 ? `${running}/${live.slots}` : `${running}`;
  return `${speed} ${use}${queued ? ` +${queued} queued` : ''} ${base}`;
}

// showInTab(store): keep document.title current from the live feed; the page title while offline.
export function showInTab(store) {
  let base = document.title;
  const host = document.getElementById('host-tag');
  store.on('snapshot', (snap) => {
    // The server's host name ([host] in kiln.toml) names the page: "Kiln strix".
    if (snap.host) base = `Kiln ${snap.host}`;
    if (host && snap.host && host.textContent !== snap.host) host.textContent = snap.host;
  });
  const set = (title) => { if (document.title !== title) document.title = title; };
  store.on('snapshot', (snap) => set(tabTitle(snap.live, base)));
  store.on('tick', (tick) => { if (tick.type === 'tick') set(tabTitle(tick.live, base)); });
  // Offline or stale: numbers that no longer update would mislead.
  store.on('conn', (conn) => { if (conn !== 'open') set(base); });
}
