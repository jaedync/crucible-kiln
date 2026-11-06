// Pages of the rack panel, in the tab strip's order (tabs_model.TABS). Each page has `update(view)`, which
// writes only changed text. Only Home's alarm (the mute) and Settings have buttons, and they change panel
// state only: no page acts on the engine.
import { enginePage } from './engine.js';
import { fleetPage } from './fleet.js';
import { glancePage } from './glance.js';
import { settingsPage } from './settings.js';
import { todayPage } from './today.js';

export const PAGES = [glancePage, enginePage, fleetPage, todayPage, settingsPage];
