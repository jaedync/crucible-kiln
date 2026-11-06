// Tokens per second. 15m is live (1-second buckets from the WebSocket); longer
// ranges come from /api/v1/series and show the average rate per bucket.
// The live view only appends: a second, its height and a start line never change once drawn
// (live/rates.py). A finished request's average column appears when it ends, stacked with the others,
// widest at the bottom (columns.js). A second blue line is a smoothed trend, because one second is noisy.
// Exact, respread counts are in the longer ranges.
// Decode and prefill are two stacked plots on one time axis, each with its own
// scale, so neither line hides the other. Their cursors are synced.
import uPlot from '../../vendor/uplot/uPlot.esm.js';
import { h, setText, setAttr, segmented, panelHead } from '../dom.js';
import * as f from '../format.js';
import { emptyBuffer, fromSeries, putSecond, fromRangeResponse, niceMax, holdMax, lastIndex, smoothRate }
  from '../series.js';
import { keepStarts, startLineAt } from '../starts.js';
import { keepColumns, columnAt, latestColumn, stackBands, stackTop } from '../columns.js';

const RANGES = [['15m', '15m'], ['1h', '1h'], ['6h', '6h'], ['24h', '24h'], ['7d', '7d'], ['30d', '30d']];
const INK = '#161616', BLUE = '#1d4f91', RED = '#d2342a', RULE = 'rgba(22,22,22,.12)';
// A request's start: a thin ink line across both plots, darker than the time grid.
const START_LINE = 'rgba(22,22,22,.5)';
// A finished request's average: a lighter blue column behind the decode line (columns.js).
const AVG_FILL = 'rgba(91,134,197,.34)', AVG_EDGE = '#5b86c5';
// The live view's trend: a brighter blue than the per-second line, on top of it. A Gaussian of sigma 4 s
// (about 9 s at half height) follows a request's start or end within a few seconds; 30 s blocks lagged.
const MEAN_BLUE = '#0b84d8', SMOOTH_SIGMA_S = 4;
const COLUMN_ROWS = 200;  // requests fetched for the live view's columns: 15 min of a busy agent
const LIVE_S = 15 * 60;
const REFRESH_MIN_MS = 15000;
// Whole-number tick steps only: half-unit ticks rounded to the same label.
const INT_INCRS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1e3, 2e3, 2500, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5, 1e6];
const FONT = '600 11px "DM Sans", system-ui, sans-serif';
const Y_AXIS_PX = 56; // both plots, so their time axes line up
const SYNC_KEY = 'kiln-tps';
const LIVE_NOTE = 'Last 15 min, by second, as it happened: a drawn second never changes. Prefill is estimated (≈) '
  + 'while it runs. A thin line marks each request\'s start; a light column is a finished request at its average '
  + 'rate, stacked widest first. The bright blue line is the smoothed rate. 1h and longer use the exact counts.';
const LIVE_NOTE_FULL = 'Decode is counted by the second each token arrived (scaled by the engine\'s tokens per stream '
  + 'event when that is not 1). Prefill is counted while it runs, at the rate of the engine\'s progress reports '
  + 'or its recent prefill rate: its exact size is known only when it ends. A yellow band marks a prompt that is '
  + 'prefilling now. The 1h and longer ranges use the exact counts, with prefill on its real interval and held-back '
  + 'tool calls spread over the engine\'s decode time, so they can differ a little from what this view drew. '
  + 'When a request ends, a light blue column spans its decode (first to last token, or the engine\'s decode time) '
  + 'at its average rate: its area is the tokens it added. Columns stack, the widest at the bottom, so the top '
  + 'of the stack is the summed rate of the requests running at that moment; a band steps where a request under '
  + 'it starts or ends, and a wider request that ends later goes under the columns already drawn. The bright '
  + 'blue line is the decode rate smoothed over about 9 s (a Gaussian, sigma 4 s); its newest seconds use only '
  + 'the past, so they move a little as the next seconds arrive.';

export default {
  id: 'chart',
  label: 'Tokens per second chart',
  mount(el, { store, api }) {
    let range = '15m';
    let live = emptyBuffer();
    let shown = { t: [], decode: [], prefill: [] };
    let starts = new Map();  // request starts in the live view: id -> time (the snapshot's, and streams since)
    let columns = new Map();  // finished requests in the live view, by id
    let host = null;  // the live view is this host's engine only
    const tops = { decode: 0, prefill: 0 };  // the live view's y axis tops: they grow, never shrink (holdMax)
    let timer = null;
    const note = h('p', { class: 'chart-note' });
    const status = h('p', { class: 'chart-status', role: 'status' });
    const seg = segmented('Chart range', RANGES, range, pick);
    const legend = chartLegend();
    const decHost = h('div', { class: 'plot' });
    const preHost = h('div', { class: 'plot' });
    // Fixed-height wrappers (css): the chart never grows to fill a tall grid row.
    const decWrap = h('div', { class: 'plot-wrap plot-dec' }, decHost);
    const preWrap = h('div', { class: 'plot-wrap plot-pre' }, preHost);
    el.append(panelHead('Tokens / second', seg.el), legend.el, decWrap, preWrap,
      h('div', { class: 'chart-foot' }, note, status));

    const onCursor = (u) => legend.render(shown, u.cursor.idx, range, columns);
    const view = { range: () => range, starts: () => starts, columns: () => columns, tops };
    const dec = new uPlot(plotOptions('decode', store, view, onCursor), [[], []], decHost);
    const pre = new uPlot(plotOptions('prefill', store, view, onCursor), [[], []], preHost);
    for (const [plot, wrap] of [[dec, decWrap], [pre, preWrap]]) {
      new ResizeObserver(() => plot.setSize({ width: wrap.clientWidth, height: wrap.clientHeight })).observe(wrap);
    }

    function draw(s) {
      const mean = range === '15m' ? smoothRate(s.t, s.decode, SMOOTH_SIGMA_S) : s.t.map(() => null);
      shown = { ...s, mean };
      dec.setData([s.t, s.decode, mean]);
      pre.setData([s.t, s.prefill]);
      legend.render(shown, null, range, columns);
    }

    // The live view's columns: the last 15 min of this host's requests on (re)connect, then each `request`
    // event.
    async function loadColumns() {
      try {
        const res = await api.getJSON('requests', { limit: COLUMN_ROWS, host });
        columns = keepColumns(columns, res.rows || [], store.now() - LIVE_S);
        drawLive();
      } catch (err) {
        console.warn('kiln: request columns not loaded', err);  // the chart works without them
      }
    }

    function drawLive() {
      if (range !== '15m') return;
      draw(live);
      setText(note, LIVE_NOTE);
      setAttr(note, 'title', LIVE_NOTE_FULL);
    }

    async function loadRange() {
      clearTimeout(timer);
      const asked = range;
      setText(status, 'Loading…');
      try {
        // The live 15 min view is this host's engine only, so longer ranges match it.
        const { tick } = store.get();
        const res = await api.getJSON('series', { range: asked, group: 'none', host: tick && tick.host });
        if (range !== asked) return;
        const s = fromRangeResponse(res);
        draw(s);
        const { step } = s;
        setText(note, `${RANGES.find((r) => r[0] === asked)[1]} · average tok/s per ${f.dur(step)} bucket`);
        setAttr(note, 'title', null);
        setText(status, '');
        timer = setTimeout(loadRange, Math.max(REFRESH_MIN_MS, step * 1000));
      } catch (err) {
        setText(status, `Couldn't load ${asked}: ${err.message}`);
        timer = setTimeout(loadRange, REFRESH_MIN_MS);
      }
    }

    function pick(value) {
      range = value;
      tops.decode = 0;
      tops.prefill = 0;
      if (range === '15m') {
        clearTimeout(timer);
        setText(status, '');
        drawLive();
      } else {
        loadRange();
      }
    }

    store.on('snapshot', (snap) => {
      live = fromSeries(snap.series_1s);
      starts = keepStarts(new Map(), (snap.series_1s && snap.series_1s.starts) || [], store.now() - LIVE_S);
      tops.decode = 0;
      tops.prefill = 0;
      host = snap.host || null;
      drawLive();
      loadColumns();
    });
    store.on('request', (r) => {
      if (!r || (host && r.host !== host)) return;
      const before = columns;
      columns = keepColumns(columns, [r], store.now() - LIVE_S);
      if (columns.size !== before.size) drawLive();
    });
    store.on('tick', (tick) => {
      if (tick.type !== 'tick') return;
      const known = starts;
      if (tick.live) starts = keepStarts(starts, tick.live.streams || [], store.now() - LIVE_S);
      // `seconds` lists every second completed since the last tick (several if
      // the server lagged); `second` alone is the older contract.
      const secs = Array.isArray(tick.seconds) ? tick.seconds : tick.second ? [tick.second] : [];
      if (!secs.length) {
        if ([...starts.keys()].some((id) => !known.has(id))) drawLive();  // a request started: show its line now
        return;
      }
      for (const sec of secs) live = putSecond(live, sec);
      drawLive();
    });
  },
};

// One line above the plots: the hovered second, or the newest one while nothing is hovered.
function chartLegend() {
  const time = h('span', { class: 'cl-time' });
  const decode = h('span', { class: 'cl-v' });
  const prefill = h('span', { class: 'cl-v' });
  const startItem = h('span', { class: 'cl-item', title: 'A thin line marks the second each request started' },
    h('i', { class: 'swatch cl-start', 'aria-hidden': 'true' }), 'Request start');
  const avg = h('span', { class: 'cl-v' });
  const avgItem = h('span', { class: 'cl-item', title: 'A finished request\'s average decode rate over its decode time' },
    h('i', { class: 'swatch cl-avg', 'aria-hidden': 'true' }), 'Request avg ', avg);
  const mean = h('span', { class: 'cl-v' });
  const meanItem = h('span', { class: 'cl-item', title: 'The decode rate smoothed over about 9 s' },
    h('i', { class: 'swatch cl-mean', 'aria-hidden': 'true' }), 'Smoothed ', mean);
  const el = h('p', { class: 'chart-legend' }, time,
    h('span', { class: 'cl-item' }, h('i', { class: 'swatch cl-dec', 'aria-hidden': 'true' }), 'Decode ', decode),
    meanItem,
    h('span', { class: 'cl-item' }, h('i', { class: 'swatch cl-pre', 'aria-hidden': 'true' }), 'Prefill ', prefill),
    avgItem, startItem);

  function render(s, idx, range, columns) {
    const hovered = idx != null && idx >= 0 && idx < s.t.length;
    const i = hovered ? idx : Math.max(lastIndex(s.decode), lastIndex(s.prefill));
    const ts = i >= 0 ? s.t[i] : null;
    const at = ts == null ? f.DASH : range === '15m' ? f.clock(ts) : f.dateTime(ts);
    setText(time, hovered ? at : `Latest · ${at}`);
    setText(decode, i >= 0 ? `${f.rate(s.decode[i])} tok/s` : f.DASH);
    // Live prefill is counted while it runs at the engine's reported rate: an estimate.
    setText(prefill, i >= 0 ? `${range === '15m' && s.prefill[i] ? '≈' : ''}${f.rate(s.prefill[i])} tok/s` : f.DASH);
    startItem.hidden = range !== '15m';
    avgItem.hidden = range !== '15m';
    meanItem.hidden = range !== '15m';
    const m = i >= 0 && s.mean ? s.mean[i] : null;
    setText(mean, m != null ? `${f.rate(m)} tok/s` : f.DASH);
    // The column under the cursor (its second's middle), or the last request to finish.
    const col = hovered ? columnAt(columns, ts + 0.5) : latestColumn(columns);
    setText(avg, col ? `${f.rate(col.avg)} tok/s` : f.DASH);
  }

  return { el, render };
}

function plotOptions(kind, store, view, onCursor) {
  const isDec = kind === 'decode';
  const getRange = view.range;
  const floor = isDec ? 10 : 100;
  const stepped = uPlot.paths.stepped({ align: 1 });
  const axis = { stroke: INK, font: FONT, grid: { stroke: RULE, width: 1 }, ticks: { stroke: RULE, width: 1 } };
  const color = isDec ? BLUE : RED;
  return {
    width: 600, height: isDec ? 260 : 120,
    padding: [10, 14, 0, 0],
    cursor: { sync: { key: SYNC_KEY }, drag: { x: false, y: false }, points: { size: 7 } },
    legend: { show: false },
    scales: {
      x: { time: true },
      // Live: the top grows for a new peak and holds, so drawn seconds keep their height.
      // The decode stack can rise above the line where columns overlap, so the top counts it too.
      y: { range: (u, min, max) => [0, getRange() === '15m'
        ? (view.tops[kind] = holdMax(view.tops[kind], isDec ? Math.max(max, stackTop(view.columns())) : max, floor))
        : niceMax(max, floor)] },
    },
    series: [
      {},
      { label: isDec ? 'Decode' : 'Prefill', stroke: color, width: isDec ? 2 : 1.5, paths: stepped,
        // In the live view the decode line has no fill of its own: the finished requests' columns fill under
        // it, so each reads as a rectangle (drawColumns). Longer ranges have no columns and keep the fill.
        fill: isDec ? () => (getRange() === '15m' ? null : 'rgba(29,79,145,.16)') : 'rgba(210,52,42,.14)' },
      ...(isDec ? [{ label: 'Smoothed', stroke: MEAN_BLUE, width: 3 }] : []),
    ],
    axes: [
      // The decode plot sits on top: it keeps the time grid, the prefill plot below labels it.
      isDec ? { ...axis, space: 80, size: 4, ticks: { show: false }, values: (u, vals) => vals.map(() => '') }
        : { ...axis, space: 80, values: (u, vals) => vals.map((v) => axisTime(v, getRange())) },
      { ...axis, stroke: color, incrs: INT_INCRS, size: Y_AXIS_PX, label: isDec ? 'decode' : 'prefill',
        labelSize: 14, labelFont: FONT, values: (u, vals) => vals.map((v) => f.compact(v)) },
    ],
    hooks: {
      setCursor: [onCursor],
      // Under the line: the columns (decode) or prefill bands, then the request start lines on both.
      drawClear: isDec ? [(u) => drawColumns(u, view.columns(), getRange), (u) => drawStarts(u, view.starts(), getRange)]
        : [(u) => drawPrefillBands(u, store, getRange), (u) => drawStarts(u, view.starts(), getRange)],
    },
  };
}

// Finished requests' averages, under the decode line: a band across each decode window, as thick as its
// average rate, stacked on the wider requests running at each moment (stackBands), so the stack's top is the
// summed rate. A top edge keeps stacked bands apart; it steps where the band does. Live view only.
function drawColumns(u, columns, getRange) {
  if (getRange() !== '15m' || !columns.size || !u.data[0].length) return;
  const { ctx, bbox } = u;
  const top = u.scales.y.max;
  const all = stackBands(columns);
  const yOf = (v) => u.valToPos(Math.min(v, top), 'y', true);
  const xOf = (t) => u.valToPos(t, 'x', true);
  ctx.save();
  ctx.beginPath();
  ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
  ctx.clip();
  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.fillStyle = AVG_FILL;
  ctx.strokeStyle = AVG_EDGE;
  for (const c of columns.values()) {
    const pieces = all.get(c.id).filter((b) => b.base < top);
    if (!pieces.length) continue;
    for (const b of pieces) {
      const y = yOf(b.base + c.avg);
      ctx.fillRect(xOf(b.from), y, xOf(b.to) - xOf(b.from), yOf(b.base) - y);
    }
    ctx.beginPath();
    pieces.forEach((b, k) => {
      const y = yOf(b.base + c.avg);
      if (k && pieces[k - 1].to === b.from) ctx.lineTo(xOf(b.from), y);
      else ctx.moveTo(xOf(b.from), y);
      ctx.lineTo(xOf(b.to), y);
    });
    ctx.stroke();
  }
  ctx.restore();
}

function axisTime(ts, range) {
  return range === '7d' || range === '30d' ? f.day(ts) : f.hm(ts);
}

// A thin line at the second each request started, across the whole plot. Live view only.
function drawStarts(u, starts, getRange) {
  if (getRange() !== '15m' || !starts.size || !u.data[0].length) return;
  const { ctx, bbox } = u;
  const width = Math.max(1, Math.round(devicePixelRatio));
  const { min, max } = u.scales.x;
  ctx.save();
  ctx.strokeStyle = START_LINE;
  ctx.lineWidth = width;
  ctx.beginPath();
  for (const t of starts.values()) {
    const at = startLineAt(t, min, max);
    if (at == null) continue;
    // On a pixel's middle for an odd width, so the line stays sharp; inside the plot at its right edge.
    const x = Math.min(Math.round(u.valToPos(at, 'x', true)) + (width % 2) / 2, bbox.left + bbox.width - width / 2);
    ctx.moveTo(x, bbox.top);
    ctx.lineTo(x, bbox.top + bbox.height);
  }
  ctx.stroke();
  ctx.restore();
}

// While a request is prefilling its tokens aren't known yet, so instead of a
// guessed rate the prefill plot shades the interval it has been prefilling.
function drawPrefillBands(u, store, getRange) {
  const { tick } = store.get();
  if (getRange() !== '15m' || !store.isLive() || !tick || !u.data[0].length) return;
  const pre = tick.live.streams.filter((s) => s.phase === 'prefill');
  if (!pre.length) return;
  const { ctx, bbox } = u;
  const right = u.data[0][u.data[0].length - 1] + 1;
  ctx.save();
  ctx.fillStyle = 'rgba(242,194,48,.35)';
  for (const s of pre) {
    const x0 = Math.max(bbox.left, u.valToPos(s.started, 'x', true));
    const x1 = Math.min(bbox.left + bbox.width, u.valToPos(right, 'x', true));
    if (x1 > x0) ctx.fillRect(x0, bbox.top, x1 - x0, bbox.height);
  }
  ctx.restore();
}
