// The rack panel's home page, read from across the room (2-3 m): a colour block for the state with power
// and temperatures under it, one large number over a column chart, and one lane per engine slot (blue
// decode, striped yellow prefill). At night it shows the fleet's use of the GPU; an alarm turns the whole
// screen red, with one button to mute it for 30 minutes; during a mute a red tag on the large number counts
// the minutes left. What it says comes from glance_model.js.
import { h, setText, setAttr } from '../dom.js';
import { DASH, int } from '../format.js';
import * as g from './glance_model.js';
import { columnChart, fit, setApprox } from './view.js';

const WORD_SIZES = [84, 72, 62, 54];
const NAME_SIZES = [40, 34, 28, 24];
const LEASE_SIZES = [40, 34, 28, 24];  // the agent's name, beside its clock
const ALARM_SIZES = [120, 104, 92, 80];

// Also the Engine screen's state block (engine.js).
export function stateTile() {
  const word = h('div', { class: 'g-word' });
  const name = h('div', { class: 'g-name' });
  const sub = h('div', { class: 'g-sub' });
  const el = h('section', { class: 'tile g-state' }, word, name, sub);
  return {
    el,
    update(st) {
      setText(word, st.word);
      setText(name, st.name);
      setText(sub, st.sub);
      setAttr(el, 'data-tone', st.tone);
      fit(word, WORD_SIZES);
      fit(name, NAME_SIZES);
    },
  };
}

// Three cells under the state block, the same in both layouts: package power, GPU and CPU temperature,
// each a gauge that fills to its level in its zone's colour (glance_model.vitals).
const VITAL_SIZES = [52, 44, 38, 34];  // "100°C" is wider than a temperature cell at 52 px

function vitalsTile() {
  const cell = (label, unit) => {
    const fill = h('i', { class: 'g-vfill' });
    const num = h('b');
    const line = h('div', { class: 'g-vnum' }, num, h('small', null, unit));
    return { fill, num, line, el: h('section', { class: 'tile g-vital' }, fill, h('div', { class: 'kicker' }, label), line) };
  };
  const cells = { power: cell('Power', 'W'), gpu: cell('GPU', '°C'), cpu: cell('CPU', '°C') };
  const el = h('div', { class: 'g-vitals' }, Object.values(cells).map((c) => c.el));
  return {
    el,
    update(hw) {
      const v = g.vitals(hw);
      Object.entries(cells).forEach(([k, c]) => {
        setText(c.num, v[k].text);
        setAttr(c.el, 'data-zone', v[k].zone);
        const w = `${v[k].pct}%`;
        if (c.fill.style.width !== w) c.fill.style.width = w;
        fit(c.line, VITAL_SIZES);
      });
    },
  };
}

// The large number: decode tok/s, or prefill progress when nothing decodes, or GPU busy at night.
function heroTile() {
  const chip = h('i', { class: 'chip' });
  const label = h('span');
  const big = h('div', { class: 'g-huge' });
  const sideTop = h('b');
  const sideMid = h('span');
  const sideEnd = h('span');
  const fill = h('i', { class: 'hatch' });
  const tag = h('div', { class: 'g-mtag', hidden: true });  // over the corner block during an alarm mute
  // The side text sits in a row with the number, so a wide number pushes it instead of covering it.
  const el = h('section', { class: 'tile g-hero corner' }, h('div', { class: 'kicker' }, chip, label),
    h('div', { class: 'g-row' }, big, h('div', { class: 'g-side' }, sideTop, sideMid, sideEnd)),
    h('div', { class: 'g-band' }, fill), tag);
  const draw = columnChart(el, g.HERO_COLUMNS);

  function side(top, mid, end) {
    setText(sideTop, top);
    setText(sideMid, mid);
    setText(sideEnd, end);
  }

  return {
    el,
    muted(text) {
      tag.hidden = !text;
      setText(tag, text);
    },
    decode(hero, cols) {
      setAttr(el, 'data-kind', 'decode');
      setAttr(chip, 'data-kind', 'dec');
      setText(label, 'Decode tok/s');
      setApprox(big, hero.value, hero.approx);
      draw(cols.decode, null, cols.prefill);  // a mark: the window also prefilled
    },
    prefill(hero) {
      setAttr(el, 'data-kind', hero.kind);
      setAttr(chip, 'data-kind', 'pre');
      setText(label, 'Prefill');
      if (hero.kind === 'prefill') {
        setApprox(big, String(hero.pct), false, '%');
        side(`${hero.done} / ${hero.total}`, hero.cached ? `+${hero.cached} cached` : 'tokens',
          hero.left ? `≈ ${hero.left} left` : '');
        const w = `${hero.pct}%`;
        if (fill.style.width !== w) fill.style.width = w;
      } else {
        setApprox(big, hero.value, false, hero.unit);
        side(hero.count > 1 ? `${int(hero.count)} prompts` : '1 prompt', hero.read ? `≈ ${hero.read} read` : 'no progress',
          hero.read ? 'so far' : 'reported yet');
      }
    },
    busy(hw, cols) {
      setAttr(el, 'data-kind', 'busy');
      setAttr(chip, 'data-kind', null);
      setText(label, 'GPU busy');
      setApprox(big, hw.gpu_busy_pct == null ? DASH : String(Math.round(hw.gpu_busy_pct)), false,
        hw.gpu_busy_pct == null ? null : '%');
      draw(cols, 100);
    },
  };
}

// One lane per slot. The elements stay; each draw only rewrites what changed.
function lane() {
  const fill = h('i', { class: 'fill hatch' });
  const name = h('span', { class: 'nm' });
  const tok = h('span', { class: 'tok' });
  const val = h('span', { class: 'val' });
  const draftNum = h('b');
  const draft = h('span', { class: 'dr' }, draftNum);
  const el = h('div', { class: 'lane' }, fill, name, tok, draft, val);
  return {
    el,
    update(r) {
      setAttr(el, 'data-kind', r.kind);
      setText(name, r.name);
      setText(val, r.val);
      const key = `${r.kind}|${r.approx}|${r.tok}`;
      if (tok.dataset.key !== key) {
        tok.dataset.key = key;
        tok.replaceChildren(...(r.approx ? [h('span', { class: 'approx' }, '≈')] : []), r.tok,
          ...(r.kind === 'dec' ? [h('small', null, 'tok')] : []));
      }
      const w = r.pct == null ? '0%' : `${r.pct}%`;
      if (fill.style.width !== w) fill.style.width = w;
      // A decode lane's conversation draft acceptance: its band colours the cell (css .lane .dr).
      setAttr(el, 'data-draft', r.draft ? r.draft.band : null);
      setText(draftNum, r.draft ? r.draft.text : '');
    },
  };
}

function lanesTile() {
  const label = h('span');
  const more = h('span', { class: 'more' });
  const lanes = Array.from({ length: g.MAX_LANES }, lane);
  const box = h('div', { class: 'lanes' }, lanes.map((l) => l.el));
  const el = h('section', { class: 'tile g-lanes' }, h('div', { class: 'kicker' }, label, more), box);
  return {
    el,
    update(view) {
      setText(label, view.label);
      setText(more, view.more);
      setAttr(box, 'data-compact', view.compact);
      setAttr(box, 'data-drafts', view.drafts);
      lanes.forEach((l, i) => {
        l.el.hidden = i >= view.rows.length;
        if (i < view.rows.length) l.update(view.rows[i]);
      });
    },
  };
}

// The fleet's running lease: who holds the GPU, its clock, and a bar toward the hard limit. By day the
// daily engine holds the lock itself, so the tile names it and shows no bar.
function leaseTile() {
  const meta = h('span', { class: 'meta' });
  const name = h('div', { class: 'g-lease' });
  const clock = h('b');
  const of = h('small');
  const fill = h('i');
  const bar = h('span', { class: 'bar' }, fill);
  const el = h('section', { class: 'tile g-lease-tile' }, h('div', { class: 'kicker' }, 'GPU lease', meta),
    h('div', { class: 'g-lease-row' }, name, h('div', { class: 'g-clock' }, clock, of)), bar);
  return {
    el,
    update(panel, now) {
      const v = g.leaseNow(panel, now);
      setText(meta, v.meta);
      setText(name, v.name);
      setText(clock, v.clock);
      setText(of, v.of);
      setAttr(el, 'data-tone', v.tone === 'idle' ? null : v.tone);
      bar.hidden = v.pct == null;
      const w = `${v.pct ?? 0}%`;
      if (fill.style.width !== w) fill.style.width = w;
      fit(name, LEASE_SIZES);
    },
  };
}

// What waits for the GPU: the lease wrapper's queue as each job's kind, usual hold and hard limit, or the night
// driver's plan as each item's label, state and expected minutes (glance_model.queueView).
function queueTile() {
  const label = h('span');
  const more = h('span', { class: 'more' });
  const list = h('div', { class: 'g-qrows' });
  const el = h('section', { class: 'tile g-queue-tile' }, h('div', { class: 'kicker' }, label, more), list);
  return {
    el,
    update(fleet) {
      const q = g.queueView(fleet);
      setText(label, q.label);
      setText(more, q.more);
      const key = JSON.stringify([q.rows, q.empty]);
      if (list.dataset.key === key) return;
      list.dataset.key = key;
      list.replaceChildren(...q.rows.map((r) => h('div', { class: 'g-qrow' }, h('span', { class: 'nm' }, r.name),
        h('span', { class: 'kind' }, r.kind), h('span', { class: 'usual' }, r.usual), h('span', { class: 'max' }, r.max))),
      ...(q.empty ? [h('div', { class: 'g-qempty' }, q.empty)] : []));
    },
  };
}

export function glancePage() {
  const serve = { state: stateTile(), vitals: vitalsTile(), hero: heroTile(), lanes: lanesTile() };
  const night = { state: stateTile(), vitals: vitalsTile(), hero: heroTile(), lease: leaseTile(), queue: queueTile() };
  night.right = h('div', { class: 'g-right' }, night.lease.el, night.queue.el);
  const left = (t) => h('div', { class: 'g-left' }, t.state.el, t.vitals.el);
  const alarmTitle = h('div', { class: 'g-alarm-title' });
  const alarmSub = h('div', { class: 'g-alarm-sub' });
  const muteNote = h('small');
  // data-ctl: main.js passes a tap to `tap`, which returns the kind to mute (glance_model.muteButton).
  const mute = h('div', { class: 'g-mute', role: 'button', 'data-ctl': 'mute', hidden: true },
    h('b', null, 'Mute 30 min'), muteNote);
  let muteKind = null;  // as drawn last, and only when this session may act
  const layouts = {
    serve: h('div', { class: 'g-layout g-serve' }, left(serve), serve.hero.el, serve.lanes.el),
    fleet: h('div', { class: 'g-layout g-fleet' }, left(night), night.hero.el, night.right),
    alarm: h('div', { class: 'g-layout g-alarm' }, h('section', { class: 'tile' }, alarmTitle, alarmSub, mute)),
  };
  const el = h('div', { class: 'page page-glance' }, Object.values(layouts));
  let memo = new Map();  // per stream: prefill progress or the decode count shown (glance_model.lanes)

  function update(v) {
    const hw = (v.tick && v.tick.hw) || {};
    const live = v.tick && v.tick.live;
    const mode = g.glanceMode({ engine: v.engine, hw, connDownS: v.connDownS, mute: v.mute });
    Object.entries(layouts).forEach(([k, node]) => { node.hidden = k !== mode; });
    if (mode === 'alarm') {
      const a = g.alarmInfo({ engine: v.engine, hw, connDownS: v.connDownS, now: v.now, mute: v.mute });
      setText(alarmTitle, a.title);
      setText(alarmSub, a.sub);
      const b = g.muteButton(a, v.canEdit);
      mute.hidden = !b.show;
      fit(alarmTitle, ALARM_SIZES);  // beside the button "ENGINE FAILED" needs less than 120 px
      setAttr(mute, 'aria-disabled', b.editable ? null : 'true');
      setText(muteNote, b.note);
      muteKind = b.editable ? b.kind : null;
      return;
    }
    muteKind = null;
    const tag = g.muteTag(v.mute, v.now);
    const st = g.stateTile(v.engine, v.now);
    if (mode === 'serve') {
      serve.state.update(st);
      serve.vitals.update(hw);
      const res = g.lanes(live, v.now, memo, v.tick ? v.tick.ts : v.now);
      memo = res.memo;
      serve.lanes.update(res.view);
      serve.hero.muted(tag);
      const hero = g.heroServe(live, v.now, memo);
      if (hero.kind === 'decode') serve.hero.decode(hero, v.heroCols);
      else serve.hero.prefill(hero);
      return;
    }
    night.state.update(st);
    night.vitals.update(hw);
    night.hero.busy(hw, v.busyCols);
    night.hero.muted(tag);
    // Without a fleet ([schedule]) there is no lease or queue: the GPU's busy columns take the width.
    const fleet = v.fleet !== false;
    night.right.hidden = !fleet;
    layouts.fleet.classList.toggle('no-fleet', !fleet);
    if (!fleet) return;
    night.lease.update(v.panel, v.now);
    night.queue.update(v.panel && v.panel.fleet);
  }
  // A tap on the alarm's button: { mute: kind } to send, or null.
  function tap(target) {
    const box = target && target.closest ? target.closest('[data-ctl="mute"]') : null;
    return box && muteKind ? { mute: muteKind } : null;
  }
  return { id: 'glance', title: 'Glance', full: true, el, update, tap };
}
