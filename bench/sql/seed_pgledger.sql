-- psql -v accounts=N
DROP TABLE IF EXISTS bench_accounts;

CREATE TABLE bench_accounts(
    idx int PRIMARY KEY,
    id text NOT NULL
);

INSERT INTO bench_accounts
SELECT
    g,
    a.id
FROM
    generate_series(1, :accounts) g,
    LATERAL pgledger_create_account('bench_' || g, 'USD') a;

