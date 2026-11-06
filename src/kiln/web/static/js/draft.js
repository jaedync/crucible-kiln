// Draft acceptance: the drafts the target accepted, over those the engine proposed. A live stream shows its
// running turn's own counts when the engine reports them during decode (Crucible's crucible_decode line, about
// once a second) and they are enough to colour; else its conversation's last finished turns (server:
// live/drafts.py), which an engine reports as each turn ends. Its meter (css .dg-bar) is filled by band. The
// last request shows its own counts as plain text.
import * as f from './format.js';

// Bands from the 6-slot engine's served turns (in a measured run, 6,596 turns with 50 or more drafts): median 70%, 10th
// percentile 61%, 90th 82%. Low is the slowest tenth; high is about the best quarter.
export const HIGH_PCT = 75;
export const LOW_PCT = 60;
// Fewer drafts than this give a share too noisy to colour (a 20-token reply drafts about 20).
export const MIN_DRAFTS = 50;

// { pct, text, band: 'high' | 'mid' | 'low' | null } or null below `least` drafts (at least 1). The band follows the shown
// percentage, so 74.96% shows as 75% and is high; it is null below MIN_DRAFTS.
function view(accepted, proposed, least) {
  if (!(proposed >= least) || !Number.isFinite(accepted) || accepted < 0 || accepted > proposed) return null;
  const pct = Math.round((100 * accepted) / proposed);
  const band = proposed < MIN_DRAFTS ? null : pct >= HIGH_PCT ? 'high' : pct >= LOW_PCT ? 'mid' : 'low';
  return { pct, text: `${pct}%`, band };
}

// A live stream's conversation: { accepted, proposed, turns } from the tick, or null.
export function draftChip(d) {
  const v = d ? view(d.accepted, d.proposed, MIN_DRAFTS) : null;
  if (!v) return null;
  const turns = d.turns === 1 ? 'last turn' : `last ${d.turns} turns`;
  return {
    ...v,
    title: `Drafts accepted in this conversation's ${turns}: ${f.int(d.accepted)} of ${f.int(d.proposed)}. `
      + 'The current turn counts when it ends.',
  };
}

// A live stream (tick.live.streams[]): its running turn, else its conversation. `live` marks the turn's own.
export function streamDraft(s) {
  const t = s && s.draft_turn;
  const v = t ? view(t.accepted, t.proposed, MIN_DRAFTS) : null;
  if (!v) return draftChip(s ? s.draft : null);
  return { ...v, live: true, title: `Drafts accepted in this turn so far: ${f.int(t.accepted)} of ${f.int(t.proposed)}.` };
}

// A finished request (tick.last or a request row): its own counts, exact even when few, or null for an engine
// without them.
export function draftOfRequest(r) {
  const v = r ? view(r.draft_accepted, r.draft_proposed, 1) : null;
  return v && { ...v, title: `Drafts accepted in this request: ${f.int(r.draft_accepted)} of ${f.int(r.draft_proposed)}.` };
}
