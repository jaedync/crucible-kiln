// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as s from '../../src/kiln/web/static/js/panel/sync_model.js';

const SNAP = { me: { can_act: true, identity: 'localhost', sid: 7 },
  panel: { screen: 'glance', seq: 40, at: 1790000000, by: { source: 'idle', who: null, sid: null, req: null },
    settings: { idle_home_s: 300 } } };
const msg = (screen, seq, by = {}) => ({ v: 1, type: 'panel', screen, seq, at: 1790000001,
  by: { source: 'remote', who: 'owner-laptop', sid: 3, req: 1, ...by } });
const following = () => s.snapshot(s.initial(), SNAP);

test('a local change shows at once, sends one panel.set, and its echo clears it', () => {
  const { state, send } = s.act(following(), 'engine', 1000);
  assert.deepEqual(send, { type: 'panel.set', screen: 'engine', req: 1 });
  assert.equal(s.shown(state), 'engine');
  const echoed = s.received(state, msg('engine', 41, { sid: 7, req: 1, source: 'touch' }), 1100);
  assert.equal(echoed.pending, null);
  assert.equal(s.shown(echoed), 'engine');
  assert.equal(echoed.note, null);  // its own change needs no "set by"
});

test('another change while one is pending is kept but not shown: no flicker', () => {
  let st = s.act(following(), 'engine', 1000).state;
  st = s.received(st, msg('fleet', 41), 1050);
  assert.equal(s.shown(st), 'engine');
  st = s.received(st, msg('engine', 42, { sid: 7, req: 1 }), 1100);
  assert.equal(s.shown(st), 'engine');
  assert.equal(st.shared.seq, 42);
});

test('an older echo of the same session keeps the newest local change', () => {
  let st = s.act(following(), 'engine', 1000).state;
  st = s.act(st, 'fleet', 1010).state;
  st = s.received(st, msg('engine', 41, { sid: 7, req: 1 }), 1050);
  assert.equal(s.shown(st), 'fleet');
  st = s.received(st, msg('fleet', 42, { sid: 7, req: 2 }), 1060);
  assert.equal(st.pending, null);
});

test('a refusal or no echo in 2 s goes back to the shared screen', () => {
  const pending = s.act(following(), 'engine', 1000).state;
  const refused = s.refused(pending, { type: 'panel.refused', req: 1, error: 'this device may not change the rack panel' }, 1100);
  assert.equal(s.shown(refused), 'glance');
  assert.match(refused.note.text, /may not change/);
  assert.equal(s.refused(pending, { req: 99, error: 'x' }, 1100).pending.req, 1);  // not this change
  assert.equal(s.shown(s.expire(pending, 1000 + s.ECHO_TIMEOUT_MS - 1)), 'engine');
  assert.equal(s.shown(s.expire(pending, 1000 + s.ECHO_TIMEOUT_MS)), 'glance');
});

test('a change by another device or the server shows and names who made it for 10 s', () => {
  let st = s.received(following(), msg('today', 41), 1000);
  assert.equal(s.shown(st), 'today');
  assert.equal(st.note.text, 'Set by owner-laptop');
  assert.equal(s.expire(st, 1000 + s.NOTE_MS - 1).note.text, 'Set by owner-laptop');
  assert.equal(s.expire(st, 1000 + s.NOTE_MS).note, null);
  const texts = [['touch', 'Set by rack touch'], ['idle', 'Back home: idle'], ['alarm', 'Home: alarm'],
    ['auto:night_start', 'Auto: night_start']];
  for (const [source, text] of texts) {
    st = s.received(st, msg('glance', st.shared.seq + 1, { source, sid: null }), 2000);
    assert.equal(st.note.text, text);
  }
  st = s.received(st, { ...msg('glance', st.shared.seq + 1), settings: { idle_home_s: 600 } }, 3000);
  assert.equal(st.note.text, 'Settings set by owner-laptop');
  assert.deepEqual(st.shared.settings, { idle_home_s: 600 });
});

test('an old or repeated seq is ignored', () => {
  const st = s.received(following(), msg('engine', 41), 1000);
  assert.equal(s.received(st, msg('fleet', 41), 1001), st);
  assert.equal(s.received(st, msg('fleet', 12), 1001), st);
  assert.equal(s.received(st, { type: 'panel', screen: 'fleet' }, 1001), st);
});

test('a snapshot replaces the local state', () => {
  let st = s.act(following(), 'engine', 1000).state;
  st = s.received(st, msg('fleet', 41), 1001);
  st = s.snapshot(st, { ...SNAP, panel: { ...SNAP.panel, screen: 'today', seq: 50 }, me: { ...SNAP.me, sid: 9 } });
  assert.equal(st.pending, null);
  assert.equal(st.note, null);
  assert.equal(st.sid, 9);
  assert.equal(s.shown(st), 'today');
  assert.equal(s.shown(s.snapshot(s.initial(), { me: SNAP.me })), 'glance');  // an older server: no panel
});

test('a pinned session never sends and ignores the shared screen', () => {
  let st = s.snapshot(s.initial({ pin: 'engine' }), SNAP);
  assert.equal(s.mark(st), 'LOCAL');
  assert.equal(s.shown(st), 'engine');
  const turned = s.act(st, 'fleet', 1000);
  assert.equal(turned.send, null);
  assert.equal(s.input(turned.state, 1000).send, null);
  st = s.received(turned.state, msg('today', 41), 1000);
  assert.equal(s.shown(st), 'fleet');
  assert.equal(st.note, null);
  assert.equal(s.shown(st, { noData: true }), 'fleet');  // no alarm jump either
  assert.equal(s.shown(s.expire(st, 1e9)), 'fleet');  // and no way home
  assert.equal(s.shown(s.setPin(st, null)), 'today');  // the hash went away: it follows again
  assert.equal(s.mark(s.setPin(st, null)), null);
});

test('a view-only session pages locally, sends nothing, and returns to the shared screen after 60 s', () => {
  let st = s.snapshot(s.initial(), { ...SNAP, me: { can_act: false, identity: null, sid: 8 } });
  assert.equal(s.mark(st), 'VIEW ONLY');
  const turned = s.act(st, 'engine', 1000);
  assert.equal(turned.send, null);
  assert.equal(s.input(turned.state, 1000).send, null);
  st = s.received(turned.state, msg('today', 41), 2000);
  assert.equal(s.shown(st), 'engine');  // still looking around
  st = s.input(st, 30000).state;
  assert.equal(s.shown(s.expire(st, 30000 + s.VIEW_RETURN_MS - 1)), 'engine');
  assert.equal(s.shown(s.expire(st, 30000 + s.VIEW_RETURN_MS)), 'today');
  assert.equal(s.shown(s.received(st, msg('glance', 42, { source: 'alarm', sid: null }), 31000)), 'glance');
});

test('NO DATA shows home locally on a following session', () => {
  const st = s.received(following(), msg('engine', 41), 1000);
  assert.equal(s.shown(st, { noData: true }), 'glance');
  assert.equal(s.shown(st), 'engine');
});

test('person input sends panel.touch at most once in 5 s, and receiving never sends', () => {
  let st = following();
  const sent = [];
  for (let t = 0; t <= 12000; t += 500) {
    const r = s.input(st, t);
    st = r.state;
    if (r.send) sent.push([t, r.send.type]);
  }
  assert.deepEqual(sent, [[0, 'panel.touch'], [5000, 'panel.touch'], [10000, 'panel.touch']]);
  // A panel.set counts as input on the server too: no touch right after it.
  st = s.act(st, 'engine', 13000).state;
  assert.equal(s.input(st, 14000).send, null);
  // The reducer's receive functions return a state only: nothing to send.
  const r = s.received(st, msg('fleet', 41), 15000);
  assert.equal(r.send, undefined);
});

test('the demo (no socket) pages locally with no mark', () => {
  const st = s.snapshot(s.initial({ solo: true }), { me: null });
  assert.equal(s.mark(st), null);
  const turned = s.act(st, 'settings', 0);
  assert.equal(turned.send, null);
  assert.equal(s.shown(s.expire(turned.state, 1e9)), 'settings');
});

test('before the first snapshot a session sends nothing', () => {
  const r = s.act(s.initial(), 'engine', 0);
  assert.equal(r.send, null);
  assert.equal(s.input(s.initial(), 0).send, null);
  assert.equal(s.mark(s.initial()), null);
});

const SETTINGS = { idle_home_s: 300, manual_quiet_s: 300, alarm_home: true,
  auto: { enabled: false, rules: { engine_switch: { on: false, screen: 'engine', hold_s: 300 } } } };
const withSettings = () => s.snapshot(s.initial(), { ...SNAP, panel: { ...SNAP.panel, settings: SETTINGS } });

test('a settings change shows at once, sends one panel.settings, and its echo hands over to the server', () => {
  const { state, send } = s.actSettings(withSettings(), { auto: { enabled: true } }, 1000);
  assert.deepEqual(send, { type: 'panel.settings', settings: { auto: { enabled: true } }, req: 1 });
  assert.equal(s.settingsShown(state).auto.enabled, true);
  assert.equal(s.settingsShown(state).auto.rules.engine_switch.screen, 'engine');  // a deep merge
  assert.equal(s.shown(state), 'glance');  // no screen change
  // The server's own answer wins once it comes: here it also clamps nothing, so the box stays on.
  const echo = { ...msg('glance', 41, { sid: 7, req: 1, source: 'touch' }), settings: { ...SETTINGS, auto: { ...SETTINGS.auto, enabled: true } } };
  const done = s.received(state, echo, 1100);
  assert.equal(done.pendingSettings, null);
  assert.equal(s.settingsShown(done).auto.enabled, true);
  assert.equal(done.note, null);
});

test('two quick settings taps show both until the later echo; an earlier echo does not flash the old value', () => {
  let st = s.actSettings(withSettings(), { idle_home_s: 60 }, 1000).state;
  const second = s.actSettings(st, { manual_quiet_s: 900 }, 1010);
  assert.equal(second.send.req, 2);
  st = second.state;
  st = s.received(st, { ...msg('glance', 41, { sid: 7, req: 1 }), settings: { ...SETTINGS, idle_home_s: 60 } }, 1050);
  assert.deepEqual([s.settingsShown(st).idle_home_s, s.settingsShown(st).manual_quiet_s], [60, 900]);
  st = s.received(st, { ...msg('glance', 42, { sid: 7, req: 2 }), settings: { ...SETTINGS, idle_home_s: 60, manual_quiet_s: 900 } }, 1060);
  assert.equal(st.pendingSettings, null);
});

test('a refused or unanswered settings change goes back to the server settings', () => {
  const pending = s.actSettings(withSettings(), { idle_home_s: 0 }, 1000).state;
  const refused = s.refused(pending, { req: 1, error: 'idle_home_s must be a number' }, 1100);
  assert.equal(s.settingsShown(refused).idle_home_s, 300);
  assert.equal(refused.note.kind, 'refused');
  assert.equal(s.settingsShown(s.expire(pending, 1000 + s.ECHO_TIMEOUT_MS - 1)).idle_home_s, 0);
  assert.equal(s.settingsShown(s.expire(pending, 1000 + s.ECHO_TIMEOUT_MS)).idle_home_s, 300);
});

test('only a following session may change settings', () => {
  const view = s.snapshot(s.initial(), { ...SNAP, me: { can_act: false, sid: 8 }, panel: { ...SNAP.panel, settings: SETTINGS } });
  assert.equal(s.canEdit(view), false);
  assert.deepEqual(s.actSettings(view, { alarm_home: false }, 0), { state: view, send: null });
  const pinned = s.snapshot(s.initial({ pin: 'settings' }), { ...SNAP, panel: { ...SNAP.panel, settings: SETTINGS } });
  assert.equal(s.canEdit(pinned), false);
  assert.equal(s.canEdit(withSettings()), true);
  assert.equal(s.settingsShown(s.initial()), null);
});

test('the last change is named for the sync cell; a settings change says so', () => {
  let st = s.received(withSettings(), msg('today', 41), 1000);
  assert.equal(s.byText(st.shared), 'Set by owner-laptop');
  st = s.received(st, { ...msg('today', 42), settings: SETTINGS }, 1100);
  assert.equal(s.byText(st.shared), 'Settings set by owner-laptop');
  assert.equal(s.byText(withSettings().shared), 'Back home: idle');
});
