// Masthead strip: today's totals from tick.today, so the top of the page answers
// "how much has it done today" without scrolling. The cost turns on digit wheels as prompts and tokens stream
// (cost.js, odometer.js): frames run only while it moves, and the browser pauses them in a hidden tab.
import { h, setText } from '../dom.js';
import * as f from '../format.js';
import { hasReference, referenceShort } from '../pricing.js';
import { chase, daySplit, liveParts, moving, owedStep, settled, storedCost, tintStep, wheels } from '../cost.js';
import { costMix, MIX_TITLE } from '../cost_mix.js';
import { odometer } from '../odometer.js';

const REDUCED = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;

export default {
  id: 'today',
  label: 'Today',
  slot: '#today',
  mount(el, { store }) {
    const stat = (label) => {
      const v = h('span', { class: 'td-v' });
      const sub = h('span', { class: 'td-sub' });
      return { el: h('div', { class: 'td-stat' }, h('span', { class: 'kicker' }, label), v, sub), v, sub };
    };
    const req = stat('Requests');
    const prompt = stat('Prompt tokens');
    const out = stat('Output tokens');
    const cost = stat('Via API');
    const odo = odometer();
    const mix = costMix();
    cost.v.classList.add('td-cost');
    cost.el.title = MIX_TITLE;
    cost.v.append(odo.el, mix.el);
    const head = h('p', { class: 'kicker td-head' }, 'Today');
    el.append(head,
      h('div', { class: 'td-row' }, req.el, prompt.el, out.el, cost.el));

    let shown = null;
    let owed = null;  // cost.js owedStep: what the turning wheels are paying off, for their colour
    let tint = null;  // cost.js tintStep: the wheels that turned, in the colour of what turned them
    let frame = 0;
    let lastT = null;
    const roll = (t) => {
      frame = 0;
      const { tick, pricing } = store.get();
      const now = store.now();
      const parts = liveParts(tick, pricing, now);
      const target = parts && parts.stored + parts.cached + parts.input + parts.output;
      const dt = lastT === null ? 0 : (t - lastT) / 1000;
      const before = shown ? shown.units : null;
      shown = chase(shown, target, storedCost(tick), dt, REDUCED && REDUCED.matches ? 0 : undefined,
        tick && tick.today ? tick.today.since : null);
      owed = owedStep(owed, parts, before, shown ? shown.units : null);
      mix.set(daySplit(tick, pricing, parts));
      tint = tintStep(tint, shown ? wheels(shown.units) : [], owed.kind, t / 1000);
      odo.set(shown ? shown.units : null, tint.kinds);
      // The target moves on for CAP_S past its tick (cost.js); after that only a new tick moves it.
      if (moving(tick, now) || !settled(shown, target) || tint.kinds.some(Boolean)) {
        lastT = t;
        frame = requestAnimationFrame(roll);
      } else lastT = null;
    };

    store.on('tick', (tick) => {
      const t = tick.today;
      if (!t) return;
      setText(head, tick.host ? `Today on ${tick.host}` : 'Today');
      setText(req.v, f.int(t.requests));
      setText(req.sub, 'since midnight');
      setText(prompt.v, f.compact(t.prompt));
      setText(prompt.sub, t.prompt ? `${f.pct(t.cached / t.prompt)} cached` : 'none');
      setText(out.v, f.compact(t.completion));
      setText(out.sub, 'generated');
      cost.el.hidden = !hasReference(store.get().pricing);
      if (!frame) frame = requestAnimationFrame(roll);
      setText(cost.sub, `${referenceShort(store.get().pricing)} · ${f.usd(t.cost.openrouter)} OpenRouter`);
    });
  },
};
