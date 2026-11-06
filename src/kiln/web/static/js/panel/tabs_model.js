// The rack panel's tab strip (pure; tools/web_tests/panel_tabs.test.mjs): the five screens, and the sync
// cell that says who set the rack's screen and when. The strip sits on the bottom bezel of every screen but
// Home; on Home a tap shows it for STRIP_MS. The panel draws twice a second, so every text here changes at
// most once a minute: the age is coarse ("3 min ago"), then a clock time.
import { hm } from '../format.js';
import { byText, flagsShown, mode } from './sync_model.js';

export const TABS = [['glance', 'Home'], ['engine', 'Engine'], ['fleet', 'Fleet'], ['today', 'Today'],
  ['settings', 'Settings']];
export const STRIP_MS = 10000;
const NAMES = Object.fromEntries(TABS);

export const tabName = (id) => NAMES[id] || id;

// The Fleet screen needs the GPU schedule ([schedule] in kiln.toml, sent as engine.schedule). Without
// it the tab is hidden, paging skips it, and a shared screen of 'fleet' shows Home.
export const fleetShown = (engine) => Boolean(engine && engine.schedule);

export function tabsShown(fleet) {
  return TABS.map(([id]) => id).filter((id) => fleet || id !== 'fleet');
}

export const screenShown = (screen, tabs) => (tabs.includes(screen) ? screen : 'glance');

// The screen one swipe away from `current` in `tabs` (dir 1 or -1), round the ends.
export function turnTo(current, tabs, dir) {
  const i = Math.max(0, tabs.indexOf(current));
  return tabs[(((i + dir) % tabs.length) + tabs.length) % tabs.length];
}

export function ageText(at, now) {
  if (at === null || at === undefined) return '';
  const s = Math.max(0, now - at);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `since ${hm(at)}`;
}

// The dashboard's flags as { screen: tone }: yellow ('warn') for a change of state, red ('bad') for a fault.
export function tabFlags(flags) {
  return Object.fromEntries((flags || []).map((f) => [f.screen, f.tone]));
}

// Why a flagged screen did not open (live/panel_screen.Flag.cause). The quiet time counts whole minutes.
function causeText(f, now) {
  if (f.cause === 'off') return 'Auto-switch off';
  if (f.cause === 'fault') return 'Fault';
  if (f.cause === 'dwell') return 'Held: a rule just acted';
  const left = typeof f.quiet_until === 'number' ? f.quiet_until - now : 0;
  return left > 0 ? `Manual quiet · ${Math.ceil(left / 60)} m left` : 'Held in the quiet time';
}

// The flag the sync cell names: a fault first, else the newest; none for the screen that shows.
function flagCell(sess, screen, now) {
  const others = flagsShown(sess).filter((f) => f.screen !== screen);
  const f = others.find((x) => x.tone === 'bad') || others[others.length - 1];
  return f ? { head: causeText(f, now), line: f.why, tone: f.tone === 'bad' ? 'bad' : 'hold' } : null;
}

// { head, line, tone }: head is the small upper line, line the larger one under it.
export function syncCell(sess, screen, now) {
  const note = sess.note;
  if (note && note.kind === 'refused') return { head: 'Not changed', line: note.text.replace(/^Not changed: /, ''), tone: 'bad' };
  if (sess.solo) return { head: 'Demo', line: 'not synced', tone: null };
  const shared = sess.shared;
  if (!shared) return { head: 'Connecting', line: '', tone: null };
  if (sess.pending) return { head: 'Sending', line: tabName(sess.pending.screen), tone: null };
  // A pinned or view-only session may show a screen that the rack does not.
  if (mode(sess) !== 'follow' && screen !== shared.screen) {
    return { head: 'Only here', line: `Rack shows ${tabName(shared.screen)}`, tone: null };
  }
  const flag = flagCell(sess, screen, now);
  if (flag) return flag;
  return { head: byText(shared), line: `${tabName(shared.screen)} · ${ageText(shared.at, now)}`, tone: null };
}

export const stripAfterTap = (nowMs) => nowMs + STRIP_MS;

// Off Home the strip always shows. On Home it shows after a tap, and while a note is news: a change by
// another device or by the dashboard, or this session's own refusal.
export function stripShown({ screen, stripUntil, note }, nowMs) {
  return screen !== 'glance' || nowMs < stripUntil || Boolean(note);
}

// What a tap does. `shown`: the strip was on screen; `tab`: the tab under the finger; `current`: the screen.
export function tapAction({ shown, tab, current }) {
  if (!shown) return { strip: true };
  if (tab && tab === current) return { same: true };
  if (tab) return { open: tab };
  return current === 'glance' ? { strip: true } : {};
}
