import { createStore } from './store.js';
import { connectLive } from './ws.js';
import * as api from './api.js';
import { h } from './dom.js';
import { PANELS } from './panels/index.js';
import { showInTab } from './title.js';

const store = createStore();
const ctx = { store, api };
const grid = document.getElementById('main');

for (const panel of PANELS) {
  const el = panel.slot
    ? document.querySelector(panel.slot)
    : grid.appendChild(h('section', { class: `panel panel-${panel.id}`, 'aria-label': panel.label }));
  try {
    panel.mount(el, ctx);
  } catch (err) {
    // One broken panel must not take the dashboard down with it.
    console.error(`kiln: panel ${panel.id} failed to mount`, err);
    el.append(h('p', { class: 'panel-error' }, `${panel.label} failed to load. See the browser console.`));
  }
}

showInTab(store);
connectLive(store);
