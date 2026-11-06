// The rack panel's shared screen as one session sees it (pure; tools/web_tests/panel_sync.test.mjs).
// The dashboard holds the screen (dash/panel_sync.py) and sends it to every socket. A session that may act
// shows its own change at once and keeps it until its echo (same sid and req) comes back. A session sends
// only for a person's input, never because a message came: two sessions can never ping-pong.
// Modes: follow (may act), view (may not act: it pages locally and goes back to the shared screen),
// local (a #<id> pin, or the demo without a socket: it never sends and ignores the shared screen).
// Settings work the same way: a change shows at once (pendingSettings) and the echo hands over to the server.
// So does the alarm mute (pendingMute). The mute and the tab flags are the dashboard's: every panel message
// carries them whole, and a session only draws them.
export const HOME = 'glance';
export const ECHO_TIMEOUT_MS = 2000;
export const TOUCH_EVERY_MS = 5000;  // the server's idle timer needs to know a person is there, not each tap
export const VIEW_RETURN_MS = 60000;
export const NOTE_MS = 10000;
export const MUTE_S = 1800;  // live/panel_screen.MUTE_S: the server sets the end; this is the guess until its echo

export const initial = ({ pin = null, solo = false } = {}) => Object.freeze({
  pin, solo, canAct: false, sid: null, shared: null, local: pin, pending: null, pendingSettings: null,
  pendingMute: null, req: 0,
  lastInput: -Infinity, lastTouch: -Infinity, note: null,
});

const next = (s, patch) => Object.freeze({ ...s, ...patch });
const isLocal = (s) => Boolean(s.pin || s.solo);

export function mode(s) {
  if (isLocal(s)) return 'local';
  return s.canAct && s.sid !== null ? 'follow' : 'view';
}

export function mark(s) {
  if (s.solo) return null;
  if (s.pin) return 'LOCAL';
  return s.sid !== null && !s.canAct ? 'VIEW ONLY' : null;
}

// what: the last change (screen, settings, mute or flags); the sync cell names a settings or a mute change.
// A dashboard of step 2 sends no `what`, mute or flags: then a message (not a snapshot) with settings is a
// settings change.
const sharedOf = (p, prev, guess = 'screen') => ({ screen: p.screen, seq: p.seq, at: p.at, by: p.by || {},
  settings: p.settings || (prev && prev.settings) || null, what: p.what || guess,
  mute: p.mute || null, flags: Array.isArray(p.flags) ? p.flags : [] });

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
function merged(base, patch) {
  const out = { ...base };
  for (const [k, v] of Object.entries(patch)) out[k] = isObj(v) && isObj(base[k]) ? merged(base[k], v) : v;
  return out;
}

export function snapshot(s, snap) {
  const me = snap.me || {};
  const panel = snap.panel && typeof snap.panel.seq === 'number' ? sharedOf(snap.panel, null) : null;
  return next(s, { canAct: me.can_act === true, sid: typeof me.sid === 'number' ? me.sid : null,
    shared: panel, pending: null, pendingSettings: null, pendingMute: null, note: null,
    local: isLocal(s) ? s.local : null });
}

function noteText(by, what, mute) {
  const who = by.who || 'another device';
  const source = String(by.source || '');
  if (what === 'settings') return `Settings set by ${who}`;
  if (what === 'mute') return mute ? `Alarm muted by ${source === 'touch' ? 'rack touch' : who}` : 'Alarm mute ended';
  if (source === 'touch') return 'Set by rack touch';
  if (source === 'remote') return `Set by ${who}`;
  if (source === 'idle') return 'Back home: idle';
  if (source.startsWith('auto:')) return `Auto: ${source.slice(5)}`;
  if (source === 'alarm') return 'Home: alarm';
  return 'Home: restart';
}

// Who made the last change, for the strip's sync cell.
export const byText = (shared) => noteText(shared.by || {}, shared.what === 'flags' ? 'screen' : shared.what,
  shared.mute);

// The note a message brings: a flag that appeared, a change by someone else, or nothing.
function newsOf(s, shared, own, nowMs) {
  const until = nowMs + NOTE_MS;
  if (shared.what === 'flags') {
    const had = new Set(((s.shared && s.shared.flags) || []).map((f) => `${f.screen}|${f.why}`));
    const fresh = shared.flags.filter((f) => !had.has(`${f.screen}|${f.why}`));
    return fresh.length ? { kind: 'flag', text: fresh[fresh.length - 1].why, until } : s.note;
  }
  return own ? s.note : { kind: 'change', text: noteText(shared.by, shared.what, shared.mute), until };
}

export function received(s, msg, nowMs) {
  const known = s.shared ? s.shared.seq : -1;
  if (typeof msg.seq !== 'number' || msg.seq <= known) return s;
  const shared = sharedOf(msg, s.shared, msg.settings ? 'settings' : 'screen');
  if (isLocal(s)) return next(s, { shared });  // kept, for the moment the pin goes away
  const by = shared.by;
  // A flag or a mute's end by the dashboard keeps `by`: it may still name this session, so it is no echo.
  const own = s.sid !== null && by.sid === s.sid && shared.what !== 'flags';
  const echo = own && s.pending !== null && by.req === s.pending.req;
  const settingsEcho = own && s.pendingSettings !== null && by.req === s.pendingSettings.req;
  const muteEcho = own && s.pendingMute !== null && by.req === s.pendingMute.req;
  const note = newsOf(s, shared, own, nowMs);
  // A pending change stays on screen until its own echo: an earlier message must not flash through.
  // An alarm also ends a view-only session's look around.
  const done = echo || (s.pending === null && mode(s) === 'follow') || (mode(s) === 'view' && by.source === 'alarm');
  const local = done ? null : s.local;
  return next(s, { shared, note, pending: echo ? null : s.pending, local,
    pendingSettings: settingsEcho ? null : s.pendingSettings, pendingMute: muteEcho ? null : s.pendingMute });
}

export function refused(s, msg, nowMs) {
  const note = { kind: 'refused', text: `Not changed: ${msg.error || 'refused'}`, until: nowMs + NOTE_MS };
  if (s.pendingSettings !== null && msg.req === s.pendingSettings.req) return next(s, { pendingSettings: null, note });
  if (s.pendingMute !== null && msg.req === s.pendingMute.req) return next(s, { pendingMute: null, note });
  if (s.pending === null || msg.req !== s.pending.req) return s;
  return next(s, { pending: null, local: null, note });
}

export const canEdit = (s) => mode(s) === 'follow';

// A person changed a setting (a partial settings object). Taps in a row add up until the last one's echo.
export function actSettings(s, patch, nowMs) {
  if (!canEdit(s)) return { state: s, send: null };
  const req = s.req + 1;
  const all = merged(s.pendingSettings ? s.pendingSettings.patch : {}, patch);
  const state = next(s, { req, pendingSettings: { req, patch: all, at: nowMs }, lastInput: nowMs, lastTouch: nowMs });
  return { state, send: { type: 'panel.settings', settings: patch, req } };
}

// The settings to draw: the server's, with this session's change on top until its echo.
export function settingsShown(s) {
  const base = s.shared && s.shared.settings;
  if (!base) return null;
  return s.pendingSettings ? merged(base, s.pendingSettings.patch) : base;
}

// A person muted the alarm of `kind` ('hot' or 'failed') for 30 minutes, or ended the mute (null). wallNow:
// the dashboard's clock (store.now), for the minutes shown until the echo brings the real end.
export function actMute(s, kind, nowMs, wallNow) {
  if (!canEdit(s)) return { state: s, send: null };
  const req = s.req + 1;
  const mute = kind ? { kind, until: wallNow + MUTE_S } : null;
  const state = next(s, { req, pendingMute: { req, mute, at: nowMs }, lastInput: nowMs, lastTouch: nowMs });
  return { state, send: { type: 'panel.mute', kind: kind || null, req } };
}

// The mute in force at wall time `now` (this session's change on top until its echo), or null.
export function muteShown(s, now) {
  const m = s.pendingMute ? s.pendingMute.mute : s.shared && s.shared.mute;
  return m && typeof m.until === 'number' && m.until > now ? m : null;
}

export const flagsShown = (s) => (s.shared && s.shared.flags) || [];

// A person paged to `screen`. Returns the new state and the message to send (or null).
export function act(s, screen, nowMs) {
  switch (mode(s)) {
    case 'local': return { state: next(s, { local: screen }), send: null };
    case 'view': return { state: next(s, { local: screen, lastInput: nowMs }), send: null };
    default: {
      const req = s.req + 1;
      // The server counts a panel.set as input too, so no touch is due for TOUCH_EVERY_MS.
      const state = next(s, { req, pending: { req, screen, at: nowMs }, local: screen, lastInput: nowMs,
        lastTouch: nowMs });
      return { state, send: { type: 'panel.set', screen, req } };
    }
  }
}

// Any other person input (a tap that turns no page, a key).
export function input(s, nowMs) {
  const m = mode(s);
  if (m === 'local') return { state: s, send: null };
  if (m === 'view' || nowMs - s.lastTouch < TOUCH_EVERY_MS) return { state: next(s, { lastInput: nowMs }), send: null };
  return { state: next(s, { lastInput: nowMs, lastTouch: nowMs }), send: { type: 'panel.touch' } };
}

// Timers, checked when the panel draws (ticks come twice a second): no timer of its own.
export function expire(s, nowMs) {
  let patch = null;
  if (s.pending && nowMs - s.pending.at >= ECHO_TIMEOUT_MS) patch = { pending: null, local: null };
  if (s.pendingSettings && nowMs - s.pendingSettings.at >= ECHO_TIMEOUT_MS) patch = { ...patch, pendingSettings: null };
  if (s.pendingMute && nowMs - s.pendingMute.at >= ECHO_TIMEOUT_MS) patch = { ...patch, pendingMute: null };
  if (mode(s) === 'view' && s.local !== null && nowMs - s.lastInput >= VIEW_RETURN_MS) patch = { ...patch, local: null };
  if (s.note && nowMs >= s.note.until) patch = { ...patch, note: null };
  return patch ? next(s, patch) : s;
}

export function setPin(s, pin) {
  if (pin === s.pin) return s;
  return next(s, { pin, local: pin, pending: null, note: null });
}

// The screen to draw. Without data for CONN_GRACE_S a following session shows home (its NO DATA alarm).
export function shown(s, { noData = false } = {}) {
  if (noData && !isLocal(s)) return HOME;
  if (s.local !== null) return s.local;
  return s.shared ? s.shared.screen : HOME;
}
