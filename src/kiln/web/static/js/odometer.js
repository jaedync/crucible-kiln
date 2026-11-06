// A dollar figure on digit wheels (cost.js gives the positions). Each wheel is one digit wide and one line
// high and holds a strip 0-9 then 0 again, moved up by its position, so a 9 rolls on into the 0 above it.
// The last two wheels (below a cent) are grey. A wheel that turns can take the colour of what moved it
// (data-kind, cost.js tintStep). Only transforms and that attribute change between draws: no layout work.
import { h, setAttr } from './dom.js';
import { DASH } from './format.js';
import { DECIMALS, splitUsd, wheels } from './cost.js';

const STRIP = 11;  // 0..9 and the 0 a 9 rolls into
const SUB_CENT = DECIMALS - 2;

export function odometer() {
  const el = h('span', { class: 'odo', role: 'img' });
  let strips = [];
  let wheelEls = [];
  let built = -1;  // wheels built, -1 for the dash

  function build(n) {
    built = n;
    const ints = n - DECIMALS;
    strips = [];
    wheelEls = [];
    const kids = [h('span', { class: 'odo-c' }, '$')];
    for (let i = 0; i < n; i += 1) {
      if (i === ints) kids.push(h('span', { class: 'odo-c' }, '.'));
      else if (i > 0 && i < ints && (ints - i) % 3 === 0) kids.push(h('span', { class: 'odo-c' }, ','));
      const strip = h('span', { class: 'odo-s' }, Array.from({ length: STRIP }, (_, d) => h('span', null, d % 10)));
      const wheel = h('span', { class: `odo-w${i >= n - SUB_CENT ? ' odo-dim' : ''}`, 'aria-hidden': 'true' }, strip);
      strips.push(strip);
      wheelEls.push(wheel);
      kids.push(wheel);
    }
    el.replaceChildren(...kids);
  }

  // set(units | null, kinds): kinds, one per wheel (cost.js tintStep), colour the wheels that turn
  function set(units, kinds = null) {
    if (units === null || units === undefined) {
      if (built !== -1) { built = -1; strips = []; wheelEls = []; el.replaceChildren(DASH); }
      setAttr(el, 'aria-label', DASH);
      return;
    }
    const pos = wheels(units);
    if (pos.length !== built) build(pos.length);
    pos.forEach((r, i) => {
      const t = `translateY(${(-(r / STRIP) * 100).toFixed(3)}%)`;
      if (strips[i].style.transform !== t) strips[i].style.transform = t;
      setAttr(wheelEls[i], 'data-kind', kinds && kinds.length === pos.length ? kinds[i] : null);
    });
    setAttr(el, 'aria-label', splitUsd(units).join(''));
  }
  return { el, set };
}
