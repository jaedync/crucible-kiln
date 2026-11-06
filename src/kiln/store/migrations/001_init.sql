-- Kiln schema v1. Times are UTC epoch seconds. Message content is never stored.

CREATE TABLE requests (
  id                    TEXT PRIMARY KEY,
  source                TEXT NOT NULL,        -- gateway | direct (engine log only, bypassed the gateway)
                                              -- | engine_pending (engine line waiting for its gateway record)
  host                  TEXT NOT NULL,
  container             TEXT,
  ts_start              REAL NOT NULL,
  ts_first_token        REAL,
  ts_last_token         REAL,
  ts_end                REAL,
  client_key            TEXT,
  client_name           TEXT,
  client_label          TEXT,
  client_node           TEXT,
  peer_ip               TEXT,
  profile               TEXT,
  engine                TEXT,
  model                 TEXT,
  path                  TEXT,
  status                INTEGER,
  finish                TEXT,
  error                 TEXT,
  prompt                INTEGER,
  cached                INTEGER,
  completion            INTEGER,
  reasoning             INTEGER,
  token_events          INTEGER,
  ttft_s                REAL,
  latency_s             REAL,
  max_tokens            INTEGER,
  reasoning_effort      TEXT,
  -- from the engine's own log line, joined after the fact
  engine_prefill_tokens INTEGER,
  engine_prefill_s      REAL,
  engine_decode_s       REAL,
  engine_queue_s        REAL,
  spec_rounds           INTEGER,
  draft_proposed        INTEGER,
  draft_accepted        INTEGER,
  think_close           TEXT,
  kv_pool_used          INTEGER,
  kv_pool_total         INTEGER,
  engine_joined_at      REAL,
  engine_line_id        TEXT,                 -- id of the engine line joined here (makes replays idempotent)
  series_done           INTEGER NOT NULL DEFAULT 0,  -- prefill attributed to series_1s (and decode, for direct)
  raw                   TEXT                  -- the original gateway record (no content)
);
CREATE INDEX requests_ts ON requests (ts_start);
CREATE INDEX requests_client ON requests (client_key, ts_start);
CREATE INDEX requests_join ON requests (container, completion, ts_end);
CREATE INDEX requests_pending ON requests (series_done, ts_end);
CREATE INDEX requests_engine_line ON requests (engine_line_id);

CREATE TABLE price_cards (
  id             INTEGER PRIMARY KEY,
  source         TEXT NOT NULL,               -- reference | openrouter | ...
  model          TEXT NOT NULL,               -- canonical model id, or '*' for the reference card
  provider_model TEXT,
  input_per_m    REAL NOT NULL,
  cached_per_m   REAL NOT NULL,
  output_per_m   REAL NOT NULL,
  effective_from REAL NOT NULL,
  fetched_at     REAL NOT NULL,
  raw            TEXT
);
CREATE INDEX price_cards_lookup ON price_cards (source, model, effective_from);

CREATE TABLE request_costs (
  request_id TEXT NOT NULL REFERENCES requests (id) ON DELETE CASCADE,
  source     TEXT NOT NULL,
  card_id    INTEGER REFERENCES price_cards (id),
  usd        REAL,
  flags      TEXT,                            -- e.g. 'card_after_request'
  PRIMARY KEY (request_id, source)
);

CREATE TABLE series_1s (
  t          INTEGER NOT NULL,
  host       TEXT NOT NULL,
  profile    TEXT NOT NULL,                   -- profile name, or 'container:<name>' for direct traffic
  model      TEXT NOT NULL,
  client_key TEXT NOT NULL,
  decode     INTEGER NOT NULL DEFAULT 0,
  prefill    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (t, host, profile, model, client_key)
);

CREATE TABLE hw_samples (
  ts   REAL NOT NULL,
  host TEXT NOT NULL,
  data TEXT NOT NULL,                         -- JSON, so new sensors need no migration
  PRIMARY KEY (ts, host)
);

CREATE TABLE engine_events (
  id      INTEGER PRIMARY KEY,
  ts      REAL NOT NULL,
  host    TEXT NOT NULL,
  profile TEXT,
  kind    TEXT NOT NULL,
  detail  TEXT
);
CREATE INDEX engine_events_ts ON engine_events (ts);

CREATE TABLE clients (
  key     TEXT PRIMARY KEY,
  name    TEXT,                               -- last known device name (Tailscale)
  node    TEXT,
  alias   TEXT,                               -- user-chosen display name, wins over name
  color   TEXT,
  updated REAL
);

CREATE TABLE ingest_offsets (
  source   TEXT PRIMARY KEY,
  position TEXT NOT NULL
);
