// RAM by part and the engine's disk use now (tick.hw.mem): the data behind the two area maps, where each
// part's area is its share of the whole. Pure functions; panels/memory.js draws them.

const GPU_BLUE = '#1d4f91';

// The server's RAM parts (collect/memory.py RAM_BANDS) and their groups. The GPU parts are blue, the PLE
// yellow in both maps, the host's own parts grey; free memory is the paper colour.
export const RAM_BANDS = [
  { key: 'weights', label: 'Model weights', group: 'gpu', color: GPU_BLUE,
    note: 'The engine\'s weight arena (its start line)' },
  { key: 'kv', label: 'KV cache', group: 'gpu', color: '#2f6bb3',
    note: 'Each slot\'s attention KV for its whole context, made when the engine starts '
      + '(its sequence storage line less the fixed state, times the slots)' },
  { key: 'recurrent', label: 'Recurrent state', group: 'gpu', color: '#3d78bf',
    note: 'Each slot\'s linear-attention (Gated DeltaNet) state; a fixed size that does not grow with the '
      + 'context (fixed_state_bytes on the sequence storage line, times the slots)' },
  { key: 'kv_state', label: 'KV cache and state', group: 'gpu', color: '#2f6bb3',
    note: 'Each slot\'s attention KV and recurrent state for its whole context, made when the engine starts '
      + '(its sequence storage line, times the slots). An engine that prints its fixed state shows the two '
      + 'apart' },
  { key: 'cache', label: 'Conversation cache', group: 'gpu', color: '#4f86c9',
    note: 'Saved conversation prefixes, in pinned memory the GPU reads (cache_bytes in the engine\'s last turn)' },
  { key: 'cache_free', label: 'Cache pool, empty', group: 'gpu', color: '#a9c3e6',
    note: 'Pinned memory the conversation cache holds and has not filled; the pool does not shrink '
      + '(cache_pinned_bytes less cache_bytes)' },
  { key: 'checkpoints', label: 'Slot checkpoints', group: 'gpu', color: '#7ea3d6',
    note: 'Three checkpoints per slot and the prefill cut buffer (the engine\'s slot checkpoint lines)' },
  { key: 'stage', label: 'Stage arena', group: 'gpu', color: '#5d7fa8',
    note: 'The shared work area of the attention and MoE stages (its stage arena line)' },
  { key: 'vision_draft', label: 'Vision and draft head', group: 'gpu', color: '#8aa0bd',
    note: 'The vision tower and the draft head\'s weights (their start lines)' },
  { key: 'gpu_other', label: 'Unitemized GPU memory', group: 'gpu', color: '#c8d5e6',
    note: 'GPU memory the engine prints no size for: prefill workspace, scratch rows, MTP caches, plans, '
      + 'the HIP runtime and any other GPU user' },
  { key: 'ple_ram', label: 'PLE in RAM', group: 'files', color: '#f2c230',
    note: 'Pages of the per-layer embedding table that are in the page cache now' },
  { key: 'file_cache', label: 'Other files', group: 'files', color: '#cfc6b3', note: 'Page cache of other files' },
  { key: 'programs', label: 'Programs', color: '#8c8678', note: 'Anonymous and shared memory of processes' },
  { key: 'kernel', label: 'Kernel', color: '#4f4b44', note: 'Slab, page tables and other kernel memory' },
  { key: 'free', label: 'Free', color: '#ebe5d8', note: 'Not in use' },
];

export const GROUPS = {
  gpu: { label: 'GPU memory (GTT)' },
  files: { label: 'Page cache' },
};

export const DISK_BANDS = [
  { key: 'ple_file', label: 'PLE table', color: '#f2c230',
    note: 'The per-layer embedding table the engine maps from disk and reads as it runs' },
  { key: 'states', label: 'Saved conversations', group: 'cap', color: '#4a7cc0',
    note: 'Conversation states (KV) the engine wrote to disk, in its state directory' },
  { key: 'room', label: 'Room under the limit', group: 'cap', color: '#ebe5d8',
    note: 'What the engine may still write before it removes old states' },
];

// A tree of {key, label, value, color, note, children?}: top-level parts, with grouped parts under a group
// node. Parts without a value or with 0 are left out, and so is a group left empty.
function grouped(bands, values, groups) {
  const out = [];
  const byGroup = new Map();
  for (const b of bands) {
    const v = values[b.key];
    if (!(v > 0)) continue;
    const leaf = { key: b.key, label: b.label, value: v, color: b.color, note: b.note };
    if (!b.group) {
      out.push(leaf);
      continue;
    }
    if (!byGroup.has(b.group)) {
      const node = { key: b.group, label: groups[b.group].label, value: 0, children: [] };
      byGroup.set(b.group, node);
      out.push(node);
    }
    const node = byGroup.get(b.group);
    node.children.push(leaf);
    node.value += v;
  }
  return out;
}

export function ramTree(mem) {
  if (!mem || !mem.ram) return [];
  return grouped(RAM_BANDS, mem.ram, GROUPS);
}

// The PLE table, and the engine's limit on saved states split into what it holds and the room left.
export function diskTree(mem) {
  const d = mem && mem.disk;
  if (!d) return [];
  const room = d.states_cap != null && d.states != null ? Math.max(0, d.states_cap - d.states) : null;
  const label = d.states_cap != null ? `Saved-state limit` : 'Saved states';
  return grouped(DISK_BANDS, { ...d, room }, { cap: { label } });
}

export function treeTotal(tree) {
  return tree.reduce((s, n) => s + (n.value > 0 ? n.value : 0), 0);
}

// The narrowest a map with a total gets, so a small one stays visible (its area is then a little too large).
export const MIN_MAP_PX = 24;

// The sizes of the two maps (RAM, disk) so that one GiB covers the same area in both. Side by side they
// share `height` and split the width less the gap; stacked they share `width` and split twice `height`.
// A map with no total gets no room, and the other takes all of it.
export function mapSizes(totals, { width, gap, height, stacked = false }) {
  const t = totals.map((v) => (v > 0 ? v : 0));
  const sum = t[0] + t[1];
  if (!t[0] || !t[1]) {
    const full = t[1] && !t[0] ? 1 : 0;
    return [0, 1].map((i) => (i === full ? { w: width, h: height } : { w: 0, h: 0 }));
  }
  const room = stacked ? 2 * height : width - gap;
  let a = Math.max(MIN_MAP_PX, Math.min(room - MIN_MAP_PX, (room * t[0]) / sum));
  const b = room - a;
  return stacked ? [{ w: width, h: a }, { w: width, h: b }] : [{ w: a, h: height }, { w: b, h: height }];
}

// Squarified treemap (Bruls, Huizing, van Wijk 2000): rectangles in x, y, w, h whose areas are in
// proportion to the values, as close to square as the order allows. Largest first.
export function treemap(nodes, x, y, w, h) {
  const items = nodes.filter((n) => n.value > 0).sort((a, b) => b.value - a.value);
  const total = items.reduce((s, n) => s + n.value, 0);
  if (!total || w <= 0 || h <= 0) return [];
  const scale = (w * h) / total;
  const out = [];
  let rect = { x, y, w, h };
  let row = [];
  for (let i = 0; i < items.length;) {
    const side = Math.min(rect.w, rect.h);
    const next = [...row, items[i]];
    if (!row.length || worst(next, side, scale) <= worst(row, side, scale)) {
      row = next;
      i += 1;
    } else {
      rect = placeRow(row, rect, scale, out);
      row = [];
    }
  }
  if (row.length) placeRow(row, rect, scale, out);
  return out;
}

function worst(row, side, scale) {
  const areas = row.map((n) => n.value * scale);
  const s = areas.reduce((a, b) => a + b, 0);
  return Math.max(...areas.map((a) => Math.max((side * side * a) / (s * s), (s * s) / (side * side * a))));
}

// One row along the rectangle's shorter side; returns the rectangle that is left.
function placeRow(row, rect, scale, out) {
  const s = row.reduce((a, n) => a + n.value * scale, 0);
  if (rect.w >= rect.h) {  // a column at the left
    const cw = s / rect.h;
    let y = rect.y;
    for (const n of row) {
      const hh = (n.value * scale) / cw;
      out.push({ node: n, x: rect.x, y, w: cw, h: hh });
      y += hh;
    }
    return { x: rect.x + cw, y: rect.y, w: rect.w - cw, h: rect.h };
  }
  const rh = s / rect.w;  // a row at the top
  let x = rect.x;
  for (const n of row) {
    const ww = (n.value * scale) / rh;
    out.push({ node: n, x, y: rect.y, w: ww, h: rh });
    x += ww;
  }
  return { x: rect.x, y: rect.y + rh, w: rect.w, h: rect.h - rh };
}

// The whole map: top-level rectangles, and inside each group its parts under a caption strip. A group too
// small for a caption gets none. Every rectangle keeps its depth (0 top level, 1 inside a group).
export function layout(tree, w, h, captionPx = 20, gapPx = 2) {
  const out = [];
  for (const r of treemap(tree, 0, 0, w, h)) {
    out.push({ ...r, depth: 0 });
    if (!r.node.children) continue;
    const cap = r.h > captionPx * 2.5 && r.w > 80 ? captionPx : 0;
    const inner = treemap(r.node.children, r.x + gapPx, r.y + cap + gapPx, r.w - 2 * gapPx, r.h - cap - 2 * gapPx);
    for (const c of inner) out.push({ ...c, depth: 1 });
  }
  return out;
}
