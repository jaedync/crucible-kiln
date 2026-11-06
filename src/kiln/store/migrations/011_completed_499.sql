-- Pi closes the connection a few ms after a stream's last token, before [DONE]. The gateway recorded those as
-- cancels (499 client_disconnected) although the finish reason and the usage report had been relayed: 262 of
-- 266 such rows in one day, shown as errors in the usage table and left out of "last request". They are
-- completed requests (gateway/stream.py ended_whole); a stream cut before its end keeps its 499.
UPDATE requests SET status = 200, error = NULL
WHERE status = 499 AND error = 'client_disconnected' AND finish IS NOT NULL AND finish <> '' AND completion IS NOT NULL;
