// Shared state and a small event bus. State objects are frozen and replaced on
// every change, so a panel holding an old reference sees a consistent picture.
//
// Events: 'snapshot', 'tick', 'request', 'engine', 'conn', 'clients', 'panel', 'panel.refused'.

// Same order as the server's (store/queries.py): most distinct first.
const PALETTE = ['#1d4f91', '#d2342a', '#f2c230', '#161616', '#7a7466', '#2f7a5c', '#6b4f96', '#5b86c5', '#e8846f',
  '#b39020', '#8a2f24', '#9fb8dc', '#3d3a33', '#f6dc7a'];

export function createStore() {
  let state = Object.freeze({
    conn: 'connecting',   // connecting | open | stale | reconnecting
    skew: 0,              // server clock minus browser clock, seconds
    tick: null,           // latest tick (or the snapshot's tick fields)
    engine: null,         // latest engine state, from tick or engine events
    profiles: [],
    me: null,
    build: null,
    clients: {},          // client key -> {name, color}, from /api/v1/activity
    pricing: null,        // {reference: name}, from the snapshot
    panel: null,          // the rack panel's shared screen {screen, seq, at, by, settings} (dash/panel_sync.py)
  });
  const subs = new Map();

  function on(evt, fn) {
    if (!subs.has(evt)) subs.set(evt, new Set());
    subs.get(evt).add(fn);
    return () => subs.get(evt).delete(fn);
  }

  function emit(evt, payload) {
    for (const fn of subs.get(evt) || []) {
      try {
        fn(payload, state);
      } catch (err) {
        console.error(`kiln: ${evt} handler failed`, err);
      }
    }
  }

  const get = () => state;
  const set = (patch) => { state = Object.freeze({ ...state, ...patch }); };
  // Server time, so elapsed clocks agree with the server's timestamps.
  const now = () => Date.now() / 1000 + state.skew;
  const isLive = () => state.conn === 'open';

  // The server's colour wins: pass a client object carrying `color`, or a key
  // plus an explicit colour. The hash palette is only a fallback for old data.
  function colorFor(client, explicit) {
    if (explicit) return explicit;
    if (client && typeof client === 'object') {
      if (client.color) return client.color;
      client = client.key;
    }
    const key = client;
    const known = state.clients[key];
    if (known && known.color) return known.color;
    let hash = 0;
    for (const ch of String(key)) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    return PALETTE[hash % PALETTE.length];
  }

  return { get, set, on, emit, now, isLive, colorFor, palette: PALETTE };
}
