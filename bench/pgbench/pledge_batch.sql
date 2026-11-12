\set a random(1, :accounts)
\set b random(1, :accounts - 1)
\set b CASE WHEN :b >= :a THEN :b + 1 ELSE :b END
\set c random(1, :accounts)
\set d random(1, :accounts - 1)
\set d CASE WHEN :d >= :c THEN :d + 1 ELSE :d END
SELECT id FROM pledge.create_transfers(ARRAY[pledge.request((SELECT id FROM bench_accounts WHERE idx = :a), (SELECT id FROM bench_accounts WHERE idx = :b), 1.00), pledge.request((SELECT id FROM bench_accounts WHERE idx = :c), (SELECT id FROM bench_accounts WHERE idx = :d), 2.00)]);
