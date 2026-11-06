// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as st from '../../src/kiln/web/static/js/panel/settings_model.js';

const SETTINGS = { idle_home_s: 300, manual_quiet_s: 300, min_dwell_s: 30, stable_s: 3, alarm_home: true,
  auto: { enabled: false, rules: {
    engine_switch: { on: true, screen: 'engine', hold_s: 300 },
    night_start: { on: true, screen: 'fleet', hold_s: 120 },
    day_start: { on: false, screen: 'glance', hold_s: 0 } } } };

const checked = (choices) => choices.filter((c) => c.checked).map((c) => c.label);

test('each control shows its choices with the current one checked', () => {
  const v = st.settingsView(SETTINGS, true);
  assert.equal(v.editable, true);
  assert.deepEqual(checked(v.auto.choices), ['Off']);
  assert.equal(v.auto.choices[1].note, '2 rules');  // the rules that auto turns on
  assert.deepEqual(v.idle.choices.map((c) => c.label), ['1 m', '5 m', '15 m', 'Never']);
  assert.deepEqual(checked(v.idle.choices), ['5 m']);
  assert.deepEqual(checked(v.quiet.choices), ['5 m']);
  assert.deepEqual(v.quiet.choices.map((c) => c.label), ['2 m', '5 m', '15 m', '1 h']);
  assert.deepEqual(v.rules.map((r) => [r.id, r.on, r.then]),
    [['alarm_home', true, 'Home'], ['engine_switch', true, 'Engine'], ['night_start', true, 'Fleet'],
      ['day_start', false, 'Home']]);
  assert.deepEqual(checked(st.settingsView({ ...SETTINGS, idle_home_s: 0 }, true).idle.choices), ['Never']);
});

test('a value set elsewhere that is not a choice checks no box and is named', () => {
  const v = st.settingsView({ ...SETTINGS, idle_home_s: 600 }, true);
  assert.deepEqual(checked(v.idle.choices), []);
  assert.equal(v.idle.other, 'now 10 m');
  assert.equal(st.settingsView(SETTINGS, true).idle.other, '');
});

test('without settings (an older dashboard) every control is off and not editable', () => {
  const v = st.settingsView(null, true);
  assert.equal(v.editable, false);
  assert.deepEqual(checked(v.idle.choices), []);
  assert.deepEqual(v.rules.map((r) => r.on), [false, false, false, false]);
});

test('a tap gives the smallest patch the server takes', () => {
  assert.deepEqual(st.patchFor('auto', 'on'), { auto: { enabled: true } });
  assert.deepEqual(st.patchFor('auto', 'off'), { auto: { enabled: false } });
  assert.deepEqual(st.patchFor('idle', '0'), { idle_home_s: 0 });
  assert.deepEqual(st.patchFor('idle', '900'), { idle_home_s: 900 });
  assert.deepEqual(st.patchFor('quiet', '3600'), { manual_quiet_s: 3600 });
  assert.deepEqual(st.patchFor('rule', 'alarm_home', SETTINGS), { alarm_home: false });
  assert.deepEqual(st.patchFor('rule', 'day_start', SETTINGS), { auto: { rules: { day_start: { on: true } } } });
  assert.deepEqual(st.patchFor('rule', 'engine_switch', SETTINGS), { auto: { rules: { engine_switch: { on: false } } } });
  assert.equal(st.patchFor('idle', 'soon'), null);
  assert.equal(st.patchFor('idle', '42'), null);  // only the boxes on screen
  assert.equal(st.patchFor('rule', 'prefill', SETTINGS), null);
  assert.equal(st.patchFor('colour', 'red'), null);
  assert.equal(st.patchFor('rule', 'day_start', null), null);
});

test('Screen at night offers Normal and Dim; Off is not a choice yet', () => {
  const v = st.settingsView({ ...SETTINGS, night_display: 'dim' }, true);
  assert.deepEqual(v.night.choices.map((c) => c.label), ['Normal', 'Dim']);
  assert.deepEqual(checked(v.night.choices), ['Dim']);
  assert.deepEqual(checked(st.settingsView(SETTINGS, true).night.choices), ['Normal']);  // an older dashboard
  assert.deepEqual(st.patchFor('night', 'dim'), { night_display: 'dim' });
  assert.equal(st.patchFor('night', 'off'), null);
});

test('the Alarm tile: no alarm, an alarm to mute, or a mute to end', () => {
  const NOW = 1790000000;
  assert.deepEqual(st.alarmTile({ alarm: null, mute: null, now: NOW, editable: true }),
    { state: 'none', title: 'Alarm', sub: 'no alarm now', action: null, editable: false });
  assert.deepEqual(st.alarmTile({ alarm: { kind: 'hot' }, mute: null, now: NOW, editable: true }),
    { state: 'alarm', title: 'Mute 30 min', sub: 'GPU too hot', action: 'hot', editable: true });
  assert.deepEqual(st.alarmTile({ alarm: null, mute: { kind: 'failed', until: NOW + 1500 }, now: NOW, editable: false }),
    { state: 'muted', title: 'Unmute', sub: 'Engine failed · muted 25 m', action: 'unmute', editable: false });
  // NO DATA cannot be muted: the tile has nothing to offer for it.
  assert.equal(st.alarmTile({ alarm: { kind: 'nodata' }, mute: null, now: NOW, editable: true }).action, null);
});
