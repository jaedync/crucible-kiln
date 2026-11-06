-- Clients are devices: requests that sent X-Kiln-Client were keyed by that label, which
-- split one device into many rows. Rekey them by peer address and keep the label as metadata.
-- series_1s has no peer column, so each label moves to the device that sent most of its requests.
CREATE TEMP TABLE relabel AS
  SELECT old, new, MAX(n) AS n FROM (
    SELECT client_key AS old, peer_ip AS new, COUNT(*) AS n FROM requests
     WHERE client_label IS NOT NULL AND client_key = client_label AND peer_ip IS NOT NULL
     GROUP BY client_key, peer_ip)
   GROUP BY old;

INSERT INTO series_1s (t, host, profile, model, client_key, decode, prefill)
  SELECT s.t, s.host, s.profile, s.model, m.new, s.decode, s.prefill
    FROM series_1s s JOIN relabel m ON s.client_key = m.old WHERE true
  ON CONFLICT (t, host, profile, model, client_key)
  DO UPDATE SET decode = decode + excluded.decode, prefill = prefill + excluded.prefill;
DELETE FROM series_1s WHERE client_key IN (SELECT old FROM relabel);

UPDATE requests SET client_key = peer_ip
 WHERE client_label IS NOT NULL AND client_key = client_label AND peer_ip IS NOT NULL;

-- A device seen only through labels needs its own client row. Its colour is assigned at startup.
INSERT OR IGNORE INTO clients (key, name, node, updated)
  SELECT m.new, c.name, c.node, c.updated FROM relabel m JOIN clients c ON c.key = m.old;
DELETE FROM clients WHERE key IN (SELECT old FROM relabel)
   AND key NOT IN (SELECT client_key FROM requests);
-- Loopback traffic comes from the Kiln host itself (evals, local checks).
UPDATE clients SET name = (SELECT host FROM requests WHERE client_key = clients.key LIMIT 1)
 WHERE key IN ('127.0.0.1', '::1') AND name IS NULL;

DROP TABLE relabel;
