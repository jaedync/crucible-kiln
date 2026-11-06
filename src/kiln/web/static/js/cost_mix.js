// The bar under the Via API figure: today's cost by kind (cost.js daySplit), one segment per kind, as wide
// as its share. Blue is output (decode), yellow new prompt tokens (prefill), striped yellow cache reads. Only
// widths change between draws.
import { h } from './dom.js';
import { BAR_KINDS } from './cost.js';

export const MIX_TITLE = 'Today\'s cost by kind. Blue: output (decode). Yellow: new prompt tokens (prefill). '
  + 'Striped: cached prompt tokens. A digit that turns flashes in the colour of what turns it now (dark yellow '
  + 'for prefill).';

export function costMix() {
  const parts = Object.fromEntries(BAR_KINDS.map((k) => [k, h('i', { 'data-kind': k })]));
  const el = h('span', { class: 'cost-mix', 'aria-hidden': 'true' }, ...BAR_KINDS.map((k) => parts[k]));
  // set(split | null): split from cost.js daySplit; hidden while the day has no cost
  function set(split) {
    const sum = split ? BAR_KINDS.reduce((n, k) => n + split[k], 0) : 0;
    const op = sum > 0 ? '1' : '0';
    if (el.style.opacity !== op) el.style.opacity = op;
    if (op === '0') return;
    for (const k of BAR_KINDS) {
      const w = `${((split[k] / sum) * 100).toFixed(1)}%`;
      if (parts[k].style.width !== w) parts[k].style.width = w;
    }
  }
  return { el, set };
}
