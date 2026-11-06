// The rack panel's Engine screen, read at arm's length: the state block, a middle tile (the model and its
// memory while it serves, the steps of a switch, why no engine runs, or the error), and the GPU numbers of
// the old GPU page on the right. No buttons: the panel shows state only. What it says comes from engine_model.js.
import { h, setText, setAttr } from '../dom.js';
import * as e from './engine_model.js';
import { stateTile } from './glance.js';
import { rebuilder, spark } from './view.js';

const fact = (f) => h('li', { 'data-tone': f.tone }, h('span', null, f.label), h('em', null, f.value));

function step(s) {
  const fill = s.fill === null ? null : h('i', { class: 'fillbar' }, h('b', { style: { width: `${s.fill}%` } }));
  return h('div', { class: 'step', 'data-s': s.state }, h('b', null, s.label), h('em', null, s.value),
    s.note ? h('span', null, s.note) : null, fill);
}

function middleTile() {
  const label = h('span');
  const meta = h('span', { class: 'r' });
  const title = h('div', { class: 'e-title' });
  const note = h('div', { class: 'e-note' });
  const facts = h('ul', { class: 'facts' });
  const steps = h('div', { class: 'steps' });
  const el = h('section', { class: 'tile e-mid' }, h('h2', { class: 'kicker' }, label, meta), title, note, facts, steps);
  const drawFacts = rebuilder(facts, fact);
  const drawSteps = rebuilder(steps, step);
  return {
    el,
    update(mode, mid) {
      setAttr(el, 'data-mode', mode);
      setText(label, mid.kicker);
      setText(meta, mid.meta);
      setText(title, mid.title);
      setText(note, mid.note);
      title.hidden = !mid.title;
      note.hidden = !mid.note;
      facts.hidden = !mid.facts.length;
      steps.hidden = !mid.steps.length;
      drawFacts(mid.facts);
      drawSteps(mid.steps);
    },
  };
}

function gpuCell(id) {
  const meta = h('span', { class: 'r' });
  const label = h('span');
  const value = h('span', { class: 'v' });
  const unit = h('span', { class: 'unit' });
  const sub = h('div', { class: 'sub' });
  const el = h('section', { class: `tile e-cell e-${id}` }, h('h2', { class: 'kicker' }, label, meta),
    h('div', { class: 'vrow' }, value, unit), sub);
  const line = id === 'busy' || id === 'power' ? spark(el, { h: 22 }) : null;
  return {
    el,
    update(c, series) {
      setText(label, c.label);
      setText(meta, c.meta);
      setText(value, c.value);
      setText(unit, c.unit);
      setText(sub, c.sub);
      setAttr(el, 'data-tone', c.tone);
      if (line) line(series, c.id === 'busy' ? 100 : undefined);
    },
  };
}

export function enginePage() {
  const state = stateTile();
  const mid = middleTile();
  const cells = ['busy', 'power', 'clock', 'temp'].map(gpuCell);
  const el = h('div', { class: 'page page-engine' }, state.el, mid.el, h('div', { class: 'e-gpu' }, cells.map((c) => c.el)));

  function update(v) {
    const view = e.engineView({ engine: v.engine, hw: v.tick && v.tick.hw, live: v.tick && v.tick.live,
      last: v.tick && v.tick.last, profiles: v.profiles, fleet: v.panel && v.panel.fleet, now: v.now });
    setAttr(el, 'data-mode', view.mode);
    state.update(view.state);
    mid.update(view.mode, view.mid);
    const hw = (v.tick && v.tick.hw) || {};
    e.gpuCells(hw, v.busy, v.power).forEach((c, i) => cells[i].update(c, c.spark === 'busy' ? v.busy : v.power));
  }
  return { id: 'engine', title: 'Engine', el, update };
}
