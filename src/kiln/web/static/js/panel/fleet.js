// The rack panel's Fleet screen, read at arm's length. At night with a night plan: the state block, the
// item that runs with a strip of the whole plan, and what the GPU does apart from the plan. By day or
// without a plan: the GPU lease, the queue (oldest first) and the last leases. What it says comes from
// fleet_model.js; a draw writes only what changed.
import { h, setText, setAttr } from '../dom.js';
import * as fm from './fleet_model.js';
import * as g from './glance_model.js';
import * as m from './model.js';
import { stateTile } from './glance.js';
import { fit, rebuilder } from './view.js';

const NOW_SIZES = [52, 44, 38, 32, 28];  // the running item's label
const LEASE_SIZES = [44, 38, 32, 28, 24];

// The strip: one box per item. A running box's fill moves alone, so the boxes are rebuilt only when an
// item's label, state, width or note changes.
function planStrip() {
  const el = h('div', { class: 'plan' });
  let fills = [];
  return {
    el,
    update(boxes) {
      const key = JSON.stringify(boxes.map((b) => [b.label, b.state, b.grow, b.min, b.note]));
      if (el.dataset.key !== key) {
        el.dataset.key = key;
        fills = [];
        el.replaceChildren(...boxes.map((b) => {
          const fill = b.fill === null ? null : h('u', { class: 'f' });
          fills.push(fill);
          return h('i', { 'data-s': b.state, style: { flex: `${b.grow} 1 0px`, minWidth: `${b.min}px` } }, fill, h('b', null, b.label),
            b.note ? h('small', null, b.note) : null);
        }));
      }
      boxes.forEach((b, i) => {
        const w = `${b.fill ?? 0}%`;
        if (fills[i] && fills[i].style.width !== w) fills[i].style.width = w;
      });
    },
  };
}

function planTile() {
  const label = h('span');
  const ends = h('span', { class: 'r' });
  const name = h('b', { class: 'f-name' });
  const spent = h('span', { class: 'f-spent' });
  const of = h('small');
  const strip = planStrip();
  const el = h('section', { class: 'tile f-plan' }, h('h2', { class: 'kicker' }, label, ends),
    h('div', { class: 'f-now' }, name, h('span', { class: 'f-time' }, spent, of)), strip.el);
  return {
    el,
    update(sum, boxes) {
      setText(label, `Night plan · ${sum.ended} of ${sum.total} ended`);
      setText(ends, sum.endsAt === null ? '' : `ends ≈ ${fm.hmShort(sum.endsAt)}`);
      setText(name, sum.current ? sum.current.label : 'Night plan done');
      setText(spent, sum.current ? sum.current.spent : '');
      setText(of, sum.current ? sum.current.of : '');
      fit(name, NOW_SIZES);
      strip.update(boxes);
    },
  };
}

const factRow = (r) => h('li', { 'data-tone': r.tone }, h('span', null, r.label), h('em', null, r.value));

function offPlanTile() {
  const list = h('ul', { class: 'facts' });
  const el = h('section', { class: 'tile f-off corner' }, h('h2', { class: 'kicker' }, 'Not in the plan'), list);
  const draw = rebuilder(list, factRow);
  return { el, update: (rows) => draw(rows) };
}

// The lease by day: who holds the GPU, its clock toward the hard limit, and the bar. The daily engine holds
// the lock itself: no clock and no bar.
function leaseTile() {
  const meta = h('span', { class: 'r' });
  const name = h('div', { class: 'f-lease' });
  const clock = h('div', { class: 'f-clock' });
  const fill = h('i');
  const bar = h('span', { class: 'bar' }, fill);
  const el = h('section', { class: 'tile f-lease-tile' }, h('h2', { class: 'kicker' }, 'GPU lease', meta), name, clock, bar);
  return {
    el,
    update(panel, now) {
      const v = g.leaseNow(panel, now);
      const l = panel && panel.fleet && panel.fleet.lease;
      setText(meta, l && !l.daily ? v.meta.split(' · usually')[0] : l ? v.meta : '');
      setText(name, v.name);
      fit(name, LEASE_SIZES);
      setText(clock, v.clock ? `${v.clock} ${v.of}`.trim() : l && l.daily ? 'holds the GPU itself' : v.meta);
      setAttr(el, 'data-tone', v.tone === 'idle' ? null : v.tone);
      bar.hidden = v.pct == null;
      const w = `${v.pct ?? 0}%`;
      if (fill.style.width !== w) fill.style.width = w;
    },
  };
}

const qRow = (r) => h('div', { class: 'g-qrow' }, h('span', { class: 'nm' }, r.name), h('span', { class: 'kind' }, r.kind),
  h('span', { class: 'usual' }, r.usual), h('span', { class: 'max' }, r.max));

function queueTile() {
  const label = h('span');
  const oldest = h('span', { class: 'r' });
  const list = h('div', { class: 'g-qrows' });
  const foot = h('div', { class: 'f-more' });
  const el = h('section', { class: 'tile f-queue' }, h('h2', { class: 'kicker' }, label, oldest), list, foot);
  const draw = rebuilder(list, qRow);
  return {
    el,
    update(q) {
      setText(label, q.label);
      setText(oldest, q.oldest);
      draw(q.rows);
      setText(foot, q.more || q.empty);
    },
  };
}

const leaseRow = (r) => h('li', { 'data-tone': r.tone }, h('span', null, r.agent), h('em', null, r.outcome === 'ok' ? 'OK' : r.outcome));

function recentTile() {
  const list = h('ul', { class: 'facts f-recent-rows' });
  const el = h('section', { class: 'tile f-recent' }, h('h2', { class: 'kicker' }, 'Last leases'), list);
  const draw = rebuilder(list, leaseRow);
  return {
    el,
    update(fleet) {
      const rows = m.recentRows(fleet, 4);
      draw(rows.length ? rows : [{ agent: fleet ? 'No finished lease' : 'No fleet data', outcome: '', tone: null }]);
    },
  };
}

export function fleetPage() {
  const state = stateTile();
  const plan = { tile: planTile(), off: offPlanTile() };
  const day = { lease: leaseTile(), queue: queueTile(), recent: recentTile() };
  const views = {
    plan: h('div', { class: 'f-view f-plan-view' }, plan.tile.el, plan.off.el),
    day: h('div', { class: 'f-view f-day-view' }, day.lease.el, day.queue.el, day.recent.el),
  };
  const el = h('div', { class: 'page page-fleet' }, state.el, views.plan, views.day);

  function update(v) {
    const fleet = v.panel && v.panel.fleet;
    const mode = fm.fleetMode({ engine: v.engine, fleet });
    Object.entries(views).forEach(([k, node]) => { node.hidden = k !== mode; });
    const sum = mode === 'plan' ? fm.planSummary(fleet.plan, v.now) : null;
    state.update(fm.fleetState(v.engine, sum, mode));
    if (mode === 'plan') {
      plan.tile.update(sum, fm.planStrip(fleet.plan.items));
      plan.off.update(fm.notInPlan(fleet, (v.tick && v.tick.hw) || {}, v.engine, v.now));
      return;
    }
    day.lease.update(v.panel, v.now);
    day.queue.update(fm.dayQueue(fleet, 4));
    day.recent.update(fleet);
  }
  return { id: 'fleet', title: 'Fleet', el, update };
}
