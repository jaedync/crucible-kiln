-- Cache diagnostics (content-free). Prefix fields come from the gateway's memory-only comparison of a
-- client's consecutive requests: counts, booleans and a random conversation id, never content or hashes.
ALTER TABLE requests ADD COLUMN conv TEXT;
ALTER TABLE requests ADD COLUMN n_messages INTEGER;
ALTER TABLE requests ADD COLUMN prefix_msgs_kept INTEGER;
ALTER TABLE requests ADD COLUMN prev_n_messages INTEGER;
ALTER TABLE requests ADD COLUMN system_changed INTEGER;
ALTER TABLE requests ADD COLUMN tools_changed INTEGER;
ALTER TABLE requests ADD COLUMN new_conversation INTEGER;
ALTER TABLE requests ADD COLUMN prev_prompt INTEGER;
ALTER TABLE requests ADD COLUMN prev_completion INTEGER;
-- Derived at ingest: tokens the client added since its previous turn, and tokens the engine read again
-- although the client kept them (NULL when the client changed its prefix or the previous turn is unknown).
ALTER TABLE requests ADD COLUMN appended_tokens INTEGER;
ALTER TABLE requests ADD COLUMN reread_tokens INTEGER;
-- The engine's own cache report for the turn (Crucible crucible_turn, Halogen "closest earlier").
ALTER TABLE requests ADD COLUMN cache_session_label TEXT;
ALTER TABLE requests ADD COLUMN cache_saved_tokens INTEGER;
ALTER TABLE requests ADD COLUMN cache_tail_tokens INTEGER;
ALTER TABLE requests ADD COLUMN cache_save_refused TEXT;
ALTER TABLE requests ADD COLUMN cache_entries INTEGER;
ALTER TABLE requests ADD COLUMN cache_bytes INTEGER;
ALTER TABLE requests ADD COLUMN cache_evictions INTEGER;
ALTER TABLE requests ADD COLUMN host_free_mib INTEGER;
ALTER TABLE requests ADD COLUMN slot_context INTEGER;
ALTER TABLE requests ADD COLUMN replay_hit INTEGER;
ALTER TABLE requests ADD COLUMN closest_shared_tokens INTEGER;
ALTER TABLE requests ADD COLUMN closest_age_s REAL;
ALTER TABLE requests ADD COLUMN closest_outcome TEXT;
CREATE INDEX requests_conv ON requests (conv, ts_start);
