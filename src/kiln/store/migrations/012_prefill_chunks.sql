-- The prefill chart spread each request's computed tokens evenly over [first token - prefill seconds, first
-- token]. Crucible prefills one request at a time but pauses a long prefill to run a short one first, so two
-- requests' intervals covered the same seconds and the chart counted that engine time twice: the inference host's
-- database held 2,055 tok/s in one second and 2,183 in another for an engine that prefills at
-- about 1,400 tok/s.
-- The engine reports its progress (Crucible per 4,096 computed rows, Halogen per 32,768), with the seconds
-- since its prefill began. reports holds those events until the request's engine line joins them to a row;
-- the row then keeps [[chunk end time, chunk seconds, tokens], ...] in prefill_progress, so series_1s can be
-- rebuilt from requests alone. Requests whose engine reported figures that put their prefill above any real
-- engine rate (a conversation-cache restore logged as prefill work) get no progress and no chart entry.
ALTER TABLE requests ADD COLUMN prefill_progress TEXT;
ALTER TABLE requests ADD COLUMN engine_req TEXT;
CREATE TABLE prefill_chunks (
  container TEXT NOT NULL,
  req       TEXT NOT NULL,               -- the engine's own request id or number, as its lines give it
  ts        REAL NOT NULL,               -- when the engine logged the report (journal time)
  elapsed_s REAL NOT NULL,               -- engine seconds since that request's prefill began
  done      INTEGER NOT NULL,            -- prompt tokens in the cache at the end of that chunk
  total     INTEGER
);
CREATE INDEX prefill_chunks_req ON prefill_chunks (container, req, ts);
CREATE INDEX prefill_chunks_ts ON prefill_chunks (ts);
-- Only rows that have chunks are in this index. Ingest reads the other work of a request's engine on the
-- event loop, and both bounds matter: the read looks for work ending after the request began, so indexing
-- ts_end keeps it to the seconds around the join. Walking every chunked row of that container each time
-- stalled the tick (2,600 to 98,600 VM steps with 3,000 old rows; migration 010).
CREATE INDEX requests_prefill_progress ON requests (container, ts_end) WHERE prefill_progress IS NOT NULL;
