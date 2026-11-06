// Engine card: active profile, state, KV capacity and exact pool use, and the
// profile switcher with a confirm step and live progress.
import { h, setText, setAttr, panelHead } from '../dom.js';
import * as f from '../format.js';
import { windowText, confirmNotes } from '../schedule.js';

const STATE_TEXT = { ready: 'Ready', starting: 'Starting', stopping: 'Stopping', stopped: 'Stopped', failed: 'Failed' };
const LOG_LINES = 8;

export default {
  id: 'engine',
  label: 'Engine',
  mount(el, { store, api }) {
    const card = stateCard();
    const kv = kvBlock();
    const sw = switcher(store, api);
    el.append(panelHead('Engine'), card.el, kv.el, sw.el);

    function render() {
      const engine = store.get().engine;
      const now = store.now();
      card.render(engine, now, store.get().profiles);
      kv.render(engine, now);
      sw.render();
    }

    async function refresh() {
      try {
        const res = await api.getJSON('engines');
        sw.setServer(res);
        if (res.profiles) store.set({ profiles: res.profiles });
        render();
      } catch (err) {
        sw.setError(`Couldn't load engine list: ${err.message}`);
      }
    }

    store.on('tick', render);
    store.on('conn', render);
    store.on('snapshot', refresh);
    store.on('engine', (msg) => { sw.log(msg); render(); if (msg.engine.state === 'ready' || msg.engine.state === 'failed') refresh(); });
    setInterval(() => card.render(store.get().engine, store.now(), store.get().profiles), 1000);
    render();
  },
};

function stateCard() {
  const word = h('span', { class: 'eng-word' });
  const label = h('p', { class: 'eng-label' });
  const since = h('p', { class: 'eng-since' });
  const detail = h('p', { class: 'eng-detail' });
  const el = h('div', { class: 'eng-state', 'data-state': 'unknown' }, word, label, since, detail);

  function render(e, now, profiles) {
    const night = windowText(e, profiles);
    el.dataset.state = night && night.word ? 'maintenance' : e ? e.state : 'unknown';
    setText(word, night && night.word ? night.word : e ? STATE_TEXT[e.state] || e.state : f.DASH);
    setText(label, night && night.label ? night.label : e ? e.label || e.profile || 'No engine is serving' : 'No engine state yet');
    let s = '';
    if (e && e.since) {
      const verb = e.state === 'ready' ? 'Serving for' : e.state === 'stopped' || e.state === 'failed' ? 'Since' : 'For';
      s = e.state === 'stopped' || e.state === 'failed' ? `${verb} ${f.when(e.since, now)}` : `${verb} ${f.dur(now - e.since)}`;
    }
    if (e && e.target) s += `${s ? ' · ' : ''}switching to ${e.target}`;
    setText(since, s);
    const text = [e ? detailText(e.detail, e.label) : '', night ? night.detail : ''].filter(Boolean).join(' · ');
    setText(detail, text);
    detail.hidden = !text;
  }

  return { el, render };
}

// Engine details often repeat the profile label ("<label> answered a test generation"); drop it.
export function detailText(detail, label) {
  if (!detail) return '';
  if (!label || !detail.startsWith(label)) return detail;
  const rest = detail.slice(label.length).replace(/^[\s·:,-]+/, '');
  return rest ? rest[0].toUpperCase() + rest.slice(1) : '';
}

function kvBlock() {
  const cap = h('span', { class: 'kv-cap' });
  const capSub = h('span', { class: 'kv-sub' });
  const bar = h('span', { class: 'bar kv-bar' }, h('i'));
  const pool = h('p', { class: 'kv-pool' });
  const el = h('div', { class: 'kv' },
    h('p', { class: 'kicker' }, 'KV cache capacity'),
    h('p', { class: 'kv-line' }, cap, capSub), bar, pool);

  function render(e, now) {
    const kv = e && e.kv;
    setText(cap, kv ? `${f.int(kv.capacity_positions)} tokens` : f.DASH);
    // Halogen publishes no bytes per position, so capacity_bytes may be null.
    setText(capSub, kv && kv.capacity_bytes != null ? f.gib(kv.capacity_bytes) : '');
    // Pool use only from the engine's own report, always with its age.
    const known = kv && kv.pool_used !== null && kv.pool_used !== undefined && kv.pool_total;
    bar.hidden = !known;
    if (known) {
      bar.firstChild.style.width = `${Math.min(100, (100 * kv.pool_used) / kv.pool_total)}%`;
      setText(pool, `${f.int(kv.pool_used)} / ${f.int(kv.pool_total)} positions used · ${f.pct(kv.pool_used / kv.pool_total)} · as of ${f.ago(kv.pool_ts, now)}`);
    } else {
      setText(pool, kv ? 'Pool use: not reported by this engine.'
        : e && !e.profile ? 'No engine is loaded.' : '');
    }
  }

  return { el, render };
}

const ENGINE_TITLE = { halogen: 'Halogen', gufo: 'Gufo', crucible: 'Crucible', llamacpp: 'llama.cpp' };

// Profiles grouped by engine in config order; profiles that can't be selected yet go last
// in each group, behind a disclosure. The active profile always shows.
export function profileGroups(profiles, activeName) {
  const groups = new Map();
  for (const p of profiles) {
    if (!groups.has(p.engine)) {
      groups.set(p.engine, { engine: p.engine, title: ENGINE_TITLE[p.engine] || p.engine, ready: [], unavailable: [] });
    }
    const g = groups.get(p.engine);
    (p.disabled && p.name !== activeName ? g.unavailable : g.ready).push(p);
  }
  return [...groups.values()];
}

function switcher(store, api) {
  let server = { blocked: null, events: [] };
  let selected = null;
  let pending = false;
  let mustConfirm = false; // the server asked for confirmation (development GPU jobs run)
  let message = null; // {tone, text}
  let lines = [];
  let listKey = '';

  const list = h('div', { class: 'profiles' });
  // The list scrolls inside the engine card (css), so a long profile list never makes the page taller.
  const scroller = h('div', { class: 'profiles-scroll', tabindex: '0', role: 'region', 'aria-label': 'Engine profiles' }, list);
  let scrolledTo = null;
  const moreOpen = new Set();
  const reason = h('p', { class: 'sw-reason' });
  const confirmText = h('p', { class: 'sw-confirm-text' });
  const goBtn = h('button', { type: 'button', class: 'btn btn-red', onclick: go }, 'Switch engine');
  const cancelBtn = h('button', { type: 'button', class: 'btn', onclick: cancel }, 'Cancel');
  const notesList = h('ul', { class: 'sw-notes' });
  const confirm = h('div', { class: 'sw-confirm', hidden: true }, confirmText, notesList, h('div', { class: 'sw-actions' }, goBtn, cancelBtn));
  const msg = h('p', { class: 'sw-msg', role: 'status' });
  const log = h('ol', { class: 'sw-log', 'aria-label': 'Switch progress' });
  const el = h('div', { class: 'switcher' }, h('p', { class: 'kicker' }, 'Switch profile'), reason, scroller, confirm, msg, log);

  // Cancel hides the confirm box; focus goes back to the profile the user had picked.
  function cancel() {
    const was = selected;
    selected = null;
    mustConfirm = false;
    render();
    const btn = [...list.querySelectorAll('.profile')].find((b) => b.dataset.profile === was);
    if (btn && !btn.disabled) btn.focus();
  }

  function disabledReason() {
    const { me, engine, conn } = store.get();
    if (conn !== 'open') return 'Switching needs a live connection.';
    if (!me || !me.can_act) return `View only: ${me && me.identity ? me.identity : 'this device'} isn't allowed to switch engines.`;
    // tick.engine.blocked is fresh (~5 s); GET /engines covers servers without it.
    const blocked = engine && engine.blocked !== undefined ? engine.blocked : server.blocked;
    if (blocked) return `Blocked: ${blocked}.`;
    if (engine && (engine.target || engine.state === 'starting' || engine.state === 'stopping')) return 'A switch is in progress.';
    return null;
  }

  function render() {
    const { profiles } = store.get();
    const why = disabledReason();
    setText(reason, why || '');
    reason.hidden = !why;
    if (why) selected = null;
    // Rebuild buttons only when something they show changed, so a 2 Hz tick never
    // steals keyboard focus.
    const activeName = store.get().engine ? store.get().engine.profile : '';
    const key = JSON.stringify([profiles, activeName, selected, why]);
    if (key !== listKey) {
      listKey = key;
      list.replaceChildren(...profileGroups(profiles, activeName).map((g) => groupBlock(g, why)));
      // Bring the active profile into view once per active profile, without scrolling the page.
      const act = list.querySelector('.profile.is-active');
      if (act && scrolledTo !== activeName) {
        scrolledTo = activeName;
        requestAnimationFrame(() => {
          const top = act.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
          if (top < scroller.scrollTop || top + act.offsetHeight > scroller.scrollTop + scroller.clientHeight) {
            scroller.scrollTop = Math.max(0, top - 8);
          }
        });
      }
    }
    confirm.hidden = !selected;
    if (selected) {
      const { engine, tick } = store.get();
      const inflight = tick && tick.live ? tick.live.in_flight : 0;
      const target = profiles.find((p) => p.name === selected);
      const current = engine && (engine.label || engine.profile);
      setText(confirmText, `${current ? `Stop ${current} and start` : 'Start'} ${target ? target.label : selected}?` +
        (inflight ? ` ${inflight} request${inflight > 1 ? 's' : ''} in flight will be cut off.` : ' Nothing is in flight.'));
      const { notes, confirm: needed } = confirmNotes(engine);
      const key = notes.join('|');
      if (notesList.dataset.key !== key) {
        notesList.dataset.key = key;
        notesList.replaceChildren(...notes.map((n) => h('li', null, n)));
      }
      notesList.hidden = !notes.length;
      setText(goBtn, needed || mustConfirm ? 'Pause fleet and switch' : 'Switch engine');
      goBtn.disabled = pending;
    }
    setText(msg, message ? message.text : '');
    msg.dataset.tone = message ? message.tone : '';
    if (lines.length) log.hidden = false;
  }

  function groupBlock(g, why) {
    const items = h('ul', { class: 'pf-items', role: 'list' }, g.ready.map((p) => profileItem(p, why)));
    // The list is rebuilt when profiles or state change; keep each disclosure as the user left it.
    const more = g.unavailable.length ? h('details', { class: 'pf-more', open: moreOpen.has(g.engine) || null,
      ontoggle: (e) => { if (e.target.open) moreOpen.add(g.engine); else moreOpen.delete(g.engine); } },
      h('summary', null, `${g.unavailable.length} not available yet`),
      h('ul', { class: 'pf-items', role: 'list' }, g.unavailable.map((p) => profileItem(p, why)))) : null;
    return h('section', { class: 'pf-group' }, h('h3', { class: 'pf-group-title' }, g.title), items, more);
  }

  function profileItem(p, why) {
    // Live engine state outranks the profile list's `active` flag, which can lag a switch.
    const engine = store.get().engine;
    const active = engine ? engine.profile === p.name : p.active;
    const btn = h('button', {
      type: 'button', class: `profile${active ? ' is-active' : ''}`, 'data-profile': p.name,
      'aria-pressed': String(selected === p.name),
      'aria-current': active ? 'true' : null,
      disabled: Boolean(why) || active || Boolean(p.disabled),
      onclick: () => { selected = p.name; mustConfirm = false; message = null; render(); goBtn.focus(); },
    },
    h('span', { class: 'pf-label' }, p.label || p.name),
    h('span', { class: 'pf-meta' }, p.disabled ? `Not available yet: ${p.disabled}`
      : `${p.engine} · ${f.compact(p.context)} ctx · ${p.slots} slots · ${p.memory_gib} GiB`),
    active ? h('span', { class: 'pf-active' }, 'Active') : null);
    return h('li', null, btn);
  }

  async function go() {
    if (!selected || pending) return;
    pending = true;
    message = { tone: 'info', text: `Requesting switch to ${selected}…` };
    // Clear before the POST: the first engine event can beat the 202 response.
    lines = [];
    log.replaceChildren();
    render();
    try {
      // The confirmation step showed the fleet notes, so a click here is the user's confirmation.
      const confirmed = mustConfirm || confirmNotes(store.get().engine).confirm;
      // remember: the morning start uses the profile the user last chose here.
      await api.postAction('engines/switch', { profile: selected, confirm: confirmed, remember: true });
      message = { tone: 'ok', text: 'Switch accepted. Progress appears below.' };
      selected = null;
      mustConfirm = false;
    } catch (err) {
      if (err.status === 409 && err.body && err.body.confirm) {
        // A development job started after the notes were drawn: keep the selection and ask again.
        mustConfirm = true;
        message = { tone: 'info', text: `Confirm: ${err.message}.` };
      } else {
        const prefix = err.status === 403 ? 'Not allowed' : err.status === 409 ? 'Refused' : 'Failed';
        message = { tone: 'bad', text: `${prefix}: ${err.message}` };
      }
    } finally {
      pending = false;
      render();
    }
  }

  function showHistory(events) {
    // /engines lists events newest first; show the latest four, oldest at the top like the live log.
    const recent = events.slice(0, 4).reverse();
    log.replaceChildren(...recent.map((e) => h('li', { 'data-state': e.kind },
      h('time', null, f.when(e.ts, store.now())), ' ',
      [e.kind, e.profile].filter(Boolean).join(' ') + (e.detail ? ` · ${e.detail}` : ''))));
    log.hidden = !recent.length;
  }

  function addLog(text, state) {
    lines = [...lines, { ts: store.now(), text, state }].slice(-LOG_LINES);
    log.replaceChildren(...lines.map((l) => h('li', { 'data-state': l.state }, h('time', null, f.clock(l.ts)), ' ', l.text)));
  }

  return {
    el,
    render,
    setServer(res) {
      server = res;
      if (message && message.stale) message = null;
      // Until this page sees a switch itself, show the server's recent engine history.
      if (!lines.length) showHistory(res.events || []);
    },
    setError(text) { message = { tone: 'bad', text, stale: true }; render(); },
    log(evt) { addLog(evt.log || [evt.engine.state, evt.engine.profile].filter(Boolean).join(' '), evt.engine.state); },
  };
}
