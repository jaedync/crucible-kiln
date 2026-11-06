// DOM helpers shared by the rack panel's pages: trend lines, column charts, ≈ values and text that must fit one line.
import { svg, setAttr, h } from '../dom.js';
import * as m from './model.js';

// A trend line over w x h, appended to `parent`.
export function spark(parent, { w = 300, h: ht = 44, cls = 'spark' } = {}) {
  const line = svg('path', { d: '' });
  const el = svg('svg', { class: cls, viewBox: `0 0 ${w} ${ht}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
    svg('path', { class: 'floor', d: `M0,${ht}L${w},${ht}` }), line);
  parent.append(el);
  return (values, max) => setAttr(line, 'd', m.sparkPath(values, w, ht, max));
}

// n columns, appended to `parent`. Each has a bar and a mark at its foot (css .g-cols). A draw writes a
// style only where a height or a mark changed: between windows that is the newest column alone.
export function columnChart(parent, n, cls = 'g-cols') {
  const cols = Array.from({ length: n }, () => {
    const bar = h('b');
    return { bar, el: h('i', null, bar, h('u')) };
  });
  parent.append(h('div', { class: cls, 'aria-hidden': 'true' }, cols.map((c) => c.el)));
  return (values, top, marks = []) => {
    const heights = m.columnHeights(values, top);
    cols.forEach((c, i) => {
      const ht = `${heights[i]}%`;
      if (c.bar.style.height !== ht) c.bar.style.height = ht;
      setAttr(c.el, 'data-mark', marks[i] ? '' : null);
    });
  };
}

// A value with a small leading ≈ when the server marks it estimated, and an optional unit after it.
export function setApprox(el, text, estimated, unit = null) {
  const want = `${estimated ? '≈' : ''}${text}|${unit || ''}`;
  if (el.dataset.text === want) return;
  el.dataset.text = want;
  el.replaceChildren(...(estimated ? [h('span', { class: 'approx' }, '≈')] : []), text,
    ...(unit ? [h('small', null, unit)] : []));
}

// A list of rows that is rebuilt only when its content changes, so a draw that changes nothing writes nothing.
export function rebuilder(el, make) {
  return (items) => {
    const key = JSON.stringify(items);
    if (el.dataset.key === key) return;
    el.dataset.key = key;
    el.replaceChildren(...items.map(make));
  };
}

// The largest of `sizes` (px) at which one line of text fits its box. It measures only when the text or
// the box width changed, and never while the box is hidden (width 0); CSS ellipsis covers the rest.
export function fit(el, sizes) {
  const w = el.clientWidth;
  const key = `${el.textContent}|${w}`;
  if (!w || el.dataset.fit === key) return;
  el.dataset.fit = key;
  for (const px of sizes) {
    el.style.fontSize = `${px}px`;
    if (el.scrollWidth <= el.clientWidth) return;
  }
}

// Forget every fit, so the next draw measures again (after the fonts load).
export function refit() {
  document.querySelectorAll('[data-fit]').forEach((el) => { delete el.dataset.fit; });
}
