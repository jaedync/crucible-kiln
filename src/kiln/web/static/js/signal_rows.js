// Plain-language text for the Crucible server signals (GET /api/v1/signals and the
// tool_call_fallback flag of a request row). Kept apart from the panels so node tests can check it.
// Both signals are counts and flags; Kiln stores no message text for them.
import * as f from './format.js';

export function fallbackBadge(r) {
  if (!r || r.tool_call_fallback !== true) return null;
  return {
    text: 'tool call as text',
    title: 'The model output was not a valid tool call, so the engine returned it as content.',
  };
}

export function errorTitle() {
  return 'Crucible server errors: failures with HTTP 500 or higher, and failures after the stream began. '
    + 'No message text is stored.';
}

// Cells for one day and engine, each { text, tone }. Only a count above zero has a tone,
// so a quiet column never shows red or a yellow mark. bad: errors; warn: fallbacks; client errors: none.
export function daySummary(row) {
  const cell = (n, tone) => ({ text: f.int(n), tone: n > 0 ? tone : '' });
  return {
    errors: cell(row.errors, 'bad'),
    streamErrors: cell(row.stream_errors, 'bad'),
    fallbacks: cell(row.tool_call_fallbacks, 'warn'),
    // Never red: a refused request is the caller's mistake (for example a path the server lacks).
    clientErrors: { text: f.int(row.client_errors), tone: '' },
  };
}

// A day and engine with any count above zero. Quiet rows hide by default.
export function hasSignal(row) {
  return [row.errors, row.stream_errors, row.tool_call_fallbacks, row.client_errors].some((n) => n > 0);
}

export function quietSummary(rows, days) {
  const loud = rows.filter(hasSignal).length;
  if (!rows.length) return '';
  if (!loud) return `No errors or tool calls as text in the last ${days} days (${f.plural(rows.length, 'engine-day')}).`;
  return `${f.plural(loud, 'engine-day')} with signals; ${f.plural(rows.length - loud, 'quiet one')} hidden.`;
}
