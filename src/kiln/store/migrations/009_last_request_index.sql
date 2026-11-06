-- A host's newest request (last_request) by its end time. Without this index SQLite sorted every row of
-- the host to find one: about 7,000 pages on the inference host, read on the event loop after each request.
CREATE INDEX requests_host_end ON requests (host, ts_end);
