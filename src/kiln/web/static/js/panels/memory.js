// Memory and disk now: two area maps under the hardware strip, on one scale (a GiB covers the same area in
// both). RAM by part (the engine's weights, conversation cache and other GPU memory, the PLE pages in RAM,
// the host's own parts, free), and what the engine keeps on disk while it runs (the PLE table it reads
// from, its saved conversations inside their limit); disk parts are hatched. A new reading comes every
// 10 s (tick.hw.mem).
import { h, setText, panelHead } from '../dom.js';
import * as f from '../format.js';
import { RAM_BANDS, DISK_BANDS, ramTree, diskTree, layout, mapSizes, treeTotal } from '../memory_model.js';

const STALE_S = 35;  // three missed readings
// Text on these fills is white; on the others ink.
const DARK = new Set(['#1d4f91', '#2f6bb3', '#3d78bf', '#4f86c9', '#5d7fa8', '#4a7cc0', '#4f4b44', '#8c8678']);
const CAPTION_PX = 20;
const GAP_PX = 40;      // between the two maps side by side (the CSS gap of .mem-grid)
const STACK_GAP_PX = 24;
const BORDER_PX = 4;    // the map's 2 px frame on both sides
const EMPTY_DISK_PX = 260;  // the disk block's width when it holds only its message

export default {
  id: 'memory',
  label: 'Memory and disk',
  mount(el, { store }) {
    const age = h('span', { class: 'panel-sub mem-age' });
    const ram = mapBlock('RAM', RAM_BANDS, [], { shares: true });
    const disk = mapBlock('Engine disk', DISK_BANDS, [
      { key: 'read_bps', label: 'Reads', note: 'The engine\'s disk reads, mostly PLE rows that are not in RAM' },
      { key: 'write_bps', label: 'Writes', note: 'The engine\'s disk writes: saved conversations' },
    ], { shares: false, disk: true });
    const grid = h('div', { class: 'mem-grid' }, ram.el, disk.el);
    el.append(panelHead('Memory and disk', age), grid);
    let mem = null;

    function render() {
      const { tick } = store.get();
      const next = tick && tick.hw ? tick.hw.mem || null : null;
      const now = store.now();
      el.classList.toggle('is-stale', !next || !store.isLive() || now - next.ts > STALE_S);
      setText(age, next ? `Read ${f.ago(next.ts, now)}` : 'No reading yet');
      if (next === mem) return;
      mem = next;
      fillRam();
      fillDisk();
      place();
    }

    function fillRam() {
      const r = mem && mem.ram;
      ram.tree = ramTree(mem);
      setText(ram.total, r && r.total ? f.gib(r.total, 1) : '');
      for (const b of RAM_BANDS) {
        const v = r ? r[b.key] : null;
        setText(ram.values[b.key], f.gib(v, 1));
        setText(ram.shares[b.key], v == null || !r.total ? '' : v / r.total < 0.005 ? '<1%' : f.pct(v / r.total));
        ram.items[b.key].hidden = !(v > 0);
      }
      setText(ram.empty, r ? '' : 'No reading yet. The dashboard reads memory every 10 s.');
    }

    function fillDisk() {
      const d = (mem && mem.disk) || {};
      const room = d.states_cap != null && d.states != null ? Math.max(0, d.states_cap - d.states) : null;
      disk.tree = diskTree(mem);
      const total = treeTotal(disk.tree);
      setText(disk.total, total ? f.gib(total, 1) : '');
      setText(disk.values.ple_file, f.gib(d.ple_file, 1));
      setText(disk.values.states, f.gib(d.states, 1));
      setText(disk.values.room, f.gib(room, 1));
      setText(disk.values.read_bps, bytesRate(mem && mem.read_bps));
      setText(disk.values.write_bps, bytesRate(mem && mem.write_bps));
      disk.items.ple_file.hidden = d.ple_file == null;
      disk.items.states.hidden = d.states == null;
      disk.items.room.hidden = room == null;
      disk.items.read_bps.hidden = !mem || mem.read_bps == null;
      disk.items.write_bps.hidden = !mem || mem.write_bps == null;
      disk.el.hidden = !mem;
      setText(disk.note, mem && !total ? 'No engine disk use: the engine is stopped or keeps nothing on disk.' : '');
    }

    // Sizes both maps on one scale, side by side when each map is at least as wide as its legend, else
    // stacked; then draws them.
    function place() {
      const width = grid.clientWidth;
      if (!width) return;
      const totals = [treeTotal(ram.tree), disk.el.hidden ? 0 : treeTotal(disk.tree)];
      const diskOnlyNote = !disk.el.hidden && !totals[1];
      const avail = width - BORDER_PX * 2 - (diskOnlyNote ? EMPTY_DISK_PX + GAP_PX : 0);
      const height = Math.round(Math.min(300, Math.max(220, window.innerHeight * 0.28)));
      let stacked = false;
      let sizes = mapSizes(totals, { width: avail, gap: GAP_PX, height });
      if (sizes[0].w + BORDER_PX < legendWidth(ram) || (totals[1] && sizes[1].w + BORDER_PX < legendWidth(disk))) {
        stacked = true;
        sizes = mapSizes(totals, { width: width - BORDER_PX, gap: STACK_GAP_PX, height: Math.round(height * 0.8),
          stacked: true });
      }
      grid.classList.toggle('is-stacked', stacked);
      size(ram, sizes[0], stacked);
      if (diskOnlyNote) {
        disk.el.style.width = stacked ? '' : `${EMPTY_DISK_PX}px`;
        disk.map.hidden = true;
      } else {
        size(disk, sizes[1], stacked);
      }
    }

    function size(block, s, stacked) {
      block.map.hidden = !(s.w > 0 && s.h > 0);
      block.el.style.width = stacked ? '' : `${Math.round(s.w + BORDER_PX)}px`;
      block.map.style.height = `${Math.round(s.h + BORDER_PX)}px`;
      paint(block, block.tree, Math.round(s.w), Math.round(s.h));
    }

    new ResizeObserver(() => place()).observe(grid);
    window.addEventListener('resize', place);
    if (document.fonts) document.fonts.ready.then(() => { ram.labelPx = null; disk.labelPx = null; place(); });
    store.on('tick', render);
    store.on('conn', render);
    setInterval(render, 1000);  // keeps "read N s ago" honest between ticks
    render();
  },
};

// The legend's width with every row shown, so a row that comes or goes never changes it.
function legendWidth(block) {
  if (block.labelPx == null) {
    const hidden = Object.values(block.items).filter((it) => it.hidden);
    for (const it of hidden) it.hidden = false;
    block.list.style.gridTemplateColumns = '';
    const label = Math.max(0, ...block.labels.map((l) => l.getBoundingClientRect().width));
    for (const it of hidden) it.hidden = true;
    if (label > 0) {
      block.labelPx = Math.ceil(label);
      block.list.style.gridTemplateColumns = block.columns(block.labelPx);
    }
  }
  return block.list.getBoundingClientRect().width;
}

// Draws the area map of a tree into its block: one box per part, groups with a caption, labels where
// they fit. Boxes are rebuilt each time: a map has a dozen at most and changes every 10 s.
function paint(block, tree, w, hgt) {
  const boxes = layout(tree, w, hgt, CAPTION_PX).map((r) => box(r));
  block.map.replaceChildren(...boxes, block.empty);
}

function box(r) {
  const n = r.node;
  const group = !!n.children;
  const el = h('div', {
    class: `mem-box${group ? ' is-group' : ''}`,
    title: `${n.label}: ${f.gib(n.value, 1)}${n.note ? `. ${n.note}` : ''}`,
    style: `left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;${n.color ? `background-color:${n.color};` : ''}`,
  });
  if (!group && DARK.has(n.color)) el.classList.add('is-dark');
  if (group) {
    if (r.h > CAPTION_PX * 2.5 && r.w > 80) {
      el.append(h('span', { class: 'mb-cap' }, h('b', null, n.label), ` ${f.gib(n.value, 1)}`));
    }
    return el;
  }
  if (r.w >= 60 && r.h >= 38) {
    el.append(h('span', { class: 'mb-name' }, n.label), h('span', { class: 'mb-v' }, f.gib(n.value, 1)));
  } else if (r.w >= 52 && r.h >= 18) {
    el.append(h('span', { class: 'mb-v' }, f.gib(n.value, 1)));
  }
  return el;
}

function bytesRate(bps) {
  if (bps == null) return f.DASH;
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(1)} GB/s`;
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(bps >= 1e8 ? 0 : 1)} MB/s`;
  if (bps >= 1e3) return `${Math.round(bps / 1e3)} kB/s`;
  return `${Math.round(bps)} B/s`;
}

// A titled map with its legend under it: one row per part (swatch, name, value, and with `shares` the
// part's share), then any extra rows. Values sit in fixed boxes, so a changing digit moves nothing.
function mapBlock(title, bands, extras, { shares, disk = false }) {
  const total = h('span', { class: 'mem-total' });
  const empty = h('p', { class: 'mem-empty' });
  const note = h('p', { class: 'mem-note' });
  const map = h('div', { class: `mem-map${disk ? ' is-disk' : ''}`, role: 'img',
    'aria-label': `${title} by part; the list under it has the sizes` }, empty);
  const values = {}, shareCells = {}, items = {}, labels = [];
  const row = (b) => {
    values[b.key] = h('span', { class: 'ml-v' });
    const mark = b.color ? h('i', { class: 'swatch', 'aria-hidden': 'true', style: `background-color:${b.color}` })
      : h('span', { 'aria-hidden': 'true' });
    const label = h('span', { class: 'ml-label' }, b.label);
    labels.push(label);
    const cells = [mark, label, values[b.key]];
    if (shares) {
      shareCells[b.key] = h('span', { class: 'ml-share' });
      cells.push(shareCells[b.key]);
    }
    items[b.key] = h('li', { class: 'ml-item', title: b.note }, ...cells);
    return items[b.key];
  };
  const list = h('ul', { class: `mem-legend${shares ? '' : ' no-share'}${disk ? ' is-disk' : ''}` },
    ...bands.map(row), ...extras.map(row));
  const columns = (px) => `12px ${px}px 9ch${shares ? ' 4ch' : ''}`;
  const el = h('div', { class: 'mem-block' },
    h('h3', { class: 'kicker mem-head' }, title, ' ', total), map, note, list);
  return { el, map, list, total, empty, note, values, shares: shareCells, items, labels, columns,
    tree: [], labelPx: null };
}
