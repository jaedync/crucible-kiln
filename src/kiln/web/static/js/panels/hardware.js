// Hardware strip: temperatures, power, fan, GPU busy, memory headroom, power profile.
import { h, setText, setAttr } from '../dom.js';
import * as f from '../format.js';

const STALE_S = 15;
// Strix Halo throttles near 100 °C Tctl; warn well before.
const HOT = { tctl_c: [85, 95], edge_c: [80, 90], mem_board_c: [70, 80] };

const CELLS = [
  { key: 'tctl', label: 'Tctl', value: (hw) => f.celsius(hw.tctl_c), sub: (hw) => `edge ${f.celsius(hw.edge_c)}`,
    tone: (hw) => heat(hw.tctl_c, HOT.tctl_c) },
  { key: 'mem', label: 'Memory board', value: (hw) => f.celsius(hw.mem_board_c),
    sub: (hw) => (hw.ambient_c != null ? `ambient ${f.celsius(hw.ambient_c)}` : 'board sensor'),
    tone: (hw) => heat(hw.mem_board_c, HOT.mem_board_c) },
  { key: 'power', label: 'APU power', value: (hw) => (hw.power_w == null ? f.DASH : `${Math.round(hw.power_w)} W`), sub: () => 'package' },
  { key: 'fan', label: 'Fan', value: (hw) => (hw.fan_rpm == null ? f.DASH : `${f.int(hw.fan_rpm)}`), sub: () => 'rpm' },
  { key: 'gpu', label: 'GPU busy', value: (hw) => (hw.gpu_busy_pct == null ? f.DASH : `${Math.round(hw.gpu_busy_pct)}%`),
    bar: (hw) => hw.gpu_busy_pct / 100, sub: () => 'gfx activity' },
  { key: 'gtt', label: 'GTT used', value: (hw) => f.gibNum(hw.gtt_used_bytes, 1), sub: (hw) => `of ${f.gib(hw.gtt_total_bytes, 0)} · ${f.gib(hw.gtt_total_bytes - hw.gtt_used_bytes, 0)} free`,
    bar: (hw) => hw.gtt_used_bytes / hw.gtt_total_bytes, unit: 'GiB' },
  { key: 'ram', label: 'RAM available', value: (hw) => f.gibNum(hw.ram_avail_bytes, 1), sub: () => 'system headroom', unit: 'GiB' },
  { key: 'profile', label: 'Power profile', value: (hw) => hw.profile || f.DASH, sub: () => 'platform profile', text: true },
];

function heat(v, [warn, crit]) {
  if (v == null) return null;
  return v >= crit ? 'bad' : v >= warn ? 'warn' : null;
}

export default {
  id: 'hardware',
  label: 'Hardware',
  mount(el, { store }) {
    const age = h('span', { class: 'hw-age' });
    const cells = CELLS.map((c) => {
      const value = h('span', { class: 'hw-v' });
      const unit = c.unit ? h('span', { class: 'hw-unit' }, c.unit) : null;
      const sub = h('span', { class: 'hw-sub' });
      const bar = c.bar ? h('span', { class: 'bar' }, h('i')) : null;
      const node = h('div', { class: `hw-cell hw-${c.key}${c.text ? ' is-text' : ''}` },
        h('span', { class: 'kicker' }, c.label), h('span', { class: 'hw-vrow' }, value, unit), sub, bar);
      return { c, node, value, sub, bar };
    });
    el.append(h('h2', { class: 'sr-only' }, 'Hardware'), ...cells.map((x) => x.node), h('div', { class: 'hw-cell hw-meta' }, h('span', { class: 'kicker' }, 'Sampled'), age));

    function render() {
      const { tick } = store.get();
      const hw = tick && tick.hw;
      const now = store.now();
      const stale = !hw || !store.isLive() || now - hw.ts > STALE_S;
      el.classList.toggle('is-stale', stale);
      setText(age, hw ? f.ago(hw.ts, now) : f.DASH);
      for (const x of cells) {
        setText(x.value, hw ? x.c.value(hw) : f.DASH);
        setText(x.sub, hw ? x.c.sub(hw) : '');
        setAttr(x.node, 'data-tone', hw && x.c.tone ? x.c.tone(hw) : null);
        if (x.bar) x.bar.firstChild.style.width = hw ? `${Math.max(0, Math.min(100, 100 * x.c.bar(hw)))}%` : '0%';
      }
    }

    store.on('tick', render);
    store.on('conn', render);
    setInterval(render, 1000); // keeps "sampled N s ago" honest between ticks
    render();
  },
};
