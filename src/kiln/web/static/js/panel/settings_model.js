// The rack panel's Settings screen (pure; tools/web_tests/panel_settings.test.mjs): rows of boxes for the
// shared panel settings (live/panel_screen.py). Settings are the only buttons on the panel, and they change
// only how the panel itself behaves, so a tap acts at once with no confirm step.
import { tabName } from './tabs_model.js';

const IDLE_NEVER = 0;  // live/panel_screen.IDLE_NEVER
export const IDLE_CHOICES = [[60, '1 m'], [300, '5 m'], [900, '15 m'], [IDLE_NEVER, 'Never']];
export const QUIET_CHOICES = [[120, '2 m'], [300, '5 m'], [900, '15 m'], [3600, '1 h']];
// live/panel_screen.NIGHT_DISPLAYS. "Off" waits for a check of the display's power control.
export const NIGHT_CHOICES = [['normal', 'Normal'], ['dim', 'Dim']];
// The auto-switch rules, in the order of panel_screen.RULES, after the alarm rule (on by default, and it
// works without auto-switch).
export const RULES = [
  ['alarm_home', 'An alarm begins'], ['engine_switch', 'Engine starts or switches'],
  ['night_start', 'The night window begins'], ['day_start', 'The day window begins']];
const AUTO_RULES = RULES.slice(1).map(([id]) => id);

const minutes = (s) => (s % 3600 === 0 && s >= 3600 ? `${s / 3600} h` : `${Math.round(s / 60)} m`);

function choices(list, value) {
  const boxes = list.map(([v, label]) => ({ value: String(v), label, checked: v === value }));
  const other = value === undefined || value === null || boxes.some((c) => c.checked) ? '' : `now ${minutes(value)}`;
  return { choices: boxes, other };
}

const ruleOn = (s, id) => (id === 'alarm_home' ? s.alarm_home === true : Boolean(s.auto.rules[id] && s.auto.rules[id].on));
const ruleThen = (s, id) => (id === 'alarm_home' ? 'Home' : tabName((s.auto.rules[id] || {}).screen || ''));

// settings: the shared settings (or null before the first snapshot); editable: this session may change them.
export function settingsView(settings, editable) {
  const s = settings && settings.auto && settings.auto.rules ? settings : null;
  const on = s ? AUTO_RULES.filter((id) => ruleOn(s, id)).length : 0;
  return {
    editable: Boolean(s) && editable,
    auto: { choices: [
      { value: 'off', label: 'Off', note: '', checked: Boolean(s) && !s.auto.enabled },
      { value: 'on', label: 'On', note: `${on} rule${on === 1 ? '' : 's'}`, checked: Boolean(s) && s.auto.enabled }] },
    idle: choices(IDLE_CHOICES, s ? s.idle_home_s : null),
    quiet: choices(QUIET_CHOICES, s ? s.manual_quiet_s : null),
    // A dashboard of step 2 has no night_display: it draws normal.
    night: { choices: NIGHT_CHOICES.map(([v, label]) => ({ value: v, label,
      checked: Boolean(s) && (s.night_display || 'normal') === v })), other: '' },
    rules: RULES.map(([id, when]) => ({ id, when, on: Boolean(s) && ruleOn(s, id), then: s ? ruleThen(s, id) : '' })),
  };
}

const pick = (list, raw) => {
  const hit = list.find(([v]) => String(v) === raw);
  return hit ? hit[0] : null;
};

// The patch for a tap on a box (data-ctl, data-val), or null for anything the screen does not offer.
export function patchFor(ctl, val, settings = null) {
  if (ctl === 'auto' && (val === 'on' || val === 'off')) return { auto: { enabled: val === 'on' } };
  if (ctl === 'idle') {
    const v = pick(IDLE_CHOICES, val);
    return v === null ? null : { idle_home_s: v };
  }
  if (ctl === 'night') {
    const v = pick(NIGHT_CHOICES, val);
    return v === null ? null : { night_display: v };
  }
  if (ctl === 'quiet') {
    const v = pick(QUIET_CHOICES, val);
    return v === null ? null : { manual_quiet_s: v };
  }
  if (ctl !== 'rule' || !RULES.some(([id]) => id === val) || !settings || !settings.auto) return null;
  const flip = !ruleOn(settings, val);
  return val === 'alarm_home' ? { alarm_home: flip } : { auto: { rules: { [val]: { on: flip } } } };
}

const ALARM_NAMES = { hot: 'GPU too hot', failed: 'Engine failed' };

// The Alarm tile: what holds and the one thing to do about it. `alarm`: the alarm that takes the screen
// (glance_model.alarmInfo, with the mute applied); `mute`: the mute in force. action: the kind to mute,
// 'unmute', or null. NO DATA cannot be muted, so it offers nothing.
export function alarmTile({ alarm, mute, now, editable }) {
  if (mute) {
    const left = Math.max(1, Math.ceil((mute.until - now) / 60));
    return { state: 'muted', title: 'Unmute', sub: `${ALARM_NAMES[mute.kind] || mute.kind} · muted ${left} m`,
      action: 'unmute', editable: Boolean(editable) };
  }
  if (alarm && ALARM_NAMES[alarm.kind]) {
    return { state: 'alarm', title: 'Mute 30 min', sub: ALARM_NAMES[alarm.kind], action: alarm.kind,
      editable: Boolean(editable) };
  }
  return { state: 'none', title: 'Alarm', sub: alarm ? 'no data: not muted' : 'no alarm now', action: null,
    editable: false };
}
