// WebSocket /api/v1/live: snapshot, tick, request, engine, and the rack panel's shared screen (panel,
// panel.refused). Reconnects with backoff. A tick older than STALE_MS marks the feed stale so
// panels blank live numbers instead of showing a frozen rate as if it were current.
// The feed is one way except for the rack panel's screen messages: connectLive returns { send }.

const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
const STALE_MS = 2500;
const DEAD_MS = 10000;
const SCHEMA = 1;

function liveUrl() {
  const u = new URL('api/v1/live', location.href);
  u.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.href;
}

export function connectLive(store) {
  let attempt = 0;
  // Freshness comes only from snapshot and tick: events alone don't prove the
  // live numbers are current.
  let lastLiveAt = 0;
  let openedAt = 0;
  let sock = null;

  function setConn(conn) {
    if (store.get().conn === conn) return;
    store.set({ conn });
    store.emit('conn', conn);
  }

  function open() {
    sock = new WebSocket(liveUrl());
    openedAt = Date.now();
    sock.onmessage = (ev) => handle(ev.data);
    sock.onclose = retry;
    sock.onerror = () => sock && sock.close();
  }

  function retry() {
    sock = null;
    setConn('reconnecting');
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt += 1;
    setTimeout(open, delay);
  }

  // A wedged peer (a hung server, a half-open link after Wi-Fi drops) never answers the close handshake,
  // so onclose would not fire for minutes: forget the socket and retry now.
  function drop() {
    const old = sock;
    old.onmessage = old.onclose = old.onerror = null;
    old.close();
    retry();
  }

  function handle(raw) {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch (err) {
      console.warn('kiln: unparseable live message', err);
      return;
    }
    if (msg.v !== SCHEMA) console.warn(`kiln: live schema v${msg.v}, UI expects v${SCHEMA}`);
    const handler = HANDLERS[msg.type];
    if (handler) handler(msg);
  }

  const HANDLERS = {
    snapshot(msg) {
      const prev = store.get().build;
      // A reconnect that lands on a new server build means the UI files changed too.
      if (prev && msg.build && prev !== msg.build) {
        location.reload();
        return;
      }
      attempt = 0;
      lastLiveAt = Date.now();
      store.set({
        build: msg.build, profiles: msg.profiles || [], me: msg.me || null, pricing: msg.pricing || null,
        tick: msg, engine: msg.engine || null, skew: msg.ts - Date.now() / 1000, panel: msg.panel || null,
      });
      setConn('open');
      store.emit('snapshot', msg);
      store.emit('tick', msg);
    },
    tick(msg) {
      lastLiveAt = Date.now();
      store.set({ tick: msg, engine: msg.engine || store.get().engine, skew: msg.ts - Date.now() / 1000 });
      setConn('open');
      store.emit('tick', msg);
    },
    request(msg) {
      store.emit('request', msg.request);
    },
    engine(msg) {
      store.set({ engine: msg.engine });
      store.emit('engine', msg);
    },
    panel(msg) {
      const prev = store.get().panel;
      if (!prev || msg.seq > prev.seq) {
        store.set({ panel: { screen: msg.screen, seq: msg.seq, at: msg.at, by: msg.by,
          settings: msg.settings || (prev && prev.settings) || null } });
      }
      store.emit('panel', msg);
    },
    'panel.refused'(msg) {
      store.emit('panel.refused', msg);
    },
  };

  // False when the socket is not open: the caller's change then times out and shows the shared state.
  function send(msg) {
    if (!sock || sock.readyState !== WebSocket.OPEN) return false;
    sock.send(JSON.stringify(msg));
    return true;
  }

  setInterval(() => {
    const quiet = Date.now() - lastLiveAt;
    if (store.get().conn === 'open' && quiet > STALE_MS) setConn('stale');
    // A socket that stays open but silent is wedged; drop it and reconnect. A new socket counts from when it
    // opened: one that never sends (the server still hung) is dropped in turn, so the retries go on.
    if (sock && Date.now() - Math.max(lastLiveAt, openedAt) > DEAD_MS) drop();
  }, 500);

  setConn('connecting');
  open();
  return { send };
}
