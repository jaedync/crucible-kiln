// Live hero: decode tok/s, slot use, active streams, and the last request.
// Rates are printed exactly as the server computed them on each tick, with no
// tweening; elapsed clocks tick locally because a clock is not an estimate.
// Token counts stream between ticks at each stream's rate (count.js).
import { h, setText, setAttr, toggleClass } from '../dom.js';
import * as f from '../format.js';
import { sameRequest, lastFromRow } from '../rows.js';
import { estimatePrefill, holdPrefill, prefillLine, prefillShare } from '../progress.js';
import { streamCount } from '../count.js';
import { placeStreams } from '../slots.js';
import { streamDraft, draftOfRequest } from '../draft.js';
import { daySpeedView } from '../day_speed.js';
import { prefillNow } from '../prefill_now.js';

const PREFILL_ESTIMATE_NOTE =
  'Estimated. The engine reports prefill progress after each chunk (Crucible: 4,096 tokens, Halogen: ' +
  '32,768); between reports the count moves on at the rate of this prompt\'s own reports. A cached ' +
  'start from a restored conversation is estimated too.';
const FIRST_REPORT_NOTE =
  'Estimated from the recent prefill rate. The prompt size is known at the engine\'s first report.';
// Before this, a stream without an estimate is just starting; after it, the engine has said nothing.
const STARTING_S = 2;

export const ESTIMATE_NOTE =
  'Estimated. This engine sends a different number of stream events than tokens, so live counts ' +
  'are scaled by its measured token/event ratio. Totals are exact once each request ends.';

const CLOCK_MS = 50;  // clocks, prefill and token counts: 20 updates a second
// Rows the stream list keeps before the first tick says how many slots the engine has: its usual count,
// so the first frame already has the panel's final height.
const DEFAULT_SLOTS = 4;
const DEAD_TEXT = {
  connecting: 'Connecting to Kiln…',
  reconnecting: 'Connection lost. Reconnecting…',
  stale: 'No live data for a few seconds.',
};

export default {
  id: 'hero',
  label: 'Live generation',
  mount(el, { store }) {
    const rate = rateBlock();
    const streams = streamsBlock(store);
    const last = lastBlock();
    el.append(rate.el, streams.el, last.el);

    function render() {
      const { tick } = store.get();
      const live = store.isLive() && tick ? tick.live : null;
      const now = store.now();
      rate.render(live, store.get().conn, tick, now);
      streams.render(live, now, tick ? tick.ts : now);
      last.render(lastOf(tick), now);
      document.body.dataset.activity = activityOf(live);
    }

    // The engine-enriched copy of the last request can arrive as a `request`
    // event before tick.last catches up; prefer it while it describes the same request.
    let enriched = null;
    const lastOf = (tick) => {
      const l = tick ? tick.last : null;
      return enriched && sameRequest(l, enriched) ? lastFromRow(enriched) : l;
    };
    store.on('request', (r) => {
      if (r && sameRequest(store.get().tick && store.get().tick.last, r)) {
        enriched = r;
        render();
      }
    });
    store.on('tick', render);
    store.on('conn', render);
    setInterval(() => {
      const live = store.isLive() && store.get().tick ? store.get().tick.live : null;
      rate.clock(live, store.now());
      streams.clocks(live, store.now());
      last.age(store.now());
    }, CLOCK_MS);
    render();
  },
};

function activityOf(live) {
  if (!live || !live.streams.length) return 'idle';
  return live.streams.some((s) => s.phase === 'decode') ? 'decode' : 'prefill';
}

function rateBlock() {
  const num = h('span', { class: 'hero-num' }, f.DASH);
  const value = h('p', { class: 'hero-value', 'aria-live': 'off' }, num);
  const state = h('p', { class: 'hero-state' });
  const slots = h('div', { class: 'slots' });
  const slotText = h('span', { class: 'slot-text' });
  const day = dayTable();
  const pf = prefillReadout();
  const el = h('div', { class: 'hero-rate' },
    h('p', { class: 'kicker' }, 'Decode · tokens per second · now'),
    h('div', { class: 'hero-top' }, value, pf.el),
    state,
    day.el,
    h('div', { class: 'slot-row' }, h('span', { class: 'kicker' }, 'In flight'), slotText, slots));

  // Whether the active engine's live counts are estimates (engine_exact holds while nothing streams).
  let inexact = false;

  function render(live, conn, tick, now) {
    toggleClass(el, 'is-dead', !live);
    if (live) inexact = live.engine_exact === undefined ? !live.decode_exact : !live.engine_exact;
    // No ≈ before the figure: its width pushed the prefill readout to a second line.
    // The hover text says when the figure is an estimate; with nothing streaming the rate is exactly 0.
    setAttr(value, 'title', live && inexact && live.streams.length ? noteOf(live) : null);
    setText(num, live ? f.rate(live.decode_tps) : f.DASH);
    setText(state, live ? stateLine(live, tick) : DEAD_TEXT[conn] || DEAD_TEXT.connecting);
    renderSlots(live);
    day.render(tick);
    clock(live, now);
  }

  // The prefill readout moves on between ticks, like the stream rows' counts.
  function clock(live, now) {
    const view = pf.render(live ? live.streams : [], now);
    toggleClass(el, 'is-reading', Boolean(view && view.onlyPrefill));
  }

  function renderSlots(live) {
    const total = live ? live.slots : 0;
    const used = live ? live.in_flight : 0;
    const slotWord = total ? `${used} / ${total} slots${used > total ? ` · ${used - total} queued` : ''}` : 'no engine slots';
    setText(slotText, live ? slotWord : f.DASH);
    const phases = live ? live.streams.map((s) => s.phase) : [];
    // One cell per slot: the queue is in the text, so the row never grows a second line.
    const cells = [];
    for (let i = 0; i < total; i++) cells.push(h('i', { class: `slot ${phases[i] || (i < used ? 'busy' : '')}` }));
    slots.replaceChildren(...cells);
    setAttr(slots, 'role', 'img');
    setAttr(slots, 'aria-label', live ? `${used} of ${total} slots in use` : 'unknown');
  }

  return { el, render, clock };
}

// Beside the decode figure, in space it leaves free: how fast the engine reads prompts now
// (prefill_now.js). Hidden while no prompt needs PREFILL_SHOW_S or more.
function prefillReadout() {
  // No ≈ here either, for the same room as the decode figure's: the hover text names an estimate.
  const num = h('span');
  const fig = h('p', { class: 'pf-v' }, num);
  const fill = h('i');
  const sub = h('p', { class: 'pf-sub' });
  const el = h('div', { class: 'hero-pf', hidden: true },
    h('p', { class: 'kicker' }, 'Prefill', h('span', { class: 'pf-unit' }, ' · tok/s')),
    fig, h('span', { class: 'pf-bar', 'aria-hidden': 'true' }, fill), sub);
  let memos = new Map();

  function render(streams, now) {
    const out = prefillNow(streams, now, memos);
    memos = out.memos;
    el.hidden = !out.view;
    if (out.view) {
      setText(num, out.view.rate);
      setText(sub, out.view.sub);
      const width = `${Math.round(out.view.share * 100)}%`;
      if (fill.style.width !== width) fill.style.width = width;
      setAttr(el, 'title', out.view.title);
    }
    return out.view;
  }

  return { el, render };
}

// Today's speeds and TTFT of the serving engine (day_speed.js): built once, its figures set on each tick.
function dayTable() {
  const cell = (cls) => h('span', { class: cls });
  const rows = daySpeedView(null, null).rows.map((r) => ({ ...r, avg: cell('hd-num hd-avg'), p5: cell('hd-num'), p95: cell('hd-num') }));
  const el = h('div', { class: 'hero-day' },
    h('span', { class: 'kicker' }, 'Today'),
    ...['avg', 'p5', 'p95'].map((k) => h('span', { class: 'kicker hd-num' }, k)),
    ...rows.flatMap((r) => [h('span', null, r.label, r.unit ? h('small', { class: 'hd-unit' }, ` ${r.unit}`) : null),
      r.avg, r.p5, r.p95]));
  function render(tick) {
    const v = daySpeedView(tick && tick.today, tick && tick.engine);
    v.rows.forEach((r, i) => ['avg', 'p5', 'p95'].forEach((k) => setText(rows[i][k], r[k])));
    setAttr(el, 'title', v.title);
  }
  return { el, render };
}

export function noteOf(live) {
  return (live && live.decode_note) || ESTIMATE_NOTE;
}

// GPU use above this with no gateway streams means traffic that bypassed the gateway.
const DIRECT_BUSY_PCT = 50;

export function stateLine(live, tick) {
  const dec = live.streams.filter((s) => s.phase === 'decode').length;
  const queued = live.streams.filter((s) => s.queued).length;  // past the slots: in the engine's queue
  const pre = live.streams.length - dec - queued;
  if (!live.streams.length) {
    const busy = tick && tick.hw && tick.hw.gpu_busy_pct >= DIRECT_BUSY_PCT;
    const serving = tick && tick.engine && tick.engine.state === 'ready';
    if (busy) return `GPU ${Math.round(tick.hw.gpu_busy_pct)}% busy outside the gateway (an eval?). Counted as each request ends.`;
    return serving ? 'Idle. Nothing is streaming.' : 'Idle. No engine is serving.';
  }
  const parts = [];
  if (dec) parts.push(`${dec} decoding`);
  if (pre) parts.push(`${pre} prefilling`);
  if (queued) parts.push(`${queued} queued`);
  return parts.join(' · ');
}

// One row per engine slot, always: the list (and the panel) keeps its height whether 0 or 4 streams run,
// and a stream keeps its row while it runs, so the others never move. Streams past the slots wait in the
// engine's queue; one line below the rows names them.
function streamsBlock(store) {
  const list = h('ol', { class: 'streams' });
  const more = h('span', { class: 'streams-more' });
  const el = h('div', { class: 'hero-streams' }, h('p', { class: 'kicker streams-head' }, h('span', null, 'Active streams'), more),
    list);
  let rows = [];  // streamRow per slot
  let placed = new Map();  // stream id -> row index

  function size(n) {
    if (n === rows.length) return;
    rows = Array.from({ length: n }, (_, i) => rows[i] || streamRow(store));
    list.replaceChildren(...rows.map((r) => r.el));
  }

  function render(live, now, tickTs) {
    size(Math.max(1, (live && live.slots) || rows.length || DEFAULT_SLOTS));
    const streams = live ? live.streams : [];
    const { placed: next, waiting } = placeStreams(placed, streams, rows.length);
    placed = next;
    const byRow = new Map([...placed].map(([id, i]) => [i, streams.find((s) => s.id === id)]));
    rows.forEach((r, i) => (byRow.has(i) ? r.update(byRow.get(i), live.decode_exact, noteOf(live), now, tickTs) : r.clear()));
    setText(more, !live ? 'Unknown while disconnected.'
      : waiting.length ? `+${waiting.length} queued: ${waiting.map((s) => f.clientName(s.client)).join(', ')}` : '');
  }

  function clocks(live, now) {
    if (!live) return;
    for (const s of live.streams) {
      const i = placed.get(s.id);
      if (i != null) rows[i].clock(s, now);
    }
  }

  size(DEFAULT_SLOTS);
  return { el, render, clocks };
}

// decodeMeta(stream, now) -> the decoding row's meta line. The TTFT and the decode time come first: the line
// ends in an ellipsis in a narrow row, and the model name is the same on every row (the engine card shows it).
export function decodeMeta(s, now) {
  const label = s.client && s.client.label ? ` · ${s.client.label}` : '';
  return `TTFT ${f.secs(s.first_token - s.started)} · ${f.secs(now - s.first_token)}${label} · ${s.model}`;
}

function streamRow(store) {
  const swatch = h('i', { class: 'swatch' });
  const name = h('span', { class: 'st-name' });
  const meta = h('span', { class: 'st-meta' });
  const phase = h('span', { class: 'phase' });
  const big = h('span', { class: 'st-big' });
  const sub = h('span', { class: 'st-sub' });
  const prog = progCells();
  const draftPct = h('b');
  const draftFill = h('i');
  const draft = h('span', { class: 'st-draft' }, h('small', null, 'Draft'), draftPct,
    h('span', { class: 'dg-bar', 'aria-hidden': 'true' }, draftFill));
  const bar = h('span', { class: 'bar' }, h('i'));
  const el = h('li', { class: 'stream' }, swatch,
    h('span', { class: 'st-id' }, name, meta),
    h('span', { class: 'st-phase' }, phase),
    h('span', { class: 'st-nums' }, big, sub, prog.el), draft, bar);
  let id = null;

  // A free slot: the row stays, with its text blank, so the list keeps its height.
  function clear() {
    if (el.dataset.phase === 'free') return;
    id = null;
    held = null;
    count = null;
    el.dataset.phase = 'free';
    swatch.style.background = '';
    for (const x of [name, meta, phase, big, sub]) setText(x, '');
    setAttr(big, 'title', null);
    setAttr(sub, 'title', null);
    setText(name, 'Free slot');
    showDraft(null);
    prog.hide();
    toggleClass(bar, 'is-off', true);
  }

  function update(s, exact, note, now, tickTs) {
    const pre = s.phase === 'prefill';
    if (s.id !== id) {  // a new stream in this row: its own colour and counts
      id = s.id;
      held = null;
      count = null;
      swatch.style.background = store.colorFor(s.client);
    }
    [shownExact, tickAt] = [exact, tickTs];
    el.dataset.phase = s.phase;
    setText(name, f.clientName(s.client));
    setText(meta, s.client && s.client.label ? `${s.model} · ${s.client.label}` : s.model);
    setAttr(meta, 'title', null);
    setText(phase, pre ? 'Prefill' : 'Decode');
    // A decode row only: drafts run as it decodes, and a prefill row needs the room for its progress.
    showDraft(pre ? null : streamDraft(s));
    if (!pre) toggleClass(bar, 'is-off', true);
    if (!pre && !s.tokens) {
      // The first token is out but none is counted yet: a rate of "≈0" would be noise.
      setText(big, 'First token');
      setText(sub, 'counting…');
      setAttr(big, 'title', null);
      setAttr(sub, 'title', null);
    } else if (!pre) {
      setText(big, `${exact ? '' : '≈'}${f.rate(s.tps)} tok/s`);
      setAttr(big, 'title', exact ? null : note);
      setAttr(sub, 'title', null);
    }
    clock(s, now);
  }

  // The row's data-draft band colours the meter and gives it a grid cell (css .st-draft).
  function showDraft(chip) {
    setAttr(el, 'data-draft', chip ? chip.band : null);
    setText(draftPct, chip ? chip.text : '');
    setAttr(draft, 'title', chip ? chip.title : null);
    const w = chip ? `${chip.pct}%` : '0%';
    if (draftFill.style.width !== w) draftFill.style.width = w;
  }

  // The count never moves back, even if a report lands short of the estimate (progress.js).
  let held = null;
  // Decode: the last tick's time and exactness, and the token count shown (it streams between ticks).
  let tickAt = 0;
  let shownExact = true;
  let count = null;  // count.js: { count, at }

  // Prefill shows elapsed time and the prompt read so far; decode shows time since the first token.
  function clock(s, now) {
    if (s.phase === 'prefill') {
      setText(big, f.secs(now - s.started));
      setAttr(big, 'title', 'Time since the request started');
      let est = estimatePrefill(s.prefill_progress, now);
      if (est) ({ est, memo: held } = holdPrefill(est, held));
      toggleClass(bar, 'is-off', !(est && est.of));
      if (est && est.of != null) {
        bar.firstChild.style.width = `${100 * prefillShare(est.read, est.of)}%`;
        setText(sub, '');
        setAttr(sub, 'title', null);
        prog.show(prefillLine(est), PREFILL_ESTIMATE_NOTE);
        return;
      }
      prog.hide();
      if (!est) {
        setText(sub, s.queued ? 'waiting for a slot' : now - s.started < STARTING_S ? 'starting…' : 'no progress reported');
        setAttr(sub, 'title', null);
      } else {
        setText(sub, `≈${f.int(est.read)} tokens read`);
        setAttr(sub, 'title', FIRST_REPORT_NOTE);
      }
    } else {
      prog.hide();
      setText(meta, decodeMeta(s, now));
      setAttr(meta, 'title', decodeMeta(s, now));
      if (s.tokens) {
        count = streamCount(s, tickAt, now, count);
        setText(sub, `${shownExact ? '' : '≈'}${f.int(count.count)} tokens`);
      }
    }
  }

  clear();
  return { el, update, clear, clock };
}

// A prefill's progress in fixed cells (css .st-prog): the count and its share, then the cached start and
// the time left. The count's box is as wide as its digits in zeros, so nothing after it moves as it climbs.
function progCells() {
  const size = h('span', { class: 'pg-size', 'aria-hidden': 'true' });
  const read = h('span');
  const of = h('span');
  const unit = h('span', { class: 'pg-unit' });
  const share = h('span', { class: 'pg-pct' });
  const cached = h('span', { class: 'pg-cached' });
  const left = h('span', { class: 'pg-left' });
  const count = h('span', { class: 'pg-count' }, h('span', { class: 'pg-fit' }, size, read), ' / ', of, ' ', unit);
  const el = h('span', { class: 'st-prog', hidden: true },
    h('span', { class: 'pg-row' }, count, share), h('span', { class: 'pg-row' }, cached, left));
  return {
    el,
    show(p, note) {
      if (el.hidden) el.hidden = false;
      setAttr(el, 'title', p.read.startsWith('≈') ? note : null);
      [[size, p.size], [read, p.read], [of, p.of], [unit, p.unit], [share, p.pct], [cached, p.cached], [left, p.left]]
        .forEach(([x, text]) => setText(x, text));
    },
    hide() { if (!el.hidden) el.hidden = true; },
  };
}

function lastBlock() {
  const ageEl = h('span', { class: 'last-age' });
  const ttft = h('span', { class: 'lv' });
  const pre = h('span', { class: 'lv' });
  const dec = h('span', { class: 'lv' });
  const draftV = h('span', { class: 'lv' });
  const draftRow = h('div', { class: 'last-draft', hidden: true }, h('dt', null, 'Drafts accepted'), h('dd', null, draftV));
  const detail = h('p', { class: 'last-detail' });
  // Plain sentence, not a badge: a bordered "STOP" read as a Stop button.
  const finish = h('p', { class: 'finish' });
  const el = h('div', { class: 'hero-last' },
    h('p', { class: 'kicker' }, h('span', null, 'Last request'), ageEl),
    h('dl', { class: 'last-grid' },
      h('div', null, h('dt', null, 'TTFT'), h('dd', null, ttft)),
      h('div', null, h('dt', null, 'Prefill tok/s'), h('dd', null, pre)),
      h('div', null, h('dt', null, 'Decode tok/s'), h('dd', null, dec)), draftRow),
    detail, finish);
  let ts = null;

  function render(last, now) {
    ts = last ? last.ts : null;
    setText(ttft, last ? f.secs(last.ttft_s) : f.DASH);
    setText(pre, last ? f.estRate(last.prefill_tps, last.prefill_estimated) : f.DASH);
    setText(dec, last ? f.estRate(last.decode_tps, last.decode_estimated) : f.DASH);
    setAttr(pre, 'title', last && last.prefill_estimated ? f.ESTIMATED_RATE : null);
    setAttr(dec, 'title', last && last.decode_estimated ? f.ESTIMATED_RATE : null);
    showDraft(last ? draftOfRequest(last) : null);
    setText(detail, last
      ? `${f.int(last.prompt)} prompt (${f.int(last.cached)} cached) · ${f.int(last.completion)} output`
      : 'No request has finished yet.');
    const info = last ? f.finishInfo(last) : null;
    setText(finish, info ? `Finished: ${info.text}${last.source === 'direct' ? ' · direct (bypassed the gateway)' : ''}` : '');
    setAttr(finish, 'title', info ? info.title : null);
    finish.dataset.kind = info && info.bad ? 'bad' : '';
    age(now);
  }

  // Only a request with draft counts has the row: an engine without speculation never shows it. Plain
  // text like the rest of the block: no meter and no band colour here.
  function showDraft(v) {
    if (draftRow.hidden !== !v) draftRow.hidden = !v;
    setText(draftV, v ? v.text : '');
    setAttr(draftV, 'title', v ? v.title : null);
  }

  function age(now) {
    setText(ageEl, ts ? f.ago(ts, now) : '');
  }

  return { el, render, age };
}
