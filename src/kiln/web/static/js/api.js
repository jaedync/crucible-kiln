// REST helpers for /api/v1. Errors carry the status and the server's message so
// panels can show something actionable.

const BASE = 'api/v1/';

export class ApiError extends Error {
  constructor(status, message, body = null) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function parse(res) {
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const msg = (body && (body.error || body.detail || body.message)) || text.slice(0, 200) || res.statusText;
    throw new ApiError(res.status, msg, body);
  }
  return body;
}

// `timeoutMs` aborts a request the server never answers (the rack panel polls every second).
export async function getJSON(path, params = {}, { timeoutMs = null } = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== undefined));
  const url = BASE + path + (qs.toString() ? `?${qs}` : '');
  let res;
  try {
    const signal = timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined;
    res = await fetch(url, { headers: { Accept: 'application/json' }, signal });
  } catch (err) {
    throw new ApiError(0, 'Dashboard unreachable');
  }
  return parse(res);
}

// Actions need X-Kiln-Action; the browser adds the same-origin Origin header itself.
export async function postAction(path, body) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Kiln-Action': '1' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new ApiError(0, 'Dashboard unreachable');
  }
  return parse(res);
}
