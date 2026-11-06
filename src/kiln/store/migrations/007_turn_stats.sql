-- Crucible turn counts (wip/turn-stats): of completion_tokens, the tokens the engine generated and the close text
-- the server wrote at once when it closed a think turn. NULL: the engine line did not say (older servers, other
-- engines). The engine decode rate is engine_generated_tokens / engine_decode_s when it is known.
ALTER TABLE requests ADD COLUMN engine_generated_tokens INTEGER;
ALTER TABLE requests ADD COLUMN engine_forced_tokens INTEGER;
