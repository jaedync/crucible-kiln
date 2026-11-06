// The rack panel's Settings screen: auto-switch, the idle return, the screen at night, the alarm mute, and
// a Rules view (local to this session, not a shared screen). Each box carries data-ctl and data-val; main.js passes a tap to `tap`, which returns
// the settings patch to send. The boxes show the settings main.js gives (the server's, with this session's
// change on top until its echo: sync_model.settingsShown). A session that may not act sees them dashed.
import { h, setText, setAttr } from '../dom.js';
import * as sm from './settings_model.js';

// A row of boxes, one per choice; the chosen one is ink. Rebuilt only when a label or a note changes.
function seg(ctl) {
  const el = h('div', { class: 'seg', role: 'radiogroup' });
  let boxes = [];
  return {
    el,
    update(choices, editable) {
      const key = JSON.stringify(choices.map((c) => [c.value, c.label, c.note || '']));
      if (el.dataset.key !== key) {
        el.dataset.key = key;
        boxes = choices.map((c) => h('i', { role: 'radio', 'data-ctl': ctl, 'data-val': c.value }, c.label,
          c.note ? h('small', null, c.note) : null));
        el.replaceChildren(...boxes);
      }
      choices.forEach((c, i) => setAttr(boxes[i], 'aria-checked', c.checked ? 'true' : 'false'));
      setAttr(el, 'aria-disabled', editable ? null : 'true');
    },
  };
}

function choiceTile(cls, label, note, ctl) {
  const meta = h('span', { class: 'r' });
  const row = seg(ctl);
  const el = h('section', { class: `tile s-choice ${cls}` }, h('h2', { class: 'kicker' }, label, meta),
    h('p', { class: 'note' }, note), row.el);
  return { el, meta, row };
}

function ruleTile(id, when) {
  const then = h('div', { class: 'then' });
  const toggle = h('div', { class: 'tg', role: 'switch', 'data-ctl': 'rule', 'data-val': id });
  const el = h('section', { class: 'tile rule' }, h('div', null, h('div', { class: 'when' }, when), then,
    h('div', { class: 'rid' }, id)), toggle);
  return {
    el,
    update(r, editable) {
      setText(then, `→ ${r.then}`);
      setText(toggle, r.on ? 'On' : 'Off');
      setAttr(toggle, 'aria-checked', r.on ? 'true' : 'false');
      setAttr(toggle, 'aria-disabled', editable ? null : 'true');
    },
  };
}

// The alarm mute (settings_model.alarmTile): a button while an alarm or a mute holds, else a dashed note.
function alarmTile() {
  const title = h('b');
  const sub = h('small');
  const el = h('section', { class: 'tile btn s-alarm', role: 'button', 'data-ctl': 'alarm' }, title, sub);
  return {
    el,
    update(a) {
      setText(title, a.title);
      setText(sub, a.sub);
      setAttr(el, 'data-state', a.state);
      setAttr(el, 'data-val', a.action);
      setAttr(el, 'aria-disabled', a.editable ? null : 'true');
    },
  };
}

function mainView() {
  const auto = choiceTile('s-auto', 'Auto-switch', 'Opt-in: the server picks the screen.', 'auto');
  const idle = choiceTile('s-idle', 'Back to Home after', 'No input from any device.', 'idle');
  const night = choiceTile('s-night', 'Screen at night', 'Night window, no alarm.', 'night');
  const sum = h('small');
  const rules = h('section', { class: 'tile btn s-open', role: 'button', 'data-ctl': 'view', 'data-val': 'rules' },
    h('b', null, 'Rules'), sum, h('span', { class: 'go', 'aria-hidden': 'true' }, '›'));
  const alarm = alarmTile();
  const el = h('div', { class: 's-view s-main' }, auto.el, idle.el, night.el, h('div', { class: 's-side' }, rules, alarm.el));
  return {
    el,
    update(v, s, a) {
      auto.row.update(v.auto.choices, v.editable);
      idle.row.update(v.idle.choices, v.editable);
      night.row.update(v.night.choices, v.editable);
      setText(idle.meta, v.idle.other);
      alarm.update(a);
      // The alarm rule apart: the Auto-switch row counts only the rules that auto turns on.
      const alarmRule = v.rules.find((r) => r.id === 'alarm_home');
      const autoOn = v.rules.filter((r) => r.on && r.id !== 'alarm_home').length;
      setText(sum, `${s && s.auto.enabled ? `${autoOn} auto` : 'auto off'} · alarm ${alarmRule && alarmRule.on ? 'on' : 'off'}`);
    },
  };
}

function rulesView() {
  const dwell = h('p', { class: 'note' });
  const about = h('section', { class: 'tile s-about' }, h('h2', { class: 'kicker' }, 'Auto-switch rules'), dwell);
  const back = h('section', { class: 'tile btn s-back', role: 'button', 'data-ctl': 'view', 'data-val': 'main' },
    h('b', null, '‹ Back'), h('small', null, 'to Settings'));
  const tiles = sm.RULES.map(([id, when]) => ruleTile(id, when));
  const quiet = choiceTile('s-quiet', 'After a person changes the screen, no auto change for', '', 'quiet');
  const el = h('div', { class: 's-view s-rules' }, about, back, ...tiles.map((t) => t.el), quiet.el);
  return {
    el,
    update(v, s) {
      setText(dwell, `A rule screen stays ${s ? Math.round(s.min_dwell_s) : '—'} s or more. Alarms go first.`);
      v.rules.forEach((r, i) => tiles[i].update(r, v.editable));
      quiet.row.update(v.quiet.choices, v.editable);
      setText(quiet.meta, v.quiet.other);
    },
  };
}

export function settingsPage() {
  const views = { main: mainView(), rules: rulesView() };
  const el = h('div', { class: 'page page-settings' }, views.main.el, views.rules.el);
  let sub = 'main';
  let shown = null;  // the settings drawn last: a rule's toggle flips what the person saw
  let editable = false;
  let alarmAction = null;  // as drawn last, and only when this session may act

  function update(v) {
    shown = v.settings;
    const view = sm.settingsView(v.settings, v.canEdit);
    editable = view.editable;
    const a = sm.alarmTile({ alarm: v.alarm, mute: v.mute, now: v.now, editable: v.canEdit });
    alarmAction = a.editable ? a.action : null;
    setAttr(el, 'data-ro', editable ? null : '');
    Object.entries(views).forEach(([k, x]) => { x.el.hidden = k !== sub; });
    views[sub].update(view, shown, a);
  }

  // A tap on this page: { settings: patch } or { mute: kind or null } to send, true for a local change of
  // view, or null.
  function tap(target) {
    const box = target && target.closest ? target.closest('[data-ctl]') : null;
    if (!box) return null;
    const { ctl, val } = box.dataset;
    if (ctl === 'view') {
      sub = val === 'rules' ? 'rules' : 'main';
      return true;
    }
    if (ctl === 'alarm') return alarmAction ? { mute: alarmAction === 'unmute' ? null : alarmAction } : null;
    const patch = editable ? sm.patchFor(ctl, val, shown) : null;
    return patch ? { settings: patch } : null;
  }

  // Leaving the screen, or a tap on its own tab, goes back to the main view.
  const leave = () => { sub = 'main'; };
  const open = (name) => { sub = name === 'rules' ? 'rules' : 'main'; };
  return { id: 'settings', title: 'Settings', el, update, tap, leave, open };
}
