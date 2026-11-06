// Plain-language text for one turn of GET /api/v1/cache: why the engine read
// prompt tokens again. Kept apart from the panel so node tests can check it.
import * as f from './format.js';

export const CAUSE = {
  new: { text: 'New conversation', tone: '' },
  ok: { text: 'Cache hit', tone: 'ok' },
  client: { text: 'Client changed its prompt', tone: 'warn' },
  engine: { text: 'Engine cache miss', tone: 'bad' },
  unknown: { text: 'Not known', tone: '' },
};

export function causeTitle(t) {
  if (t.cause === 'client') {
    const what = [];
    if (t.prev_n_messages != null && t.prefix_msgs_kept < t.prev_n_messages) {
      what.push(`kept ${t.prefix_msgs_kept} of its ${t.prev_n_messages} earlier messages`);
    }
    if (t.system_changed) what.push('changed the system prompt');
    if (t.tools_changed) what.push('changed the tool list');
    return `The client ${what.join(', ')}. Any engine must read the changed part again.`;
  }
  if (t.cause === 'engine') {
    return `The client kept all ${t.prev_n_messages} earlier messages, but the engine read ${f.int(t.reread_tokens)} of their tokens again.`;
  }
  if (t.cause === 'unknown') return 'The previous turn was still running or is not known, so Kiln cannot split the cost.';
  return '';
}

// Engine-side detail for the turn, or ''.
export function engineNote(t) {
  const parts = [];
  if (t.cache_save_refused) parts.push(`save refused (${t.cache_save_refused})`);
  if (t.cache_session_label) parts.push(t.cache_session_label.replace(/_/g, ' '));
  if (t.closest_outcome && t.closest_outcome !== 'all') {
    parts.push(`shares ${f.int(t.closest_shared_tokens)} tokens, then the ${t.closest_outcome === 'reply_differs' ? 'reply' : 'prompt'} differs`);
  }
  if (t.cache_evictions) parts.push(`${f.int(t.cache_evictions)} evictions`);
  return parts.join(' · ');
}
