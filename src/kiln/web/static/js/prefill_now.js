// The decode panel's prefill readout: how fast the engine reads prompts now, beside the decode figure.
// While a long prompt prefills alone the decode figure is 0 with the GPU at full speed, and beside a
// decode it tells why decode slowed down. It shows only for a prompt that needs PREFILL_SHOW_S or more
// in all: most prompts are almost all cached, and a readout that comes and goes within a second is noise.
// The figure adds up the rates of the prompts the engine reads (each its share of the engine); the meter
// and the time left are the prompt that ends last's. The prompt count is in the state line beside it.
import { estimatePrefill, holdPrefill, prefillShare } from './progress.js';
import * as f from './format.js';

export const PREFILL_SHOW_S = 1.5;  // see above: a shorter readout comes and goes as noise

// prefillNow(streams, now, memos) -> { view, memos }
//   view: null (nothing to show) or { rate, estimated, share, sub, title, onlyPrefill }; estimated: a rate
//     is the engine's recent one, not this prompt's reports (the title says so); onlyPrefill: no
//     stream decodes, so the decode figure's 0 steps back
//   memos: stream id -> holdPrefill memo, so the meter and the time left never move back
export function prefillNow(streams, now, memos = new Map()) {
  const next = new Map();
  const reads = [];
  for (const s of streams || []) {
    if (s.phase !== 'prefill' || s.queued) continue;
    const raw = estimatePrefill(s.prefill_progress, now);
    if (!raw || !raw.rate) continue;
    const { est, memo } = holdPrefill(raw, memos.get(s.id) || null);
    next.set(s.id, memo);
    const togo = est.of == null ? null : Math.max(0, est.of - est.read) / est.rate;
    reads.push({ est, togo, started: s.started, engine: s.prefill_progress.rate_from === 'engine' });
  }
  if (!reads.some((r) => now - r.started + (r.togo || 0) >= PREFILL_SHOW_S)) return { view: null, memos: next };

  const sized = reads.filter((r) => r.togo != null);
  const last = sized.length ? sized.reduce((a, b) => (b.togo > a.togo ? b : a))
    : reads.reduce((a, b) => (b.est.read > a.est.read ? b : a));
  const estimated = reads.some((r) => !r.engine);
  const rate = f.rate(reads.reduce((n, r) => n + r.est.rate, 0));
  const left = last.togo == null ? null : `≈${f.dur(Math.max(1, Math.ceil(last.togo)))} left`;
  const sub = left || `≈${f.int(last.est.read)} read`;
  const share = last.togo == null ? 0 : prefillShare(last.est.read, last.est.of);
  return { view: { rate, estimated, share, sub, title: titleOf(reads.length, `${estimated ? '≈' : ''}${rate}`, last, left, estimated),
    onlyPrefill: !streams.some((s) => s.phase === 'decode') }, memos: next };
}

function titleOf(n, rate, last, left, estimated) {
  const unit = last.est.cached ? 'new tokens' : 'tokens';
  return [
    `Prefill now: the engine reads ${rate} prompt tokens per second${n > 1 ? `, ${n} prompts together` : ''}.`,
    left ? `The prompt that ends last: ${f.int(last.est.read)} of ${f.int(last.est.of)} ${unit} read, ${left}.`
      : `The prompt size is known at the engine's first report: ${f.int(last.est.read)} ${unit} read so far.`,
    estimated ? 'Estimated from the recent prefill rate until the engine reports.' : '',
  ].filter(Boolean).join(' ');
}
