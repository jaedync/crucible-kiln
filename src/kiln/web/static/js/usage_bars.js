// Usage bars: the period's API cost, tokens or busy time per local hour (today, 7 days) or day (30 days,
// all), stacked by the usage table's rows in their colours, biggest at the bottom. Buckets come from the
// server (/usage `buckets`, in its zone like the activity heatmap). Point at a bar, or focus the chart and
// use the arrow keys, for that bucket in the line below.
import { h, setText, segmented } from './dom.js';
import * as f from './format.js';

const HOUR = 3600;
const DENSE = 40;  // more bars than this get 1 px gaps and a minimum width that may scroll
const TOP_NAMES = 3;  // the detail line names this many rows of a bucket, then counts the rest
const HINT = 'Point at a bar, or focus the chart and use the arrow keys, for its detail.';

// niceScale(max) -> { top, step }: the y axis top and gridline step, 1, 2, 2.5 or 5 times a power of ten.
export function niceScale(max, lines = 3) {
  if (!(max > 0)) return { top: 0, step: 0 };
  const raw = max / lines;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw * (1 - 1e-9));
  return { top: Math.ceil(max / step - 1e-9) * step, step };
}

// A gridline price: $1, $1.50, $0.25, $0.005.
export function usdTick(v) {
  if (v >= 1) return `$${Number.isInteger(Math.round(v * 100) / 100) ? Math.round(v) : v.toFixed(2)}`;
  return `$${v.toFixed(4).replace(/0{1,2}$/, '')}`;
}

// A gridline duration: 15m, 1h, 2.5h.
export function durTick(s) {
  return s < HOUR ? `${Math.round(s / 60)}m` : `${Number((s / HOUR).toFixed(1))}h`;
}

// busyScale(max, step_s): an hour bar's axis is the whole hour, so a full bar reads as "busy all hour";
// a day's axis steps in whole minutes or hours.
export function busyScale(max, step) {
  if (step === HOUR) return { top: HOUR, step: HOUR / 4 };
  if (!(max > 0)) return { top: 0, step: 0 };
  const unit = max >= 2 * HOUR ? HOUR : 60;
  const s = niceScale(max / unit);
  return { top: s.top * unit, step: s.step * unit };
}

// What a bar can measure. `series` is the per-key list in /usage buckets.
export const METRICS = {
  cost: { label: 'Cost', title: 'API cost', series: 'cost', fmt: f.usd, tick: usdTick, scale: (max) => niceScale(max) },
  tokens: { label: 'Tokens', title: 'Tokens', series: 'tokens', fmt: (v) => `${f.compact(v)} tokens`, tick: f.compact,
    scale: (max) => niceScale(max) },
  busy: { label: 'Busy', title: 'Busy time', series: 'busy', fmt: (v) => `${f.dur(v)} busy`, tick: durTick, scale: busyScale },
};

const parse = (local) => {
  const [date, time] = String(local).split(' ');
  return { date, hour: time ? Number(time.slice(0, 2)) : null, day: new Date(`${date}T12:00:00`) };
};
const md = (d) => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
const wd = (d) => d.toLocaleDateString(undefined, { weekday: 'short' });
const hh = (n) => `${String(n).padStart(2, '0')}:00`;

// axisLabels(local, step_s) -> [{ i, text, major }]: every third hour of one day; each day's start over
// several days; first day, month starts and Mondays for days (as the heatmap labels its columns).
export function axisLabels(local, step) {
  const ps = local.map(parse);
  if (step === HOUR && ps.length <= 26) {
    return ps.flatMap((p, i) => (p.hour % 3 === 0 && (i === 0 || ps[i - 1].hour !== p.hour)
      ? [{ i, text: String(p.hour).padStart(2, '0'), major: p.hour === 0 || p.hour === 12 }] : []));
  }
  if (step === HOUR) {
    return ps.flatMap((p, i) => (p.hour === 0 && (i === 0 || ps[i - 1].date !== p.date)
      ? [{ i, text: `${wd(p.day)} ${p.day.getDate()}`, major: true }] : []));
  }
  return ps.flatMap((p, i) => {
    const first = i === 0 || (p.day.getDate() === 1 && i < ps.length - 3);
    if (!first && p.day.getDay() !== 1) return [];
    return [{ i, text: first ? md(p.day) : String(p.day.getDate()), major: first }];
  });
}

// bucketName(local, step_s, many) -> "14:00–15:00", "Wed Oct 15 · 14:00–15:00" or "Wed Oct 15".
export function bucketName(local, step, many = false) {
  const p = parse(local);
  const day = `${wd(p.day)} ${md(p.day)}`;
  if (step !== HOUR) return day;
  const span = `${hh(p.hour)}–${hh((p.hour + 1) % 24)}`;
  return many ? `${day} · ${span}` : span;
}

// stacks(buckets, keys, metric) -> [{ total, parts: [{ key, value, own }] }] in the order of `keys` (the
// table's rows). Cost and tokens add up. Busy time does not: two keys busy at once count once in the
// bucket (busy_all), so each key's part is its share of that and `own` keeps its own busy time.
export function stacks(b, keys, metric = 'cost') {
  const series = b[METRICS[metric].series] || {};
  return b.t.map((_, i) => {
    const parts = keys.map((key) => ({ key, own: (series[key] || [])[i] || 0 })).filter((p) => p.own > 0);
    const sum = parts.reduce((a, p) => a + p.own, 0);
    const total = metric === 'busy' ? Math.min(sum, (b.busy_all || [])[i] ?? sum) : sum;
    const share = metric === 'busy' && sum ? total / sum : 1;
    return { total, parts: parts.map((p) => ({ ...p, value: p.own * share })) };
  });
}

// nowIndex(t, now) -> the bucket holding now, or -1
export function nowIndex(t, now) {
  for (let i = t.length - 1; i >= 0; i -= 1) if (t[i] <= now) return i;
  return -1;
}

// detailText(b, i, metric, meta) -> the line for one bucket: its three totals, then its top rows by metric.
export function detailText(b, i, metric, meta) {
  const sumAt = (series) => Object.values(series || {}).reduce((a, v) => a + (v[i] || 0), 0);
  const busy = b.busy_all ? b.busy_all[i] : sumAt(b.busy);
  const head = [bucketName(b.local[i], b.step_s, true), METRICS.cost.fmt(sumAt(b.cost)),
    METRICS.tokens.fmt(sumAt(b.tokens)), METRICS.busy.fmt(busy), f.plural(b.requests[i] || 0, 'request')];
  const series = b[METRICS[metric].series] || {};
  const named = [...meta.keys()].map((key) => ({ key, v: (series[key] || [])[i] || 0 })).filter((p) => p.v > 0)
    .sort((x, y) => y.v - x.v);
  const listed = named.slice(0, TOP_NAMES).map((p) => `${meta.get(p.key).name} ${METRICS[metric].fmt(p.v)}`);
  if (named.length > TOP_NAMES) listed.push(`+${named.length - TOP_NAMES} more`);
  return [...head, ...listed].join(' · ');
}

export function usageBars() {
  let metric = 'cost';
  const title = h('p', { class: 'kicker' });
  const pickMetric = segmented('Bars show', Object.entries(METRICS).map(([k, m]) => [k, m.label]), metric, (v) => {
    metric = v;
    if (last) render(...last);
  });
  const grid = h('div', { class: 'ub-grid', 'aria-hidden': 'true' });
  const cols = h('div', { class: 'ub-cols' });
  const plot = h('div', { class: 'ub-plot', tabindex: '0', role: 'img' }, grid, cols);
  const axis = h('div', { class: 'ub-axis', 'aria-hidden': 'true' });
  const frame = h('div', { class: 'ub-frame' }, plot, axis);
  const detail = h('p', { class: 'ub-detail', 'aria-live': 'polite' }, HINT);
  const scroller = h('div', { class: 'ub-scroll scroll-x' }, frame);
  const el = h('div', { class: 'usage-bars' }, h('div', { class: 'ub-head' }, title, pickMetric.el), scroller, detail);
  let colEls = [];
  let texts = [];
  let rest = HINT;  // the line while no bar is picked: the peak, then how to pick one
  let pick = null;
  let last = null;
  let shownPeriod = null;  // a new period scrolls a narrow screen to its newest bars

  function show(i) {
    pick = i;
    colEls.forEach((c, j) => c.classList.toggle('is-picked', j === i));
    cols.classList.toggle('has-pick', i !== null);
    setText(detail, i === null ? rest : texts[i]);
  }

  // render(buckets, rows, color(row, i), now, period): rows in the table's order, so colours match it.
  function render(b, rows, color, now, period) {
    if (!b || !b.t) return;
    last = [b, rows, color, now, period];
    const m = METRICS[metric];
    const step = b.step_s;
    const many = b.t.length > 26;
    const keys = rows.map((r) => r.key || 'unknown');
    const meta = new Map(rows.map((r, i) => [r.key || 'unknown', { name: r.name || r.key, color: color(r, i) }]));
    const st = stacks(b, keys, metric);
    const max = Math.max(0, ...st.map((s) => s.total));
    const { top, step: gstep } = m.scale(max, step);
    const nowAt = nowIndex(b.t, now);
    const unit = step === HOUR ? 'hour' : 'day';
    setText(title, `${m.title} per ${unit}`);
    const peakAt = max > 0 ? st.findIndex((s) => s.total === max) : -1;
    rest = peakAt < 0 ? 'Nothing in this period yet.' : `Peak ${bucketName(b.local[peakAt], step, many)} · ${m.fmt(max)}. ${HINT}`;
    texts = st.map((_, i) => detailText(b, i, metric, meta));
    colEls = st.map((s, i) => {
      const segs = s.parts.map((p) => h('i', { style: {
        height: `max(1px, ${((p.value / top) * 100).toFixed(3)}%)`, background: meta.get(p.key).color } }));
      const c = h('div', { class: i === nowAt ? 'ub-col is-now' : 'ub-col' }, segs);
      c.addEventListener('mouseenter', () => show(i));
      return c;
    });
    frame.classList.toggle('is-dense', b.t.length > DENSE);
    frame.style.setProperty('--n', String(b.t.length));
    cols.replaceChildren(...colEls);
    const labels = axisLabels(b.local, step);
    const majors = new Set(labels.filter((l) => l.major && l.i > 0).map((l) => l.i));
    colEls.forEach((c, i) => c.classList.toggle('is-major', many && majors.has(i)));
    axis.replaceChildren(...labels.map((l) => h('span', {
      class: `ub-tick${l.major ? ' is-major' : ''}${many ? ' at-start' : ''}`, style: { gridColumn: `${l.i + 1}` } }, l.text)));
    const lines = [];
    for (let v = gstep; gstep > 0 && v <= top + 1e-9; v += gstep) {
      lines.push(h('span', { class: 'ub-line', style: { bottom: `${((v / top) * 100).toFixed(3)}%` } }, h('span', null, m.tick(v))));
    }
    grid.replaceChildren(...lines);
    plot.setAttribute('aria-label', peakAt < 0 ? `${m.title} per ${unit}: none in this period`
      : `${m.title} per ${unit}, ${b.t.length} bars. Peak ${bucketName(b.local[peakAt], step, true)}: ${m.fmt(max)}.`);
    show(pick !== null && pick < colEls.length ? pick : null);
    if (shownPeriod !== period) scroller.scrollLeft = scroller.scrollWidth;
    shownPeriod = period;
  }

  cols.addEventListener('mouseleave', () => { if (document.activeElement !== plot) show(null); });
  plot.addEventListener('focus', () => { if (pick === null && colEls.length) show(Math.max(0, colEls.findIndex((c) => c.classList.contains('is-now')))); });
  plot.addEventListener('blur', () => show(null));
  plot.addEventListener('keydown', (e) => {
    const d = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity }[e.key];
    if (d === undefined || !colEls.length) return;
    e.preventDefault();
    show(Math.min(colEls.length - 1, Math.max(0, (pick === null ? colEls.length - 1 : pick) + d)));
  });
  return { el, render };
}
