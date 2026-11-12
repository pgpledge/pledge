-- :batch transfers between random distinct accounts in one call (run.sh passes -D batch=N)
SELECT count(*) FROM pledge.create_transfers(ARRAY(
    SELECT pledge.request(fa.id, fb.id, 1.00)
    FROM (SELECT g, floor(random() * :accounts)::int AS a, floor(random() * (:accounts - 1))::int AS d
          FROM generate_series(1, :batch) g) r
    JOIN bench_accounts fa ON fa.idx = r.a + 1
    JOIN bench_accounts fb ON fb.idx = (r.a + 1 + r.d) % :accounts + 1
    ORDER BY r.g));
