// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as t from '../../src/kiln/web/static/js/panel/tabs_model.js';
import * as s from '../../src/kiln/web/static/js/panel/sync_model.js';

const NOW = 1790000000;
const SNAP = { me: { can_act: true, identity: 'localhost', sid: 7 },
  panel: { screen: 'engine', seq: 40, at: NOW - 4, by: { source: 'touch', who: 'localhost', sid: 3, req: 1 } } };
const following = () => s.snapshot(s.initial(), SNAP);

test('the tabs are the five screens in paging order, Home first', () => {
  assert.deepEqual(t.TABS.map(([id]) => id), ['glance', 'engine', 'fleet', 'today', 'settings']);
  assert.equal(t.tabName('settings'), 'Settings');
  assert.equal(t.tabName('gpu'), 'gpu');
});

test('the age is coarse, so the sync cell repaints at most once a minute', () => {
  assert.equal(t.ageText(NOW - 4, NOW), 'just now');
  assert.equal(t.ageText(NOW - 59.9, NOW), 'just now');
  assert.equal(t.ageText(NOW - 60, NOW), '1 min ago');
  assert.equal(t.ageText(NOW - 119, NOW), '1 min ago');
  assert.equal(t.ageText(NOW - 3599, NOW), '59 min ago');
  assert.match(t.ageText(NOW - 3600, NOW), /^since \d/);  // then a clock time, which never changes
  assert.equal(t.ageText(NOW + 30, NOW), 'just now');  // a clock a little ahead of this one
  assert.equal(t.ageText(null, NOW), '');
  // Within one minute the text is the same at every draw.
  const seen = new Set();
  for (let x = 0; x < 60; x += 0.5) seen.add(t.ageText(NOW - 590, NOW + x));
  assert.deepEqual([...seen], ['9 min ago', '10 min ago']);  // one change in the minute
});

test('the sync cell says who set the shared screen and when', () => {
  assert.deepEqual(t.syncCell(following(), 'engine', NOW), { head: 'Set by rack touch', line: 'Engine · just now', tone: null });
  let st = s.received(following(), { seq: 41, screen: 'glance', at: NOW - 120, by: { source: 'idle' } }, 0);
  assert.deepEqual(t.syncCell(st, 'glance', NOW), { head: 'Back home: idle', line: 'Home · 2 min ago', tone: null });
  st = s.received(st, { seq: 42, screen: 'engine', at: NOW, by: { source: 'auto:engine_switch' } }, 0);
  assert.equal(t.syncCell(st, 'engine', NOW).head, 'Auto: engine_switch');
  st = s.received(st, { seq: 43, screen: 'fleet', at: NOW, by: { source: 'remote', who: 'owner-laptop', sid: 2 } }, 0);
  assert.equal(t.syncCell(st, 'fleet', NOW).head, 'Set by owner-laptop');
});

test('the sync cell tells a refusal, a change on its way, and a look around that the rack does not share', () => {
  const sent = s.act(following(), 'today', 1000).state;
  assert.deepEqual(t.syncCell(sent, 'today', NOW), { head: 'Sending', line: 'Today', tone: null });
  const refused = s.refused(sent, { req: 1, error: 'this device may not change the rack panel' }, 1100);
  assert.deepEqual(t.syncCell(refused, 'engine', NOW),
    { head: 'Not changed', line: 'this device may not change the rack panel', tone: 'bad' });
  const viewer = s.snapshot(s.initial(), { ...SNAP, me: { can_act: false, sid: 8 } });
  const look = s.act(viewer, 'fleet', 0).state;
  assert.deepEqual(t.syncCell(look, 'fleet', NOW), { head: 'Only here', line: 'Rack shows Engine', tone: null });
  const pinned = s.snapshot(s.initial({ pin: 'today' }), SNAP);
  assert.deepEqual(t.syncCell(pinned, 'today', NOW), { head: 'Only here', line: 'Rack shows Engine', tone: null });
  assert.deepEqual(t.syncCell(s.initial(), 'glance', NOW), { head: 'Connecting', line: '', tone: null });
  assert.deepEqual(t.syncCell(s.snapshot(s.initial({ solo: true }), {}), 'glance', NOW),
    { head: 'Demo', line: 'not synced', tone: null });
});

test('on Home the strip shows for 10 s after a tap, and while a change by someone else is news', () => {
  assert.equal(t.stripShown({ screen: 'engine', stripUntil: 0, note: null }, 5000), true);
  assert.equal(t.stripShown({ screen: 'glance', stripUntil: 0, note: null }, 5000), false);
  const until = t.stripAfterTap(1000);
  assert.equal(until, 1000 + t.STRIP_MS);
  assert.equal(t.stripShown({ screen: 'glance', stripUntil: until, note: null }, until - 1), true);
  assert.equal(t.stripShown({ screen: 'glance', stripUntil: until, note: null }, until), false);
  const note = { kind: 'change', text: 'Back home: idle', until: 9000 };
  assert.equal(t.stripShown({ screen: 'glance', stripUntil: 0, note }, 5000), true);
  // The session's own refusal is news too: the person who tapped must see why nothing changed.
  assert.equal(t.stripShown({ screen: 'glance', stripUntil: 0, note: { ...note, kind: 'refused' } }, 5000), true);
});

test('a tap on Home with the strip hidden only shows the strip; a tap on a shown tab opens it', () => {
  assert.deepEqual(t.tapAction({ shown: false, tab: null }), { strip: true });
  assert.deepEqual(t.tapAction({ shown: false, tab: 'fleet' }), { strip: true });  // a hidden tab is not there
  assert.deepEqual(t.tapAction({ shown: true, tab: 'fleet', current: 'glance' }), { open: 'fleet' });
  assert.deepEqual(t.tapAction({ shown: true, tab: 'engine', current: 'engine' }), { same: true });
  assert.deepEqual(t.tapAction({ shown: true, tab: null, current: 'glance' }), { strip: true });
  assert.deepEqual(t.tapAction({ shown: true, tab: null, current: 'engine' }), {});
});

test('without a fleet ([schedule] in kiln.toml) the Fleet tab is hidden and paging skips it', () => {
  assert.equal(t.fleetShown({ schedule: { in_day: true } }), true);
  assert.equal(t.fleetShown({ schedule: null }), false);
  assert.equal(t.fleetShown(null), false);
  assert.deepEqual(t.tabsShown(true), ['glance', 'engine', 'fleet', 'today', 'settings']);
  const tabs = t.tabsShown(false);
  assert.deepEqual(tabs, ['glance', 'engine', 'today', 'settings']);
  assert.equal(t.turnTo('engine', tabs, 1), 'today');
  assert.equal(t.turnTo('today', tabs, -1), 'engine');
  assert.equal(t.turnTo('settings', tabs, 1), 'glance');
  assert.equal(t.turnTo('fleet', tabs, 1), 'engine');  // from a screen that is gone: from Home
  // A shared screen that this setup cannot show (an old panel.json, another session) shows Home.
  assert.equal(t.screenShown('fleet', tabs), 'glance');
  assert.equal(t.screenShown('today', tabs), 'today');
});
