//! pledge._create_transfers: the write path behind pledge.create_transfer(s).
//!
//! Two statements per call, whatever the batch size (pgledger runs ~7 per transfer plus one
//! lock per account):
//!
//!   SELECT ... FOR UPDATE             every involved account, sorted by id
//!   [SELECT existing transfers]       only when the caller supplied transfer ids; a separate
//!                                     statement so its snapshot is taken after the locks
//!   (legs are applied in Rust, in request order)
//!   one write statement               UPDATE accounts + INSERT transfers + INSERT entries,
//!                                     all multi-row from unnest(...)
//!
//! Idempotency is by transfer id: a supplied id that already exists with the same values is
//! returned as-is and moves no money; with different values it is an error.
//! Retries of the same request lock the same accounts, so they serialize on those row locks and
//! the retry's lookup sees the first call's committed transfer.
//!
//! All statements are prepared once per backend and reused.

use std::cell::OnceCell;
use std::collections::{HashMap, HashSet};

use pgrx::datum::DatumWithOid;
use pgrx::prelude::*;
use pgrx::spi::{OwnedPreparedStatement, SpiClient, SpiHeapTupleData};
use pgrx::{AnyNumeric, IntoDatum, PgOid, Uuid};

use crate::id;

const LOCK_ACCOUNTS: &str = "
    SELECT id::uuid, name, currency, balance, version,
           allow_negative_balance, allow_positive_balance, closed_at IS NOT NULL
    FROM pledge.accounts
    WHERE id = ANY ($1::pledge.account_id[])
    ORDER BY id
    FOR UPDATE";

// Compares in SQL so metadata is compared as jsonb ({"a": 1.0} = {"a": 1.00}), per transfer.
const EXISTING: &str = "
    SELECT t.id::uuid, t.from_account_id::uuid, t.to_account_id::uuid, t.amount, t.created_at,
           t.event_at, t.metadata::text,
           t.metadata IS NOT DISTINCT FROM u.md::jsonb AND t.event_at = coalesce(u.ev, t.created_at)
    FROM pledge.transfers t
    JOIN unnest($1::uuid[], $2::text[], $3::timestamptz[]) AS u (id, md, ev)
        ON t.id = u.id::pledge.transfer_id
    -- drives a primary-key index scan; without it the cached generic plan can pick a hash join
    -- over a seq scan of transfers (measured: idempotent path 4x slower)
    WHERE t.id = ANY ($1::pledge.transfer_id[])";

// All writes of a call in one statement (data-modifying CTEs run whether referenced or not), so
// the executor starts once instead of three times.
const WRITE: &str = "
    WITH a AS (
        UPDATE pledge.accounts a
        SET balance = u.balance, version = u.version, updated_at = now()
        FROM unnest($1::uuid[], $2::numeric[], $3::int8[]) AS u (id, balance, version)
        WHERE a.id = u.id::pledge.account_id
    ), t AS (
        INSERT INTO pledge.transfers
            (id, from_account_id, to_account_id, amount, created_at, event_at, metadata)
        SELECT u.id::pledge.transfer_id, u.f::pledge.account_id, u.t::pledge.account_id,
               u.amount, now(), coalesce(u.ev, now()), u.md::jsonb
        FROM unnest($4::uuid[], $5::uuid[], $6::uuid[], $7::numeric[], $8::timestamptz[],
                    $9::text[]) AS u (id, f, t, amount, ev, md)
    )
    INSERT INTO pledge.entries
        (id, account_id, transfer_id, amount, account_current_balance, account_version)
    SELECT u.id::pledge.entry_id, u.a::pledge.account_id, u.t::pledge.transfer_id,
           u.amount, u.cur, u.v
    FROM unnest($10::uuid[], $11::uuid[], $12::uuid[], $13::numeric[], $14::numeric[],
                $15::int8[]) AS u (id, a, t, amount, cur, v)";

struct Plans {
    lock_accounts: OwnedPreparedStatement,
    existing: OwnedPreparedStatement,
    write: OwnedPreparedStatement,
}

thread_local! {
    // Leaked on purpose: saved SPI plans live for the backend, and freeing them from a TLS
    // destructor at process exit would run after Postgres has torn SPI down.
    static PLANS: OnceCell<&'static Plans> = const { OnceCell::new() };
}

fn plans(client: &SpiClient<'_>) -> &'static Plans {
    use pg_sys::{INT8ARRAYOID, NUMERICARRAYOID, TEXTARRAYOID, TIMESTAMPTZARRAYOID, UUIDARRAYOID};
    let prep = |sql: &str, args: &[pg_sys::Oid]| -> OwnedPreparedStatement {
        let oids: Vec<PgOid> = args.iter().map(|&o| PgOid::from(o)).collect();
        match client.prepare_mut(sql, &oids) {
            Ok(stmt) => stmt.keep(),
            Err(e) => error!("pledge: could not prepare statement: {e}"),
        }
    };
    PLANS.with(|cell| {
        *cell.get_or_init(|| {
            Box::leak(Box::new(Plans {
                lock_accounts: prep(LOCK_ACCOUNTS, &[UUIDARRAYOID]),
                existing: prep(EXISTING, &[UUIDARRAYOID, TEXTARRAYOID, TIMESTAMPTZARRAYOID]),
                write: prep(
                    WRITE,
                    &[
                        UUIDARRAYOID,
                        NUMERICARRAYOID,
                        INT8ARRAYOID,
                        UUIDARRAYOID,
                        UUIDARRAYOID,
                        UUIDARRAYOID,
                        NUMERICARRAYOID,
                        TIMESTAMPTZARRAYOID,
                        TEXTARRAYOID,
                        UUIDARRAYOID,
                        UUIDARRAYOID,
                        UUIDARRAYOID,
                        NUMERICARRAYOID,
                        NUMERICARRAYOID,
                        INT8ARRAYOID,
                    ],
                ),
            }))
        })
    })
}

// Ledger rule violations raise SQLSTATE P0001 (raise_exception) with a JSON object in DETAIL:
// a stable "code" plus the ids involved, e.g. {"code": "pledge.account_closed", "account_id": ...}.
// pgrx can't raise custom SQLSTATEs or set other error fields. Clients read DETAIL, not the
// message. The PL/pgSQL parts (sql/schema.sql) use the same shape.
const INVALID_REQUEST: &str = "pledge.invalid_request";
const ACCOUNT_NOT_FOUND: &str = "pledge.account_not_found";
const ACCOUNT_CLOSED: &str = "pledge.account_closed";
const NEGATIVE_BALANCE_NOT_ALLOWED: &str = "pledge.negative_balance_not_allowed";
const POSITIVE_BALANCE_NOT_ALLOWED: &str = "pledge.positive_balance_not_allowed";
const CURRENCY_MISMATCH: &str = "pledge.currency_mismatch";
const TRANSFER_CONFLICT: &str = "pledge.transfer_conflict";
const DUPLICATE_TRANSFER_ID: &str = "pledge.duplicate_transfer_id";

#[cold]
#[inline(never)]
fn fail(code: &'static str, msg: String, fields: &[(&str, &str)]) -> ! {
    let mut detail = format!("{{\"code\": {}", json_string(code));
    for (key, value) in fields {
        detail.push_str(&format!(", {}: {}", json_string(key), json_string(value)));
    }
    detail.push('}');
    ereport!(ERROR, PgSqlErrorCode::ERRCODE_RAISE_EXCEPTION, msg, detail);
}

fn json_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for c in value.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn spi<T>(r: pgrx::spi::SpiResult<T>) -> T {
    r.unwrap_or_else(|e| error!("pledge: {e}"))
}

fn col<T: FromDatum + IntoDatum>(row: &SpiHeapTupleData<'_>, i: usize) -> T {
    match row.get::<T>(i) {
        Ok(Some(v)) => v,
        Ok(None) => error!("pledge: unexpected NULL in column {i}"),
        Err(e) => error!("pledge: {e}"),
    }
}

/// Render like PL/pgSQL's `%` does: NULL becomes `<NULL>`.
fn show<T>(v: Option<T>, f: impl Fn(T) -> String) -> String {
    v.map_or_else(|| "<NULL>".into(), f)
}

fn account_text(u: Uuid) -> String {
    id::encode(id::ACCOUNT, u.as_bytes())
}

fn transfer_text(u: Uuid) -> String {
    id::encode(id::TRANSFER, u.as_bytes())
}

fn arg<'a, T: IntoDatum>(v: T) -> DatumWithOid<'a> {
    DatumWithOid::from(v)
}

struct Account {
    name: String,
    currency: String,
    balance: AnyNumeric,
    version: i64,
    allow_negative: bool,
    allow_positive: bool,
    closed: bool,
    touched: bool,
}

impl Account {
    fn ensure_open(&self, id: Uuid) {
        if self.closed {
            let account_id = account_text(id);
            fail(
                ACCOUNT_CLOSED,
                format!("Account (id={}, name={}) is closed", account_id, self.name),
                &[("account_id", &account_id)],
            );
        }
    }

    /// Apply one leg and check the balance flags (per leg, invariant 6). Callers check
    /// existence, `ensure_open` and currency first.
    fn apply(&mut self, id: Uuid, delta: AnyNumeric, zero: &AnyNumeric) {
        self.balance += delta;
        self.version += 1;
        self.touched = true;
        if !self.allow_negative && self.balance < *zero {
            let account_id = account_text(id);
            fail(
                NEGATIVE_BALANCE_NOT_ALLOWED,
                format!(
                    "Account (id={}, name={}) does not allow negative balance",
                    account_id, self.name
                ),
                &[("account_id", &account_id)],
            );
        }
        if !self.allow_positive && self.balance > *zero {
            let account_id = account_text(id);
            fail(
                POSITIVE_BALANCE_NOT_ALLOWED,
                format!(
                    "Account (id={}, name={}) does not allow positive balance",
                    account_id, self.name
                ),
                &[("account_id", &account_id)],
            );
        }
    }
}

type TransferRow = (
    Uuid,
    Uuid,
    Uuid,
    AnyNumeric,
    TimestampWithTimeZone,
    TimestampWithTimeZone,
    Option<String>,
);

#[allow(clippy::type_complexity)]
#[pg_extern(volatile, parallel_unsafe)]
fn _create_transfers(
    requests_null: bool,
    ids: Vec<Option<Uuid>>,
    from_ids: Vec<Option<Uuid>>,
    to_ids: Vec<Option<Uuid>>,
    amounts: Vec<Option<AnyNumeric>>,
    event_ats: Vec<Option<TimestampWithTimeZone>>,
    metadatas: Vec<Option<String>>,
) -> TableIterator<
    'static,
    (
        name!(id, Uuid),
        name!(from_account_id, Uuid),
        name!(to_account_id, Uuid),
        name!(amount, AnyNumeric),
        name!(created_at, TimestampWithTimeZone),
        name!(event_at, TimestampWithTimeZone),
        name!(metadata, Option<String>),
    ),
> {
    if requests_null {
        fail(
            INVALID_REQUEST,
            "Transfer requests must not be null".into(),
            &[],
        );
    }
    // The SQL wrappers always pass equal lengths, but this function is callable directly.
    let n = amounts.len();
    if [
        ids.len(),
        from_ids.len(),
        to_ids.len(),
        event_ats.len(),
        metadatas.len(),
    ] != [n; 5]
    {
        error!("pledge._create_transfers: all request arrays must have equal length");
    }
    let rows = Spi::connect_mut(|client| {
        transfer(
            client,
            plans(client),
            &ids,
            &from_ids,
            &to_ids,
            amounts,
            &event_ats,
            &metadatas,
        )
    });
    TableIterator::new(rows)
}

/// A stored transfer and whether its event_at and metadata match the request's.
struct Existing {
    row: TransferRow,
    details_match: bool,
}

/// Transfers that already exist for the supplied ids (the idempotent-retry path).
fn existing(
    client: &mut SpiClient<'_>,
    plans: &Plans,
    ids: &[Option<Uuid>],
    event_ats: &[Option<TimestampWithTimeZone>],
    metadatas: &[Option<String>],
) -> HashMap<Uuid, Existing> {
    let (mut supplied, mut md, mut ev) = (Vec::new(), Vec::new(), Vec::new());
    for ((id, e), m) in ids.iter().zip(event_ats).zip(metadatas) {
        if let Some(id) = id {
            supplied.push(*id);
            ev.push(*e);
            md.push(m.as_deref()); // borrowed: binds as text, no String copy
        }
    }
    let mut seen = HashSet::with_capacity(supplied.len());
    if let Some(dup) = supplied.iter().find(|id| !seen.insert(**id)) {
        let transfer_id = transfer_text(*dup);
        fail(
            DUPLICATE_TRANSFER_ID,
            format!("Transfer id ({transfer_id}) appears more than once in the request"),
            &[("transfer_id", &transfer_id)],
        );
    }
    let mut found = HashMap::new();
    if supplied.is_empty() {
        return found;
    }
    let args = [arg(supplied), arg(md), arg(ev)];
    for row in spi(client.update(&plans.existing, None, &args)) {
        let id: Uuid = col(&row, 1);
        found.insert(
            id,
            Existing {
                row: (
                    id,
                    col(&row, 2),
                    col(&row, 3),
                    col(&row, 4),
                    col(&row, 5),
                    col(&row, 6),
                    row.get::<String>(7)
                        .unwrap_or_else(|e| error!("pledge: {e}")),
                ),
                details_match: col(&row, 8),
            },
        );
    }
    found
}

#[allow(clippy::too_many_arguments)]
fn transfer(
    client: &mut SpiClient<'_>,
    plans: &Plans,
    ids: &[Option<Uuid>],
    from_ids: &[Option<Uuid>],
    to_ids: &[Option<Uuid>],
    amounts: Vec<Option<AnyNumeric>>,
    event_ats: &[Option<TimestampWithTimeZone>],
    metadatas: &[Option<String>],
) -> Vec<TransferRow> {
    // Lock every involved account in id order (invariant 4: no deadlocks between batches).
    let account_ids: Vec<Uuid> = from_ids.iter().chain(to_ids).flatten().copied().collect();
    let mut accounts: HashMap<Uuid, Account> = HashMap::with_capacity(account_ids.len());
    for row in spi(client.update(&plans.lock_accounts, None, &[arg(account_ids)])) {
        let id: Uuid = col(&row, 1);
        accounts.insert(
            id,
            Account {
                name: col(&row, 2),
                currency: col(&row, 3),
                balance: col(&row, 4),
                version: col(&row, 5),
                allow_negative: col(&row, 6),
                allow_positive: col(&row, 7),
                closed: col(&row, 8),
                touched: false,
            },
        );
    }
    let mut existing = existing(client, plans, ids, event_ats, metadatas);

    let zero = AnyNumeric::from(0);
    let infinity: AnyNumeric = "Infinity".parse().unwrap_or_else(|e| error!("pledge: {e}"));
    let now = now();

    let n = amounts.len();
    let mut rows = Vec::with_capacity(n);
    let mut t_ids = Vec::with_capacity(n);
    let mut t_from = Vec::with_capacity(n);
    let mut t_to = Vec::with_capacity(n);
    let mut t_amount = Vec::with_capacity(n);
    let mut t_event_at = Vec::with_capacity(n);
    let mut t_metadata = Vec::with_capacity(n);
    let mut e_id = Vec::with_capacity(2 * n);
    let mut e_account = Vec::with_capacity(2 * n);
    let mut e_transfer = Vec::with_capacity(2 * n);
    let mut e_amount = Vec::with_capacity(2 * n);
    let mut e_cur = Vec::with_capacity(2 * n);
    let mut e_version = Vec::with_capacity(2 * n);

    // AnyNumeric arithmetic consumes its operands and every clone is a heap copy, so each value
    // is cloned only where a second owner needs it.
    let requests = ids
        .iter()
        .zip(from_ids)
        .zip(to_ids)
        .zip(amounts)
        .zip(event_ats)
        .zip(metadatas);
    for (((((&id, &from), &to), amount), &event_at), metadata) in requests {
        if let Some(prev) = id.and_then(|id| existing.remove(&id)) {
            let (tid, f, t, a, ..) = &prev.row;
            if !(prev.details_match
                && from == Some(*f)
                && to == Some(*t)
                && amount.as_ref() == Some(a))
            {
                let transfer_id = transfer_text(*tid);
                fail(
                    TRANSFER_CONFLICT,
                    format!("Transfer (id={transfer_id}) already exists with different values"),
                    &[("transfer_id", &transfer_id)],
                );
            }
            rows.push(prev.row);
            continue;
        }

        let amount = match amount {
            Some(a) if a > zero && a < infinity => a,
            other => fail(
                INVALID_REQUEST,
                format!(
                    "Amount ({}) must be a positive finite number",
                    show(other.as_ref(), |a| a.to_string())
                ),
                &[],
            ),
        };
        let (Some(from), Some(to)) = (from, to) else {
            fail(
                INVALID_REQUEST,
                format!(
                    "Account ids (from={}, to={}) must not be null",
                    show(from, account_text),
                    show(to, account_text)
                ),
                &[],
            );
        };
        if from == to {
            fail(
                INVALID_REQUEST,
                format!(
                    "Cannot transfer to the same account (id={})",
                    account_text(from)
                ),
                &[],
            );
        }

        let missing = |id| -> ! {
            let account_id = account_text(id);
            fail(
                ACCOUNT_NOT_FOUND,
                format!("Account (id={account_id}) does not exist"),
                &[("account_id", &account_id)],
            )
        };
        // Keys are distinct (checked just above), so this cannot panic.
        let [f, t] = accounts.get_disjoint_mut([&from, &to]);
        // Request validation (existence, closed, currency) before any balance check, as in
        // pgledger: an unfunded transfer to a missing, closed or other-currency account reports
        // that, not the from-account's balance limit.
        let f = f.unwrap_or_else(|| missing(from));
        let t = t.unwrap_or_else(|| missing(to));
        f.ensure_open(from);
        t.ensure_open(to);
        if f.currency != t.currency {
            fail(
                CURRENCY_MISMATCH,
                format!(
                    "Cannot transfer between different currencies ({} and {})",
                    f.currency, t.currency
                ),
                &[("from_currency", &f.currency), ("to_currency", &t.currency)],
            );
        }

        let neg = -amount.clone();
        f.apply(from, neg.clone(), &zero);
        t.apply(to, amount.clone(), &zero);

        let tid = id.unwrap_or_else(|| Uuid::from_bytes(id::uuidv7()));
        rows.push((
            tid,
            from,
            to,
            amount.clone(),
            now,
            event_at.unwrap_or(now),
            metadata.clone(),
        ));
        t_ids.push(tid);
        t_event_at.push(event_at);
        t_metadata.push(metadata.as_deref());
        t_from.push(from);
        t_to.push(to);
        t_amount.push(amount.clone());

        e_id.push(Uuid::from_bytes(id::uuidv7()));
        e_account.push(from);
        e_transfer.push(tid);
        e_amount.push(neg);
        e_cur.push(f.balance.clone());
        e_version.push(f.version);

        e_id.push(Uuid::from_bytes(id::uuidv7()));
        e_account.push(to);
        e_transfer.push(tid);
        e_amount.push(amount);
        e_cur.push(t.balance.clone());
        e_version.push(t.version);
    }

    if t_ids.is_empty() {
        return rows; // empty request, or every transfer was an idempotent retry
    }

    let mut a_id = Vec::with_capacity(accounts.len());
    let mut a_balance = Vec::with_capacity(accounts.len());
    let mut a_version = Vec::with_capacity(accounts.len());
    for (id, acc) in accounts.into_iter().filter(|(_, a)| a.touched) {
        a_id.push(id);
        a_balance.push(acc.balance);
        a_version.push(acc.version);
    }
    let args = [
        arg(a_id),
        arg(a_balance),
        arg(a_version),
        arg(t_ids),
        arg(t_from),
        arg(t_to),
        arg(t_amount),
        arg(t_event_at),
        arg(t_metadata),
        arg(e_id),
        arg(e_account),
        arg(e_transfer),
        arg(e_amount),
        arg(e_cur),
        arg(e_version),
    ];
    spi(client.update(&plans.write, None, &args));
    rows
}
