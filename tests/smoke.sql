-- End-to-end smoke test against the docker image: docker compose run --rm check
-- Fresh database each run; fails (ON_ERROR_STOP) on the first problem.
\set QUIET on
\pset footer off
SELECT 'smoke_' || pg_backend_pid() AS db \gset
CREATE DATABASE :"db";
\c :"db"
\set QUIET off

SELECT version();
CREATE EXTENSION pledge;
SELECT extversion FROM pg_extension WHERE extname = 'pledge';

SELECT id AS alice FROM pledge.create_account('alice', 'USD', allow_negative_balance => false) \gset
SELECT id AS bank  FROM pledge.create_account('bank', 'USD') \gset

SELECT * FROM pledge.create_transfer(:'bank', :'alice', 100.00, metadata => '{"memo": "deposit"}');
SELECT * FROM pledge.create_transfers(ARRAY[
    pledge.request(:'alice', :'bank', 30),
    pledge.request(:'alice', :'bank', 20)]);

-- overdraft must fail with pgledger's message
DO $$
BEGIN
    PERFORM pledge.create_transfer(
        (SELECT id FROM pledge.accounts WHERE name = 'alice'),
        (SELECT id FROM pledge.accounts WHERE name = 'bank'), 1000);
    RAISE EXCEPTION 'overdraft was accepted';
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE '%does not allow negative balance' THEN RAISE; END IF;
    RAISE NOTICE 'overdraft rejected: %', SQLERRM;
END $$;

-- a retry with the same transfer id returns the stored transfer, no new money moves
SELECT pledge.new_transfer_id() AS t1 \gset
SELECT * FROM pledge.create_transfer(:'bank', :'alice', 5, id => :'t1');
SELECT id = :'t1'::pledge.transfer_id AS replayed
FROM pledge.create_transfer(:'bank', :'alice', 5.0, id => :'t1') \gset
\if :replayed
\else
    \echo 'idempotent replay returned a different transfer'
    SELECT 1/0;
\endif

-- accounts are never deleted; closing one stops new transfers
SELECT id AS old FROM pledge.create_account('old', 'USD') \gset
SELECT name, closed_at IS NOT NULL AS closed FROM pledge.close_account(:'old');
DO $$
BEGIN
    DELETE FROM pledge.accounts WHERE name = 'old';
    RAISE EXCEPTION 'delete was accepted';
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE '%rows cannot be removed%' THEN RAISE; END IF;
    RAISE NOTICE 'delete rejected: %', SQLERRM;
END $$;

SELECT name, balance, version FROM pledge.accounts_view ORDER BY name;
SELECT account_version, amount, account_previous_balance, account_current_balance
FROM pledge.entries_view WHERE account_id = :'alice'::pledge.account_id ORDER BY account_version;

SELECT pg_column_size(id) AS id_bytes, pledge.id_timestamp(id) <= now() AS ts_ok
FROM pledge.transfers LIMIT 1;

SELECT * FROM pledge.verify();
SELECT bool_and(ok) AS verified FROM pledge.verify() \gset
\c postgres
DROP DATABASE :"db";
\if :verified
    \echo 'SMOKE OK'
\else
    \echo 'SMOKE FAILED: verify() not clean'
    SELECT 1/0;
\endif
