// node --test tools/web_tests/*.test.mjs
// The rack panel's step 3 on the client: the alarm mute, the tab flags and the night display.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as s from '../../src/kiln/web/static/js/panel/sync_model.js';
import * as t from '../../src/kiln/web/static/js/panel/tabs_model.js';
import * as g from '../../src/kiln/web/static/js/panel/glance_model.js';

const NOW = 1790000000;
const SNAP = { me: { can_act: true, identity: 'localhost', sid: 7 },
  panel: { screen: 'glance', seq: 40, at: NOW - 4, by: { source: 'touch', who: 'localhost', sid: 3, req: 1 },
    what: 'screen', mute: null, flags: [], settings: { idle_home_s: 300 } } };
const following = () => s.snapshot(s.initial(), SNAP);
const panelMsg = (seq, extra = {}) => ({ type: 'panel', screen: 'glance', seq, at: NOW - 4,
  by: { source: 'touch', who: 'localhost', sid: 3, req: 1 }, what: 'screen', mute: null, flags: [], ...extra });
const HOT = { edge_c: 91, fan_rpm: 3120 };
const READY = { state: 'ready', label: 'Crucible', since: NOW - 600, schedule: { in_day: true } };
const ENGINE_SWITCH = { screen: 'engine', tone: 'warn', why: 'Engine switch: see Engine', cause: 'quiet',
  quiet_until: NOW + 230 };

// -- the mute in the session ------------------------------------------------------------------------
test('a mute shows at once, sends one panel.mute, and the echo hands over to the server', () => {
  const { state, send } = s.actMute(following(), 'hot', 1000, NOW);
  assert.deepEqual(send, { type: 'panel.mute', kind: 'hot', req: 1 });
  assert.deepEqual(s.muteShown(state, NOW), { kind: 'hot', until: NOW + 1800 });
  const mute = { kind: 'hot', until: NOW + 1795 };
  const echoed = s.received(state, panelMsg(41, { what: 'mute', mute, by: { source: 'touch', sid: 7, req: 1 } }), 1100);
  assert.equal(echoed.pendingMute, null);
  assert.deepEqual(s.muteShown(echoed, NOW), mute);
  assert.equal(s.muteShown(echoed, NOW + 1795), null);  // its time is over, also before the server says so
});

test('a refused mute or one without an echo shows the server state again', () => {
  const pending = s.actMute(following(), 'hot', 1000, NOW).state;
  const refused = s.refused(pending, { req: 1, error: 'no such alarm now' }, 1100);
  assert.equal(s.muteShown(refused, NOW), null);
  assert.match(refused.note.text, /no such alarm/);
  assert.equal(s.muteShown(s.expire(pending, 1000 + s.ECHO_TIMEOUT_MS), NOW), null);
});

test('a session that may not act sends no mute', () => {
  const view = s.snapshot(s.initial(), { ...SNAP, me: { can_act: false, sid: 7 } });
  assert.equal(s.actMute(view, 'hot', 1000, NOW).send, null);
});

test('a mute or its end by another device is news; a flag that appears is news', () => {
  let st = s.received(following(), panelMsg(41, { what: 'mute', mute: { kind: 'hot', until: NOW + 1800 },
    by: { source: 'remote', who: 'owner-laptop', sid: 3, req: 2 } }), 1000);
  assert.equal(st.note.text, 'Alarm muted by owner-laptop');
  st = s.received(st, panelMsg(42, { what: 'mute' }), 2000);
  assert.equal(st.note.text, 'Alarm mute ended');
  st = s.received(st, panelMsg(43, { what: 'flags', flags: [ENGINE_SWITCH] }), 3000);
  assert.deepEqual([st.note.kind, st.note.text], ['flag', 'Engine switch: see Engine']);
  const later = s.received(s.expire(st, 3000 + s.NOTE_MS), panelMsg(44, { what: 'flags', flags: [] }), 14000);
  assert.equal(later.note, null);  // a flag that goes is no news
  assert.deepEqual(s.flagsShown(st), [ENGINE_SWITCH]);
});

// -- the tab flags and the sync cell ----------------------------------------------------------------------
test('each tab gets the tone of its flag', () => {
  const bad = { screen: 'fleet', tone: 'bad', why: 'Plan item failed: Last check', cause: 'fault', quiet_until: null };
  assert.deepEqual(t.tabFlags([ENGINE_SWITCH, bad]), { engine: 'warn', fleet: 'bad' });
  assert.deepEqual(t.tabFlags(null), {});
});

test('the sync cell says why a flagged screen did not open, in minutes', () => {
  const st = s.received(following(), panelMsg(41, { what: 'flags', flags: [ENGINE_SWITCH] }), 1000);
  assert.deepEqual(t.syncCell(st, 'today', NOW), { head: 'Manual quiet · 4 m left', line: 'Engine switch: see Engine',
    tone: 'hold' });
  assert.equal(t.syncCell(st, 'today', NOW + 231).head, 'Held in the quiet time');
  const off = s.received(following(), panelMsg(41, { flags: [{ ...ENGINE_SWITCH, cause: 'off' }] }), 1000);
  assert.equal(t.syncCell(off, 'today', NOW).head, 'Auto-switch off');
  const bad = { screen: 'fleet', tone: 'bad', why: 'Plan item failed: Last check', cause: 'fault', quiet_until: null };
  const both = s.received(following(), panelMsg(41, { flags: [ENGINE_SWITCH, bad] }), 1000);
  assert.deepEqual(t.syncCell(both, 'today', NOW), { head: 'Fault', line: 'Plan item failed: Last check', tone: 'bad' });
  // On the flagged screen itself the flag says nothing new.
  assert.equal(t.syncCell(st, 'engine', NOW).line, 'Home · just now');
});

// -- the alarm with a mute on Home --------------------------------------------------------------------------
test('a muted hot GPU shows the normal layout; a new kind still takes the screen; NO DATA is never muted', () => {
  const mute = { kind: 'hot', until: NOW + 1680 };
  assert.equal(g.glanceMode({ engine: READY, hw: HOT, connDownS: 0 }), 'alarm');
  assert.equal(g.glanceMode({ engine: READY, hw: HOT, connDownS: 0, mute }), 'serve');
  assert.equal(g.glanceMode({ engine: { ...READY, state: 'failed' }, hw: HOT, connDownS: 0, mute }), 'alarm');
  assert.equal(g.alarmInfo({ engine: { ...READY, state: 'failed' }, hw: HOT, connDownS: 0, now: NOW, mute }).kind, 'failed');
  assert.equal(g.glanceMode({ engine: READY, hw: HOT, connDownS: 30, mute }), 'alarm');
  assert.equal(g.alarmInfo({ engine: READY, hw: HOT, connDownS: 30, now: NOW }).kind, 'nodata');
});

test('the mute button shows for a hot GPU and a failed engine, not for NO DATA', () => {
  assert.deepEqual(g.muteButton({ kind: 'hot' }, true), { show: true, kind: 'hot', editable: true,
    note: 'The red screen stops.\nThe red gauge stays.' });
  assert.equal(g.muteButton({ kind: 'failed' }, false).editable, false);
  assert.equal(g.muteButton({ kind: 'nodata' }, true).show, false);
  assert.equal(g.muteButton(null, true).show, false);
});

test('the muted tag counts whole minutes, so it repaints at most once a minute', () => {
  const mute = { kind: 'hot', until: NOW + 1680 };
  assert.equal(g.muteTag(mute, NOW), 'Alarm muted · 28 m');
  assert.equal(g.muteTag(mute, NOW + 1679), 'Alarm muted · 1 m');
  assert.equal(g.muteTag(mute, NOW + 1680), '');
  assert.equal(g.muteTag(null, NOW), '');
  const seen = new Set();
  for (let x = 0; x < 60; x += 0.5) seen.add(g.muteTag(mute, NOW + 30 + x));
  assert.equal(seen.size, 2);
});

// -- dim at night -------------------------------------------------------------------------------------------
test('the panel dims at night with Dim on and no alarm; an alarm is never dimmed', () => {
  const night = { ...READY, state: 'stopped', schedule: { in_day: false } };
  const dim = { night_display: 'dim' };
  assert.equal(g.dimNow({ settings: dim, engine: night, alarm: false }), true);
  assert.equal(g.dimNow({ settings: dim, engine: night, alarm: true }), false);
  assert.equal(g.dimNow({ settings: dim, engine: READY, alarm: false }), false);  // by day
  assert.equal(g.dimNow({ settings: { night_display: 'normal' }, engine: night, alarm: false }), false);
  assert.equal(g.dimNow({ settings: null, engine: night, alarm: false }), false);
  assert.equal(g.dimNow({ settings: dim, engine: { state: 'stopped' }, alarm: false }), false);  // no schedule
});

test('any alarm kind counts for the dim rule, also a muted one', () => {
  assert.deepEqual(g.alarmKinds({ engine: { state: 'failed' }, hw: HOT, connDownS: 0 }), ['failed', 'hot']);
  assert.deepEqual(g.alarmKinds({ engine: READY, hw: { edge_c: 60 }, connDownS: 25 }), ['nodata']);
  assert.deepEqual(g.alarmKinds({ engine: READY, hw: {}, connDownS: 0 }), []);
});
