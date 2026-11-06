import { test } from 'node:test';
import assert from 'node:assert/strict';
import { hmIn, windowText, confirmNotes } from '../../src/kiln/web/static/js/schedule.js';

// 2026-10-26 09:00 EDT = 13:00 UTC
const NINE = Date.UTC(2026, 9, 26, 13, 0) / 1000;
const PROFILES = [{ name: 'crucible-flashnext', label: 'Crucible · Flash-Next 3.05' }];
const night = (extra = {}) => ({
  state: 'stopped', profile: null,
  schedule: { day: '09:00-18:00', tz: 'America/New_York', in_day: false, next_change: NINE,
    day_profile: 'crucible-flashnext', holds: ['schedule'], dev_jobs: [], drain_timeout_s: 900, ...extra },
});

test('times use the schedule zone, not the browser zone', () => {
  assert.equal(hmIn(NINE, 'America/New_York'), '09:00');
  assert.equal(hmIn(NINE, 'UTC'), '13:00');
});

test('night with no engine shows maintenance and the morning profile', () => {
  const t = windowText(night({ dev_jobs: [{ pid: 1 }, { pid: 2 }] }), PROFILES);
  assert.equal(t.word, 'Maintenance');
  assert.equal(t.label, 'Maintenance until 09:00. The GPU is lent to other GPU jobs.');
  assert.equal(t.detail, 'At 09:00, Kiln starts Crucible · Flash-Next 3.05. 2 other GPU jobs running now.');
});

test('night with an engine the user started keeps the state word', () => {
  const e = { ...night(), state: 'ready', profile: 'crucible-flashnext' };
  const t = windowText(e, PROFILES);
  assert.equal(t.word, null);
  assert.match(t.detail, /^Night use: the other GPU jobs are paused/);
});

test('day window and missing schedule say nothing', () => {
  assert.equal(windowText({ state: 'ready', schedule: { ...night().schedule, in_day: true } }, PROFILES), null);
  assert.equal(windowText({ state: 'ready', schedule: null }, PROFILES), null);
  assert.deepEqual(confirmNotes({ schedule: null }), { notes: [], confirm: false });
});

test('switch confirmation names the pause and the running jobs', () => {
  const { notes, confirm } = confirmNotes(night({ dev_jobs: [{ pid: 7 }] }));
  assert.equal(confirm, true);
  assert.deepEqual(notes, [
    'The GPU is lent to other GPU jobs until 09:00. This switch pauses them until you stop the engine.',
    '1 other GPU job running. Kiln waits up to 15 min for it to finish. Kiln does not stop it.',
  ]);
  assert.equal(confirmNotes(night()).confirm, false);
});
