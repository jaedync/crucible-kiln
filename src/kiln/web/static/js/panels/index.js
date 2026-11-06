// Panel registry. To add a panel: write a module exporting
// { id, label, mount(el, ctx) }, import it here, and give `.panel-<id>` a grid
// area in css/kiln.css. Panels with `slot` mount into that existing element
// instead of a new grid section.

import health from './health.js';
import today from './today.js';
import hero from './hero.js';
import chart from './chart.js';
import engine from './engine.js';
import hosts from './hosts.js';
import hardware from './hardware.js';
import memory from './memory.js';
import usage from './usage.js';
import activity from './activity.js';
import prices from './prices.js';
import requests from './requests.js';
import cache from './cache.js';
import signals from './signals.js';
import speculation from './speculation.js';

// Order is reading order on a phone; the desktop grid places them by area.
export const PANELS = [health, today, hero, chart, engine, hosts, hardware, memory, usage, activity, prices, requests, cache,
  signals, speculation];
