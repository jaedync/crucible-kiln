-- Crucible server signals (numbers and booleans only, never message text).
-- tool_call_fallback: usage.tool_call_fallback of the request (1: the model output was not a valid tool call and
-- went back as content; 0: no fallback; NULL: unknown, a row from before this column).
ALTER TABLE requests ADD COLUMN tool_call_fallback INTEGER;
-- One row per crucible_error log line. The line's own error text is never stored: it is the server's message
-- and could echo client input. kind is derived from status, streamed and one fixed marker.
CREATE TABLE crucible_errors (
  id                TEXT PRIMARY KEY,   -- derived from container and log time, so a replayed line is a no-op
  ts                REAL NOT NULL,
  host              TEXT NOT NULL,
  profile           TEXT,
  engine            TEXT NOT NULL,
  container         TEXT,
  request_id        TEXT,               -- the server's own id (pattern-checked), NULL when absent
  status            INTEGER NOT NULL,
  streamed          INTEGER NOT NULL,
  kind              TEXT NOT NULL,      -- server_error | stream_error | client_error | tool_call_fallback
  completion_tokens INTEGER
);
CREATE INDEX crucible_errors_ts ON crucible_errors (ts);
