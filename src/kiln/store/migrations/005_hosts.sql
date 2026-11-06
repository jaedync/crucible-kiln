-- Remote hosts (ARCHITECTURE.md, Hosts): their agents add requests with source 'agent' and their own
-- host name. Every table already has a host column; views filter by it.
CREATE INDEX requests_host ON requests (host, ts_start);
