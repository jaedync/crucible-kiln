-- Queries that run on kiln-dash's event loop walked old rows. With the pages out of memory (the fleet's
-- GPU jobs on the inference host) each walk waited seconds on the disk, and the panel showed NO DATA.
-- settle(), every second: its gateway query walked requests_pending past every utility row (series_done
-- stays 0 for them, 14,321 rows on the inference host) and its engine_pending query scanned the table.
-- The join of each engine line: its completion IS NULL branch read every row of the engine without a
-- completion count (2,789 on the inference host), with no time bound.
-- These partial indexes hold only the rows each query wants: none or a few. requests_pending served only
-- settle's gateway query, and without statistics the planner kept choosing it, so it goes.
CREATE INDEX requests_gateway_unsettled ON requests (ts_end) WHERE source = 'gateway' AND series_done = 0;
CREATE INDEX requests_engine_pending ON requests (ts_end) WHERE source = 'engine_pending';
CREATE INDEX requests_gateway_unjoined ON requests (container, ts_end) WHERE source = 'gateway' AND engine_joined_at IS NULL;
DROP INDEX requests_pending;
