// Activity heatmap: recent days (columns) by hour (rows). Colour is the hour's
// top client, strength is how much of the hour the engine was busy. Days before
// the first busy one are left out (at least MIN_DAYS stay). Hover a cell, or
// focus the grid and use the arrow keys, to read an hour in the line below.
import { h, setText, panelHead } from '../dom.js';
import * as f from '../format.js';
import { pollWhileSeen } from '../poll.js';

const DAYS = 31;
const MIN_DAYS = 14;
const REFRESH_MS = 5 * 60 * 1000;
const LEVELS = [[0.6, 1], [0.3, 0.78], [0.1, 0.56], [0, 0.36]];
const HINT = 'Point at an hour, or focus the grid and use the arrow keys, for its detail.';

function level(ratio) {
  if (!(ratio > 0)) return 0;
  return LEVELS.find(([min]) => ratio > min)[1];
}

// Drops leading days with no busy hour, keeping at least `min` days.
export function trimDays(days, min = MIN_DAYS) {
  const first = days.findIndex((d) => d.hours.some((x) => x && x.busy_s > 0));
  if (first < 0) return days.slice(-min);
  return days.slice(Math.min(first, Math.max(0, days.length - min)));
}

export default {
  id: 'activity',
  label: 'Activity heatmap',
  mount(el, { store, api }) {
    const grid = h('div', { class: 'heat', role: 'img', tabindex: '0' });
    const detail = h('p', { class: 'heat-detail', 'aria-live': 'polite' }, HINT);
    const legend = h('ul', { class: 'heat-legend' });
    const zone = h('p', { class: 'heat-tz' });
    const status = h('p', { class: 'panel-status', role: 'status' });
    const sub = h('span', { class: 'panel-sub' });
    el.append(panelHead('Activity', sub), h('div', { class: 'heat-wrap scroll-x' }, grid), detail, legend, zone, status);
    let cells = [];   // [hour][day] -> {el, text}
    let focus = null; // [hour, day] picked with the keyboard

    async function load() {
      try {
        const res = await api.getJSON('activity', { days: DAYS });
        store.set({ clients: res.clients || {} });
        store.emit('clients', res.clients);
        render(res);
        setText(status, '');
      } catch (err) {
        setText(status, `Couldn't load activity: ${err.message}`);
      }
    }

    function render(res) {
      const slot = res.slot_s || 3600;
      const days = trimDays(res.days);
      const nodes = [h('span', { class: 'heat-corner' })];
      days.forEach((d, i) => nodes.push(dayLabel(d.date, i, days.length)));
      let total = 0;
      const tops = new Set();
      cells = [];
      for (let hr = 0; hr < 24; hr++) {
        nodes.push(h('span', { class: 'heat-hour' }, hr % 6 === 0 ? String(hr).padStart(2, '0') : ''));
        const row = [];
        for (const d of days) {
          const cell = d.hours[hr] || { busy_s: 0, top: null };
          total += cell.busy_s;
          if (cell.top && cell.busy_s > 0) tops.add(cell.top);
          const c = heatCell(store, res, d.date, hr, cell, slot);
          c.el.addEventListener('mouseenter', () => setText(detail, c.text));
          row.push(c);
          nodes.push(c.el);
        }
        cells.push(row);
      }
      grid.style.setProperty('--days', days.length);
      grid.replaceChildren(...nodes);
      grid.setAttribute('aria-label', `Busy ${f.dur(total)} over the last ${days.length} days`);
      setText(sub, `Last ${days.length} days × hour of day`);
      renderLegend(res.clients || {}, tops);
      if (focus) show(focus[0], Math.min(focus[1], days.length - 1));
      // Days and hours are in the server's zone, which may differ from this browser's.
      const local = Intl.DateTimeFormat().resolvedOptions().timeZone;
      setText(zone, res.tz ? `Hours in ${res.tz} (the server's zone)${res.tz === local ? '' : `; this browser is on ${local}`}` : '');
    }

    function show(hr, day) {
      for (const row of cells) for (const c of row) c.el.classList.remove('is-focus');
      focus = [hr, day];
      const c = cells[hr] && cells[hr][day];
      if (!c) return;
      c.el.classList.add('is-focus');
      setText(detail, c.text);
    }

    grid.addEventListener('mouseleave', () => setText(detail, focus ? cells[focus[0]][focus[1]].text : HINT));
    grid.addEventListener('focus', () => {
      if (!cells.length) return;
      // Start on the newest day at the current hour.
      show(focus ? focus[0] : new Date().getHours(), focus ? focus[1] : cells[0].length - 1);
    });
    grid.addEventListener('blur', () => {
      for (const row of cells) for (const c of row) c.el.classList.remove('is-focus');
      focus = null;
      setText(detail, HINT);
    });
    grid.addEventListener('keydown', (e) => {
      const step = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[e.key];
      if (!step || !focus || !cells.length) return;
      e.preventDefault();
      const hr = Math.min(23, Math.max(0, focus[0] + step[0]));
      const day = Math.min(cells[0].length - 1, Math.max(0, focus[1] + step[1]));
      show(hr, day);
    });

    // Only clients that top a shown hour: the heatmap draws no one else.
    function renderLegend(clients, tops) {
      const items = Object.entries(clients).filter(([key]) => tops.has(key)).map(([key, c]) =>
        h('li', null, h('i', { class: 'swatch', style: { background: store.colorFor(key, c.color) } }), c.name || key));
      const scale = h('li', { class: 'heat-scale' }, 'busy',
        ...[0.36, 0.56, 0.78, 1].map((o) => h('i', { class: 'swatch', style: { background: 'var(--ink)', opacity: o } })),
        'of the hour');
      legend.replaceChildren(...items, scale);
    }

    pollWhileSeen(el, load, { periodMs: REFRESH_MS });
  },
};

function dayLabel(date, i, n) {
  const d = new Date(`${date}T12:00:00`);
  // Month starts get a label unless they'd be clipped at the right edge.
  const first = i === 0 || (d.getDate() === 1 && i < n - 3);
  const show = first || d.getDay() === 1;
  const text = show ? (first ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : String(d.getDate())) : '';
  return h('span', { class: `heat-day${d.getDay() === 0 || d.getDay() === 6 ? ' wknd' : ''}` }, text);
}

function heatCell(store, res, date, hr, cell, slot) {
  const ratio = cell.busy_s / slot;
  const lvl = level(ratio);
  const who = cell.top ? (res.clients[cell.top] && res.clients[cell.top].name) || cell.top : null;
  const day = new Date(`${date}T12:00:00`).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  const text = `${day}, ${String(hr).padStart(2, '0')}:00 · ${cell.busy_s ? `${f.dur(cell.busy_s)} busy` : 'idle'}${who ? ` · mostly ${who}` : ''}`;
  const style = lvl ? { background: cell.top ? store.colorFor(cell.top, res.clients[cell.top] && res.clients[cell.top].color) : 'var(--ink)', opacity: lvl } : null;
  return { el: h('span', { class: `heat-cell${lvl ? '' : ' empty'}`, title: text, style }), text };
}
