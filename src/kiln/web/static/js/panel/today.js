// The rack panel's Today screen, read at arm's length: requests today, requests per hour (one column per
// local hour, the current hour light and outlined), the tokens and GPU busy time, and the via-API cost with
// the top clients. What it says comes from today_model.js; the hours change with the 15 s usage poll.
import { h, setText, setAttr } from '../dom.js';
import { int } from '../format.js';
import { chase, daySplit, liveParts, owedStep, splitUsd, storedCost, tintStep } from '../cost.js';
import { costMix, MIX_TITLE } from '../cost_mix.js';
import { hasReference } from '../pricing.js';
import * as m from './model.js';
import * as tm from './today_model.js';
import { fit, rebuilder } from './view.js';

const COST_SIZES = [52, 46, 40, 34, 30];  // $12.3456 must fit the Via API tile
const CLIENT_ROWS = 3;

// The figure's characters, one span each, so a digit that changed can take the colour of what changed it
// (cost.js tintStep). The spans are made again only when the count of characters changes.
function drawChars(el, chars, kinds) {
  if (el.children.length !== chars.length) el.replaceChildren(...chars.map(() => h('span')));
  chars.forEach((c, i) => {
    setText(el.children[i], c);
    setAttr(el.children[i], 'data-kind', kinds[i]);
  });
}

// costChars(units, tint, kind, now) -> {head, tail, tint}: the figure's two parts as characters with a kind
// each (null for '$', '.', ',' and digits that did not change)
function costChars(units, tint, kind, now) {
  const [head, tail] = splitUsd(units).map((t) => [...t]);
  const isDigit = (c) => c >= '0' && c <= '9';
  const next = tintStep(tint, [...head, ...tail].filter(isDigit), kind, now);
  let d = 0;
  const kindsOf = (chars) => chars.map((c) => (isDigit(c) ? next.kinds[d++] : null));
  const headKinds = kindsOf(head);
  return { head, tail, headKinds, tailKinds: kindsOf(tail), tint: next };
}

const factRow = (r) => h('li', null, h('span', null, r.label), h('em', null, r.value));

// One column per hour. The columns are rebuilt only when the day's hour count changes (23-25); a poll
// writes only the heights and marks that changed.
function hoursChart() {
  const cols = h('div', { class: 'hours', 'aria-hidden': 'true' });
  const labels = h('div', { class: 'hlabels', 'aria-hidden': 'true' });
  let bars = [];
  let marks = [];
  return {
    el: [cols, labels],
    update(view) {
      if (bars.length !== view.cols.length) {
        bars = view.cols.map(() => h('i'));
        cols.replaceChildren(...bars);
        marks = view.labels.map(() => h('span'));
        labels.replaceChildren(...marks);
      }
      view.cols.forEach((c, i) => {
        const ht = `${c.pct}%`;
        if (bars[i].style.height !== ht) bars[i].style.height = ht;
        setAttr(bars[i], 'data-zero', c.zero ? '' : null);
        setAttr(bars[i], 'data-now', c.now ? '' : null);
      });
      view.labels.forEach((t, i) => setText(marks[i], t));
    },
  };
}

export function todayPage() {
  const reqValue = h('span', { class: 'v v-key' });
  const reqSub = h('div', { class: 'sub' });
  const req = h('section', { class: 'tile t-req corner' }, h('h2', { class: 'kicker' }, 'Requests today'),
    h('div', { class: 'vrow' }, reqValue), reqSub);
  const peak = h('span', { class: 'r' });
  const chart = hoursChart();
  const hours = h('section', { class: 'tile t-hours' }, h('h2', { class: 'kicker' }, 'Requests per hour', peak), ...chart.el);
  const tokList = h('ul', { class: 'facts' });
  const tokens = h('section', { class: 'tile t-tokens' }, h('h2', { class: 'kicker' }, 'Tokens'), tokList);
  const drawTokens = rebuilder(tokList, factRow);
  const costHead = h('span');
  const costTail = h('span', { class: 'dim' });
  const costValue = h('span', { class: 'v' }, costHead, costTail);
  const clientList = h('ul', { class: 'facts' });
  const mix = costMix();
  const api = h('section', { class: 'tile t-api', title: MIX_TITLE }, h('h2', { class: 'kicker' }, 'Via API'),
    h('div', { class: 'vrow' }, costValue), mix.el, clientList);
  const drawClients = rebuilder(clientList, factRow);
  let costShown = null;  // cost.js: never back within a day
  let owed = null;  // cost.js owedStep: what the changed digits paid off, for their colour
  let tint = null;  // cost.js tintStep: the digits that changed, in the colour of what changed them
  const el = h('div', { class: 'page page-today' }, req, hours, tokens, api);

  function update(v) {
    const t = v.tick && v.tick.today;
    setText(reqValue, int(t ? t.requests : null));
    setText(reqSub, tm.requestsSub(v.usage));
    const hv = tm.hourColumns(v.usage && v.usage.buckets, v.now);
    setText(peak, hv.peak);
    chart.update(hv);
    drawTokens(tm.tokenFacts(t, v.usage));
    // To the 1/10,000 dollar, with running requests added, so it climbs as tokens come (cost.js); the bar
    // under it splits today's cost by kind (cost_mix.js).
    api.hidden = !hasReference(v.pricing);
    el.classList.toggle('no-api', api.hidden);  // the other tiles take the width
    const parts = liveParts(v.tick, v.pricing, v.now);
    const before = costShown ? costShown.units : null;
    costShown = chase(costShown, parts && parts.stored + parts.cached + parts.input + parts.output, storedCost(v.tick),
      Infinity, undefined, v.tick && v.tick.today ? v.tick.today.since : null);
    owed = owedStep(owed, parts, before, costShown ? costShown.units : null);
    mix.set(daySplit(v.tick, v.pricing, parts));
    const chars = costChars(costShown ? costShown.units : null, tint, owed.kind, v.now);
    tint = chars.tint;
    drawChars(costHead, chars.head, chars.headKinds);
    drawChars(costTail, chars.tail, chars.tailKinds);
    fit(costValue, COST_SIZES);
    const c = m.clientRows(v.usage, CLIENT_ROWS);
    drawClients(c.rows.length ? c.rows.map((r) => ({ label: r.name, value: r.requests }))
      : [{ label: c.count === null ? 'No usage data' : 'No client today', value: '' }]);
  }
  return { id: 'today', title: 'Today', el, update };
}
