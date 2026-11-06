// Engine day window (tick.engine.schedule): outside it, the GPU is lent to other GPU jobs.
// By day an engine serves; by night the GPU is for the other jobs ("maintenance").
// The user can still start an engine at night: Kiln pauses the other jobs first.

const hmCache = new Map();

// HH:MM in the schedule's own time zone, so "until 09:00" matches the timers on the host.
export function hmIn(ts, tz) {
  if (!hmCache.has(tz)) {
    hmCache.set(tz, new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: tz }));
  }
  return hmCache.get(tz).format(ts * 1000);
}

const jobs = (n) => `${n} other GPU job${n === 1 ? '' : 's'}`;

function labelOf(name, profiles) {
  const p = (profiles || []).find((x) => x.name === name);
  return p ? p.label || p.name : name;
}

// The engine card during the night window: null when there is nothing to say.
// Returns {word, label, detail}; `word` replaces the state word only while no engine runs.
export function windowText(engine, profiles) {
  const s = engine && engine.schedule;
  if (!s || s.error || s.in_day) return null;
  const until = hmIn(s.next_change, s.tz);
  const running = engine.state === 'ready' || engine.state === 'starting' || engine.state === 'stopping';
  const parts = [];
  if (s.day_profile) parts.push(`At ${until}, Kiln starts ${labelOf(s.day_profile, profiles)}.`);
  if (s.dev_jobs && s.dev_jobs.length) parts.push(`${jobs(s.dev_jobs.length)} running now.`);
  if (running) {
    return { word: null, label: null, detail: `Night use: the other GPU jobs are paused while this engine runs. ${parts.join(' ')}`.trim() };
  }
  return { word: 'Maintenance', label: `Maintenance until ${until}. The GPU is lent to other GPU jobs.`, detail: parts.join(' ') };
}

// Extra lines for the switch confirmation, and whether the request must carry confirm: true.
export function confirmNotes(engine) {
  const s = engine && engine.schedule;
  if (!s || s.error) return { notes: [], confirm: false };
  const notes = [];
  if (!s.in_day) notes.push(`The GPU is lent to other GPU jobs until ${hmIn(s.next_change, s.tz)}. This switch pauses them until you stop the engine.`);
  const n = (s.dev_jobs || []).length;
  if (n) notes.push(`${jobs(n)} running. Kiln waits up to ${Math.round(s.drain_timeout_s / 60)} min for ${n === 1 ? 'it' : 'them'} to finish. Kiln does not stop ${n === 1 ? 'it' : 'them'}.`);
  return { notes, confirm: n > 0 };
}
