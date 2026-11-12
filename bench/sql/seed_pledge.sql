-- psql -v accounts=N
DROP TABLE IF EXISTS bench_accounts;

CREATE TABLE bench_accounts(
    idx int PRIMARY KEY,
    id pledge.account_id NOT NULL
);

INSERT INTO bench_accounts
SELECT
    g,
    a.id
FROM
    generate_series(1, :accounts) g,
    LATERAL pledge.create_account('bench_' || g, 'USD') a;

