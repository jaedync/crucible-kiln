-- Utility calls (count_tokens, tokenize, ...) generate nothing; keep them out of usage figures.
UPDATE requests SET source = 'utility'
 WHERE source = 'gateway'
   AND COALESCE(TRIM(path, '/'), '') NOT IN ('chat/completions', 'completions', 'messages', 'responses');
