-- Tables, views and the public API. Tables are internal; views and functions are the public API.
-- Started as a port of pgledger; diverges where that makes the ledger faster or simpler.

CREATE TABLE pledge.accounts (
    id pledge.account_id PRIMARY KEY DEFAULT pledge.gen_uuidv7()::pledge.account_id,
    name text NOT NULL,
    currency text NOT NULL,
    balance numeric NOT NULL DEFAULT 0,
    version bigint NOT NULL DEFAULT 0,
    allow_negative_balance boolean NOT NULL,
    allow_positive_balance boolean NOT NULL,
    metadata jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    -- closed accounts keep their history but take no new transfers (accounts are never deleted)
    closed_at timestamptz
-- every transfer rewrites two account rows; free space lets those be HOT updates (no index churn)
) WITH (fillfactor = 50);

CREATE TABLE pledge.transfers (
    id pledge.transfer_id PRIMARY KEY DEFAULT pledge.gen_uuidv7()::pledge.transfer_id,
    from_account_id pledge.account_id NOT NULL REFERENCES pledge.accounts (id),
    to_account_id pledge.account_id NOT NULL REFERENCES pledge.accounts (id),
    amount numeric NOT NULL,
    created_at timestamptz NOT NULL,
    event_at timestamptz NOT NULL,
    metadata jsonb,
    CHECK (amount > 0 AND amount < 'Infinity'::numeric AND from_account_id <> to_account_id)
);

-- No (from_account_id) / (to_account_id) indexes, unlike pgledger: an account's transfers are
-- found through entries (account_id, account_version), which also gives the running balance.
-- They cost two index inserts per transfer, and the one thing that would need them (deleting
-- an account, which checks for referencing transfers) is rejected below.
CREATE INDEX ON pledge.transfers (event_at);

CREATE TABLE pledge.entries (
    id pledge.entry_id PRIMARY KEY DEFAULT pledge.gen_uuidv7()::pledge.entry_id,
    account_id pledge.account_id NOT NULL REFERENCES pledge.accounts (id),
    -- no FK: written by the same call that inserts the transfer; verify() checks it
    transfer_id pledge.transfer_id NOT NULL,
    amount numeric NOT NULL,
    -- previous balance (= current - amount) and created_at (= the transfer's) are derived in
    -- entries_view instead of stored twice
    account_current_balance numeric NOT NULL,
    account_version bigint NOT NULL,
    -- one entry per account version (part of invariant 5); also serves lookups by account
    UNIQUE (account_id, account_version)
);

CREATE INDEX ON pledge.entries (transfer_id);

-- Ledger rows are never removed, and the columns that carry money or identity never change.
-- Enforced by the database (invariants 1-3), not only by "no code path does this":
--   * statement-level BEFORE DELETE/TRUNCATE on accounts, transfers, entries;
--   * BEFORE UPDATE OF <frozen columns>, which Postgres fires only when one of those columns is
--     in the SET list. The write path only sets accounts.balance/version/updated_at, so none
--     of these triggers ever fire on the hot path.
-- They also stand in for the FKs to transfers dropped above.
CREATE FUNCTION pledge.reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'pledge.%: % cannot be changed', TG_TABLE_NAME, TG_ARGV[0]
            USING DETAIL = json_build_object('code', 'pledge.immutable', 'table', TG_TABLE_NAME)::text;
    END IF;
    RAISE EXCEPTION 'pledge.%: rows cannot be removed (% rejected)', TG_TABLE_NAME, TG_OP
        USING DETAIL = json_build_object('code', 'pledge.immutable', 'table', TG_TABLE_NAME)::text;
END $$;

CREATE TRIGGER no_delete BEFORE DELETE OR TRUNCATE ON pledge.accounts
    FOR EACH STATEMENT EXECUTE FUNCTION pledge.reject_change();
CREATE TRIGGER no_delete BEFORE DELETE OR TRUNCATE ON pledge.transfers
    FOR EACH STATEMENT EXECUTE FUNCTION pledge.reject_change();
CREATE TRIGGER no_delete BEFORE DELETE OR TRUNCATE ON pledge.entries
    FOR EACH STATEMENT EXECUTE FUNCTION pledge.reject_change();

CREATE TRIGGER frozen BEFORE UPDATE OF id, currency, created_at ON pledge.accounts
    FOR EACH ROW EXECUTE FUNCTION pledge.reject_change('id, currency, created_at');
CREATE TRIGGER frozen BEFORE UPDATE OF id, from_account_id, to_account_id, amount, created_at
        ON pledge.transfers
    FOR EACH ROW EXECUTE FUNCTION pledge.reject_change(
        'id, from_account_id, to_account_id, amount, created_at');
CREATE TRIGGER frozen BEFORE UPDATE OF id, account_id, transfer_id, amount, account_current_balance,
        account_version ON pledge.entries
    FOR EACH ROW EXECUTE FUNCTION pledge.reject_change(
        'id, account_id, transfer_id, amount, account_current_balance, account_version');

CREATE VIEW pledge.accounts_view AS
SELECT id, name, currency, balance, version, allow_negative_balance, allow_positive_balance,
       metadata, created_at, updated_at, closed_at
FROM pledge.accounts;

CREATE VIEW pledge.transfers_view AS
SELECT id, from_account_id, to_account_id, amount, created_at, event_at, metadata
FROM pledge.transfers;

CREATE VIEW pledge.entries_view AS
SELECT e.id, e.account_id, e.transfer_id, e.amount,
       e.account_current_balance - e.amount AS account_previous_balance,
       e.account_current_balance, e.account_version, t.created_at, t.event_at, t.metadata
FROM pledge.entries e
JOIN pledge.transfers t ON e.transfer_id = t.id;

-- Optional fields: id (supply one, e.g. pledge.new_transfer_id() or a client-generated plt_ id,
-- to make retries idempotent), event_at and metadata (per transfer; when NULL the call-level
-- value of create_transfers applies).
CREATE TYPE pledge.transfer_request AS (
    from_account_id pledge.account_id,
    to_account_id pledge.account_id,
    amount numeric,
    id pledge.transfer_id,
    event_at timestamptz,
    metadata jsonb
);

-- ARRAY[pledge.request(a, b, 10), pledge.request(b, c, 5, id => ...)] reads better than row casts.
CREATE FUNCTION pledge.request(
    from_account_id pledge.account_id,
    to_account_id pledge.account_id,
    amount numeric,
    id pledge.transfer_id DEFAULT NULL,
    event_at timestamptz DEFAULT NULL,
    metadata jsonb DEFAULT NULL
) RETURNS pledge.transfer_request
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN ROW(from_account_id, to_account_id, amount, id, event_at, metadata)::pledge.transfer_request;

CREATE FUNCTION pledge.new_transfer_id() RETURNS pledge.transfer_id
LANGUAGE sql VOLATILE PARALLEL SAFE
RETURN pledge.gen_uuidv7()::pledge.transfer_id;

CREATE FUNCTION pledge.create_account(
    name text,
    currency text,
    allow_negative_balance boolean DEFAULT true,
    allow_positive_balance boolean DEFAULT true,
    metadata jsonb DEFAULT NULL
) RETURNS SETOF pledge.accounts_view
LANGUAGE sql AS $$
    INSERT INTO pledge.accounts (name, currency, allow_negative_balance, allow_positive_balance,
                                 metadata, created_at, updated_at)
    VALUES (name, currency, allow_negative_balance, allow_positive_balance, metadata, now(), now())
    RETURNING id, name, currency, balance, version, allow_negative_balance, allow_positive_balance,
              metadata, created_at, updated_at, closed_at;
$$;

-- Accounts are never deleted; closing one keeps its history and rejects new transfers. Only an
-- account with a zero balance can be closed, so no money is stranded on it.
CREATE FUNCTION pledge.close_account(id pledge.account_id) RETURNS SETOF pledge.accounts_view
LANGUAGE plpgsql AS $$
DECLARE
    acc pledge.accounts;
BEGIN
    -- the row lock makes the balance check and the close one step against concurrent transfers
    SELECT * INTO acc FROM pledge.accounts a WHERE a.id = close_account.id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Account (id=%) does not exist', id
            USING DETAIL = json_build_object('code', 'pledge.account_not_found', 'account_id', id::text)::text;
    END IF;
    IF acc.closed_at IS NOT NULL THEN
        RAISE EXCEPTION 'Account (id=%) is already closed', id
            USING DETAIL = json_build_object('code', 'pledge.account_already_closed', 'account_id', id::text)::text;
    END IF;
    IF acc.balance <> 0 THEN
        RAISE EXCEPTION 'Account (id=%, name=%) has a non-zero balance (%)', id, acc.name, acc.balance
            USING DETAIL = json_build_object(
                'code', 'pledge.account_not_empty', 'account_id', id::text, 'balance', acc.balance::text
            )::text;
    END IF;
    UPDATE pledge.accounts a SET closed_at = now(), updated_at = now() WHERE a.id = close_account.id;
    RETURN QUERY SELECT * FROM pledge.accounts_view v WHERE v.id = close_account.id;
END $$;

-- The work happens in pledge._create_transfers (Rust, src/ledger.rs). These wrappers only split
-- the composite array into plain uuid/numeric arrays and put the typed ids back on the way out.
-- The returned rows come from what Rust just wrote (or found, for retries): no read-back query.
CREATE FUNCTION pledge.create_transfers(
    requests pledge.transfer_request[],
    event_at timestamptz DEFAULT NULL,
    metadata jsonb DEFAULT NULL
) RETURNS SETOF pledge.transfers_view
LANGUAGE sql AS $$
    SELECT t.id::pledge.transfer_id, t.from_account_id::pledge.account_id,
           t.to_account_id::pledge.account_id, t.amount, t.created_at, t.event_at,
           t.metadata::jsonb
    FROM (SELECT array_agg(r.id::uuid ORDER BY o) AS id,
                 array_agg(r.from_account_id::uuid ORDER BY o) AS f,
                 array_agg(r.to_account_id::uuid ORDER BY o) AS t,
                 array_agg(r.amount ORDER BY o) AS amount,
                 array_agg(coalesce(r.event_at, create_transfers.event_at) ORDER BY o) AS ev,
                 array_agg(coalesce(r.metadata, create_transfers.metadata)::text ORDER BY o) AS md
          FROM unnest(requests) WITH ORDINALITY
               AS r (from_account_id, to_account_id, amount, id, event_at, metadata, o)) x,
         pledge._create_transfers(
             requests IS NULL, coalesce(x.id, '{}'), coalesce(x.f, '{}'), coalesce(x.t, '{}'),
             coalesce(x.amount, '{}'), coalesce(x.ev, '{}'), coalesce(x.md, '{}')) t;
$$;

CREATE FUNCTION pledge.create_transfers(VARIADIC requests pledge.transfer_request[])
RETURNS SETOF pledge.transfers_view
LANGUAGE sql AS $$
    SELECT * FROM pledge.create_transfers(requests, NULL::timestamptz, NULL::jsonb);
$$;

CREATE FUNCTION pledge.create_transfer(
    from_account_id pledge.account_id,
    to_account_id pledge.account_id,
    amount numeric,
    event_at timestamptz DEFAULT NULL,
    metadata jsonb DEFAULT NULL,
    id pledge.transfer_id DEFAULT NULL
) RETURNS SETOF pledge.transfers_view
LANGUAGE sql AS $$
    SELECT t.id::pledge.transfer_id, t.from_account_id::pledge.account_id,
           t.to_account_id::pledge.account_id, t.amount, t.created_at, t.event_at,
           t.metadata::jsonb
    FROM pledge._create_transfers(
        false, ARRAY[create_transfer.id::uuid], ARRAY[create_transfer.from_account_id::uuid],
        ARRAY[create_transfer.to_account_id::uuid], ARRAY[create_transfer.amount],
        ARRAY[create_transfer.event_at], ARRAY[create_transfer.metadata::text]
    ) t;
$$;

-- Ledger invariants, checked from the tables (CLAUDE.md, "Ledger invariants").
CREATE FUNCTION pledge.verify()
RETURNS TABLE (check_name text, ok boolean, detail text)
LANGUAGE sql STABLE
SET search_path = pledge, pg_catalog
AS $$
    SELECT 'transfers have two zero-sum entries',
           count(*) = 0,
           count(*) || ' bad transfers' || coalesce(', e.g. ' || min(transfer_id::text), '')
    FROM (SELECT transfer_id FROM entries GROUP BY transfer_id
          HAVING sum(amount) <> 0 OR count(*) <> 2) bad
    UNION ALL
    SELECT 'every transfer has entries', count(*) = 0, count(*) || ' transfers without entries'
    FROM transfers t WHERE NOT EXISTS (SELECT 1 FROM entries e WHERE e.transfer_id = t.id)
    UNION ALL
    SELECT 'every entry has a transfer', count(*) = 0, count(*) || ' orphan entries'
    FROM entries e WHERE NOT EXISTS (SELECT 1 FROM transfers t WHERE t.id = e.transfer_id)

    UNION ALL
    SELECT 'balance and version match entries',
           count(*) = 0,
           count(*) || ' bad accounts' || coalesce(', e.g. ' || min(a.id::text), '')
    FROM accounts a
    LEFT JOIN (SELECT account_id, sum(amount) s, count(*) n, max(account_version) v
               FROM entries GROUP BY account_id) e ON e.account_id = a.id
    WHERE a.balance <> coalesce(e.s, 0) OR a.version <> coalesce(e.n, 0)
       OR a.version <> coalesce(e.v, 0)
    UNION ALL
    SELECT 'running balances are contiguous',
           count(*) = 0,
           count(*) || ' bad entries' || coalesce(', e.g. ' || min(id::text), '')
    FROM (SELECT id, amount, account_current_balance, account_version,
                 lag(account_current_balance, 1, 0::numeric)
                     OVER (PARTITION BY account_id ORDER BY account_version) prev,
                 row_number() OVER (PARTITION BY account_id ORDER BY account_version) rn
          FROM entries) x
    WHERE account_current_balance <> prev + amount
       OR account_version <> rn;
$$;

-- Plan the write path's statements once per backend instead of on every call. With the default
-- plan_cache_mode the planner kept picking per-call custom plans for these parameterized
-- statements, and for the FK checks they trigger: about 30% of a transfer's server time.
ALTER FUNCTION pledge._create_transfers(bool, uuid[], uuid[], uuid[], numeric[], timestamptz[], text[])
    SET plan_cache_mode = force_generic_plan;
