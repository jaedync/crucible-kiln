// Tiny DOM helpers. Everything goes through textContent and setAttribute, never
// innerHTML, because device names and engine log lines are untrusted strings.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

const SVG_NS = 'http://www.w3.org/2000/svg';

export function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== null && v !== undefined) el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

// Writes only on change, so a 2 Hz tick doesn't restyle text that stayed the same.
export function setText(el, text) {
  const s = text === null || text === undefined ? '' : String(text);
  if (el.textContent !== s) el.textContent = s;
}

export function setAttr(el, name, value) {
  if (value === null || value === undefined || value === false) {
    if (el.hasAttribute(name)) el.removeAttribute(name);
    return;
  }
  const s = value === true ? '' : String(value);
  if (el.getAttribute(name) !== s) el.setAttribute(name, s);
}

export function toggleClass(el, name, on) {
  if (el.classList.contains(name) !== Boolean(on)) el.classList.toggle(name, Boolean(on));
}

// Segmented button group (period, group, chart range). aria-pressed carries state
// so screen readers announce the current choice.
export function segmented(label, options, current, onPick) {
  const group = h('div', { class: 'seg', role: 'group', 'aria-label': label });
  const buttons = options.map(([value, text]) => h('button', {
    type: 'button', 'data-value': value, 'aria-pressed': String(value === current),
    onclick: () => { select(value); onPick(value); },
  }, text));
  function select(value) {
    for (const b of buttons) b.setAttribute('aria-pressed', String(b.dataset.value === value));
  }
  group.append(...buttons);
  return { el: group, select };
}

export function panelHead(title, ...extra) {
  return h('header', { class: 'panel-head' }, h('h2', { class: 'panel-title' }, title), ...extra);
}
