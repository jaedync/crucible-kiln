-- Direct engine traffic belongs to a device too. The engines listen on loopback, so a request that bypassed
-- the gateway came from the Kiln host: rekey direct:<container>, eval:<run> and kiln:probe to the host's
-- loopback device (the key the gateway gives its local requests) and keep the container or probe as the label.
CREATE TEMP TABLE direct_keys AS
  SELECT k AS old,
         CASE WHEN k = 'kiln:probe' THEN 'kiln-probe'
              WHEN k LIKE 'eval:%' THEN 'eval-' || substr(k, 6)
              ELSE substr(k, 8) END AS label
    FROM (SELECT client_key AS k FROM requests UNION SELECT client_key FROM series_1s UNION SELECT key FROM clients)
   WHERE k = 'kiln:probe' OR k LIKE 'direct:%' OR k LIKE 'eval:%';

INSERT INTO series_1s (t, host, profile, model, client_key, decode, prefill)
  SELECT t, host, profile, model, '127.0.0.1', decode, prefill FROM series_1s
   WHERE client_key IN (SELECT old FROM direct_keys)
  ON CONFLICT (t, host, profile, model, client_key)
  DO UPDATE SET decode = decode + excluded.decode, prefill = prefill + excluded.prefill;
DELETE FROM series_1s WHERE client_key IN (SELECT old FROM direct_keys);

-- SET sees the row as it was, so the label lookup uses the old key.
UPDATE requests SET client_label = COALESCE(client_label, (SELECT label FROM direct_keys WHERE old = requests.client_key)),
                    client_name = host, client_key = '127.0.0.1'
 WHERE client_key IN (SELECT old FROM direct_keys);

-- The loopback device is named after the Kiln host, as the gateway names it. Its colour is assigned at startup.
INSERT INTO clients (key, name, updated)
  SELECT '127.0.0.1', (SELECT host FROM requests WHERE client_key = '127.0.0.1' LIMIT 1), MAX(c.updated)
    FROM clients c JOIN direct_keys m ON c.key = m.old WHERE true HAVING COUNT(*) > 0
  ON CONFLICT (key) DO UPDATE SET name = COALESCE(name, excluded.name),
                                  updated = MAX(COALESCE(updated, 0), excluded.updated);
DELETE FROM clients WHERE key IN (SELECT old FROM direct_keys);

DROP TABLE direct_keys;
