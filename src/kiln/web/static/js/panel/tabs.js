// The rack panel's tab strip, 60 px on the bottom bezel: the feed state (KILN), the five tabs, the LOCAL or
// VIEW ONLY mark, the sync cell (who set the rack's screen, and when) and the clock. What it says comes
// from tabs_model.js; a draw writes only the text and attributes that changed.
import { h, setText, setAttr } from '../dom.js';
import { TABS } from './tabs_model.js';

const CONN_TEXT = { open: 'Kiln', stale: 'Stale', reconnecting: 'No data', connecting: 'No data' };

export function tabStrip(root) {
  const conn = h('div', { class: 'conn' }, h('i'), h('span'));
  // data-tab: main.js opens the screen on a tap (a tap moves less than 20 px, so a swipe never does).
  const tabs = TABS.map(([id, name]) => h('div', { class: 'tab', role: 'tab', 'data-tab': id }, name));
  const mark = h('div', { class: 'st-mark', hidden: true });
  const head = h('b');
  const line = h('span');
  const sync = h('div', { class: 'sync' }, head, line);
  const clock = h('div', { class: 'clock' });
  root.append(conn, ...tabs, mark, sync, clock);
  return {
    // { conn, screen, tabs: [ids shown], flags: { screen: tone }, mark, cell: { head, line, tone }, clock }
    update(v) {
      setAttr(conn, 'data-state', v.conn);
      setText(conn.lastChild, CONN_TEXT[v.conn] || v.conn);
      tabs.forEach((t) => {
        t.hidden = Boolean(v.tabs) && !v.tabs.includes(t.dataset.tab);
        setAttr(t, 'aria-current', t.dataset.tab === v.screen ? 'true' : null);
        setAttr(t, 'data-flag', (v.flags && v.flags[t.dataset.tab]) || null);  // a corner block: news there
      });
      mark.hidden = !v.mark;
      setText(mark, v.mark || '');
      setText(head, v.cell.head);
      setText(line, v.cell.line);
      setAttr(sync, 'data-tone', v.cell.tone);
      setText(clock, v.clock);
    },
  };
}
