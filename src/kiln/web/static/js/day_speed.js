// The decode panel's "Today" table: the serving engine's decode and prefill speeds and its TTFT since midnight
// (tick.today.speeds, server: store/speeds.py). A rate's avg is all tokens over all seconds; TTFT's is the plain
// mean of the waits. p5 and p95 bound the middle 90% of the requests, so one odd request does not set them.
// Other engines of the day are in the hover text only: their speeds are not this engine's.
import * as f from './format.js';

// [key, label, unit, format]: the rates in tok/s; TTFT's figures carry their own unit (ms, s).
const ROWS = [['decode', 'Decode', 'tok/s', f.rate], ['prefill', 'Prefill', 'tok/s', f.rate], ['ttft', 'TTFT', '', f.secs]];
// The server's rules (store/queries.py MIN_RATE_TOKENS, store/speeds.py MIN_PREFILL_RATE_TOKENS).
const DECODE_MIN = 32;
const PREFILL_MIN = 1024;

export function daySpeedView(today, engine) {
  const all = (today && today.speeds) || {};
  const profile = engine && engine.profile;
  const mine = (profile && all[profile]) || null;
  const rows = ROWS.map(([key, label, unit, fmt]) => {
    const s = mine && mine[key];
    return { label, unit, avg: fmt(s ? s.avg : null), p5: fmt(s ? s.p5 : null), p95: fmt(s ? s.p95 : null) };
  });
  return { rows, title: titleOf(all, mine, engine) };
}

function titleOf(all, mine, engine) {
  const name = (engine && (engine.label || engine.profile)) || 'the engine';
  const n = (s) => f.int(s ? s.n : 0);
  const head = mine
    ? `${name} since midnight. Decode: ${n(mine.decode)} requests with ${DECODE_MIN} or more output tokens.`
      + ` Prefill: ${n(mine.prefill)} requests with ${f.int(PREFILL_MIN)} or more new prompt tokens.`
      + ` TTFT: ${n(mine.ttft)} requests with a first token.`
    : `No request on ${name} today yet.`;
  const rule = 'avg: all tokens over all seconds; for TTFT, the mean wait. p5 and p95: 5% of requests were lower, 5% higher.';
  const others = Object.entries(all).filter(([k]) => k !== (engine && engine.profile))
    .map(([k, s]) => `${k}: decode ${f.rate(s.decode && s.decode.avg)} tok/s,`
      + ` prefill ${f.rate(s.prefill && s.prefill.avg)} tok/s`);
  return [head, rule, others.length ? `Other engines today: ${others.join('; ')}.` : ''].filter(Boolean).join(' ');
}
