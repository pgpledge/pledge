\set a random(1, :accounts)
\set b random(1, :accounts - 1)
\set b CASE WHEN :b >= :a THEN :b + 1 ELSE :b END
\set r random(1, 999999999999999)
SELECT id FROM pgledger_create_transfer((SELECT id FROM bench_accounts WHERE idx = :a), (SELECT id FROM bench_accounts WHERE idx = :b), 1.00, idempotency_key => 'bench-' || :client_id || '-' || :r);
