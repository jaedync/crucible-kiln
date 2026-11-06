// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { draftChip, draftOfRequest, streamDraft, HIGH_PCT, LOW_PCT, MIN_DRAFTS } from '../../src/kiln/web/static/js/draft.js';

const d = (accepted, proposed = 1000, turns = 5) => ({ accepted, proposed, turns });

test('draft chip: the share accepted, coloured by band', () => {
  assert.deepEqual([draftChip(d(820)).text, draftChip(d(820)).band], ['82%', 'high']);
  assert.equal(draftChip(d(700)).band, 'mid');
  assert.equal(draftChip(d(550)).band, 'low');
});

test('draft chip: the band follows the shown percentage, so a figure never shows in two colours', () => {
  assert.equal(draftChip(d(10 * HIGH_PCT - 0.4)).text, `${HIGH_PCT}%`);
  assert.equal(draftChip(d(10 * HIGH_PCT - 0.4)).band, 'high');  // 74.96% shows as 75%: high
  assert.equal(draftChip(d(10 * HIGH_PCT - 6)).band, 'mid');  // 74.4% shows as 74%
  assert.equal(draftChip(d(10 * LOW_PCT)).band, 'mid');
  assert.equal(draftChip(d(10 * LOW_PCT - 6)).band, 'low');
});

test('draft chip: nothing before a finished turn, or with too few drafts to colour', () => {
  assert.equal(draftChip(null), null);
  assert.equal(draftChip(undefined), null);
  assert.equal(draftChip(d(10, MIN_DRAFTS - 1, 1)), null);
  assert.ok(draftChip(d(10, MIN_DRAFTS, 1)));
  assert.equal(draftChip({ accepted: null, proposed: 100, turns: 1 }), null);
});

test('draft chip: the title gives the counts and says the live turn is not in them', () => {
  const t = draftChip(d(1234, 1700, 5)).title;
  assert.match(t, /1,234 of 1,700/);
  assert.match(t, /last 5 turns/);
  assert.match(draftChip(d(80, 100, 1)).title, /last turn\b/);
  assert.match(t, /current turn/);
});

test('draft chip: the meter takes the shown percentage', () => {
  assert.equal(draftChip(d(820)).pct, 82);
  assert.equal(draftChip(d(10 * HIGH_PCT - 0.4)).pct, HIGH_PCT);
});

test('draft of a request: its own counts, exact, with no turns', () => {
  const v = draftOfRequest({ draft_accepted: 360, draft_proposed: 480 });
  assert.deepEqual([v.text, v.band, v.pct], ['75%', 'high', 75]);
  assert.match(v.title, /^Drafts accepted in this request: 360 of 480\.$/);
  assert.equal(draftOfRequest({ draft_accepted: null, draft_proposed: null }), null);  // an engine without counts
  assert.equal(draftOfRequest({ draft_accepted: 0, draft_proposed: 0 }), null);  // a reply with no draft round
  assert.equal(draftOfRequest({ draft_accepted: 9, draft_proposed: 8 }), null);  // counts that cannot be
  assert.equal(draftOfRequest(null), null);
});

test('draft of a short request: exact even with few drafts, plain text with the counts', () => {
  const v = draftOfRequest({ draft_accepted: 15, draft_proposed: 20 });
  assert.deepEqual([v.text, v.title], ['75%', 'Drafts accepted in this request: 15 of 20.']);
});

test('a stream: its running turn once it has enough drafts, else its conversation', () => {
  const conv = { accepted: 3593, proposed: 4999, turns: 5 };
  const live = streamDraft({ draft: conv, draft_turn: { accepted: 48, proposed: 60 } });
  assert.deepEqual([live.text, live.band, live.live], ['80%', 'high', true]);
  assert.equal(live.title, 'Drafts accepted in this turn so far: 48 of 60.');
  // Too few in the turn yet (its first second): the conversation's last turns stand in.
  const early = streamDraft({ draft: conv, draft_turn: { accepted: 9, proposed: MIN_DRAFTS - 1 } });
  assert.deepEqual([early.text, early.live], ['72%', undefined]);
  assert.match(early.title, /last 5 turns/);
  assert.equal(streamDraft({ draft: null, draft_turn: null }), null);
  assert.equal(streamDraft({}), null);  // an engine without draft counts
});
