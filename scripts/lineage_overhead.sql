-- What provenance costs, as a share of the work that produced the data.
--
-- Only the digest is charged to provenance. The read is not: verifying, scoring or
-- querying the same table pays the same read, and counting it here would blame
-- lineage for the cost of storing data encrypted.
SELECT r.dataset,
       r.source                                   AS layer,
       (r.metadata ->> 'digest_ms')::numeric      AS digest_ms,
       r.records_read                             AS records,
       round((r.metadata ->> 'digest_ms')::numeric
             / NULLIF(r.records_read, 0) * 1000, 1) AS us_per_record,
       round(EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) * 1000) AS entry_ms
  FROM platform.run r
 WHERE r.run_type = 'lineage'
   AND r.status = 'succeeded'
   AND r.metadata ? 'digest_ms'
 ORDER BY r.records_read DESC;
