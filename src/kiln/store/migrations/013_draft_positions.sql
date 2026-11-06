-- Crucible's per-position speculative decoding counters (engine design note, final names). The three draft
-- lists hold one count per position 1..7; the histogram holds one round count per draft length 0..7. They
-- are compact JSON text in one column each: a request needs one row, where a table per counter would need a
-- row per position for every request, and the API sums the values per position when it needs them.
-- A request without speculation leaves them NULL, as do the rows stored before this migration, so an older
-- server keeps working unchanged.
ALTER TABLE requests ADD COLUMN draft_pos_proposed TEXT;
ALTER TABLE requests ADD COLUMN draft_pos_tested TEXT;
ALTER TABLE requests ADD COLUMN draft_pos_accepted TEXT;
ALTER TABLE requests ADD COLUMN draft_len_hist TEXT;
ALTER TABLE requests ADD COLUMN draft_passes INTEGER;
ALTER TABLE requests ADD COLUMN draft_ms REAL;
