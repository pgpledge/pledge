---
title: Reference
description: Every pledge function, view, type and error code on one page.
---

Everything lives in the `pledge` schema. Use the functions and views below. The tables behind
them are internal.

## Functions

| Function                  | Does                                                    |
| ------------------------- | ------------------------------------------------------- |
| `create_account`          | Creates an account and returns it                       |
| `close_account`           | Closes an account with a zero balance and returns it    |
| `create_transfer`         | Books one transfer and returns it                       |
| `create_transfers`        | Books many transfers, all or nothing, and returns them  |
| `request`                 | Builds one request for `create_transfers`               |
| `new_transfer_id`         | Makes a new transfer id, for [safe retries](sql/retries.md) |
| `verify`                  | Checks the whole ledger                                 |
| `id_timestamp`            | When an id was made (any of the three id types)         |

```sql
pledge.create_account(
    name text,
    currency text,
    allow_negative_balance boolean DEFAULT true,
    allow_positive_balance boolean DEFAULT true,
    metadata jsonb DEFAULT NULL
) RETURNS SETOF pledge.accounts_view

pledge.close_account(id pledge.account_id) RETURNS SETOF pledge.accounts_view

pledge.create_transfer(
    from_account_id pledge.account_id,
    to_account_id pledge.account_id,
    amount numeric,
    event_at timestamptz DEFAULT NULL,
    metadata jsonb DEFAULT NULL,
    id pledge.transfer_id DEFAULT NULL
) RETURNS SETOF pledge.transfers_view

pledge.create_transfers(
    requests pledge.transfer_request[],
    event_at timestamptz DEFAULT NULL,   -- for requests without their own
    metadata jsonb DEFAULT NULL          -- likewise
) RETURNS SETOF pledge.transfers_view

pledge.create_transfers(VARIADIC requests pledge.transfer_request[])
    RETURNS SETOF pledge.transfers_view

pledge.request(
    from_account_id pledge.account_id,
    to_account_id pledge.account_id,
    amount numeric,
    id pledge.transfer_id DEFAULT NULL,
    event_at timestamptz DEFAULT NULL,
    metadata jsonb DEFAULT NULL
) RETURNS pledge.transfer_request

pledge.new_transfer_id() RETURNS pledge.transfer_id

pledge.verify() RETURNS TABLE (check_name text, ok boolean, detail text)

pledge.id_timestamp(pledge.account_id | pledge.transfer_id | pledge.entry_id) RETURNS timestamptz
```

## Views

**`pledge.accounts_view`**: `id`, `name`, `currency`, `balance`, `version`,
`allow_negative_balance`, `allow_positive_balance`, `metadata`, `created_at`, `updated_at`,
`closed_at`.

**`pledge.transfers_view`**: `id`, `from_account_id`, `to_account_id`, `amount`, `created_at`,
`event_at`, `metadata`.

**`pledge.entries_view`**: `id`, `account_id`, `transfer_id`, `amount` (negative on the sending
side), `account_previous_balance`, `account_current_balance`, `account_version`, plus
`created_at`, `event_at` and `metadata` from the transfer.

## Types

| Type                 | Text form  | Example                          |
| -------------------- | ---------- | -------------------------------- |
| `pledge.account_id`  | `pla_…`    | `pla_01M72Y7TF2EWV94RPD57WD8XJJ` |
| `pledge.transfer_id` | `plt_…`    | `plt_01M72Y7TF5FRZAAHZ6ZFHEXJVP` |
| `pledge.entry_id`    | `ple_…`    | `ple_01M72Y7TF5FRZAAHZ6ZFHEXJVQ` |

Ids are UUIDv7 inside, so they sort by creation time. Cast to and from `uuid` with `::uuid` and
`::pledge.account_id`. Ids of different kinds can't be compared by mistake: `account_id =
transfer_id` is an error.

`pledge.transfer_request` is the row type that `pledge.request(...)` builds.

## Errors

All errors are SQLSTATE `P0001`. `DETAIL` is a JSON object with the `code` and the keys listed
under Attributes. In Python these are attributes of the exception, with `account_id` as an
`AccountId`, `transfer_id` as a `TransferId` and `balance` as a `Decimal`. See
[Errors](sql/errors.md) for how to handle them.

| Code                                  | Python class                | Attributes                     | Message                                                               |
| ------------------------------------- | --------------------------- | ------------------------------ | --------------------------------------------------------------------- |
| `pledge.invalid_request`              | `InvalidRequest`            |                                | `Amount (<amount>) must be a positive finite number`, `Cannot transfer to the same account (id=<id>)`, `Account ids (from=<id>, to=<id>) must not be null`, `Transfer requests must not be null` |
| `pledge.account_not_found`            | `AccountNotFound`           | `account_id`                   | `Account (id=<id>) does not exist`                                    |
| `pledge.account_closed`               | `AccountClosed`             | `account_id`                   | `Account (id=<id>, name=<name>) is closed`                            |
| `pledge.account_already_closed`       | `AccountAlreadyClosed`      | `account_id`                   | `Account (id=<id>) is already closed`                                 |
| `pledge.account_not_empty`            | `AccountNotEmpty`           | `account_id`, `balance`        | `Account (id=<id>, name=<name>) has a non-zero balance (<balance>)`   |
| `pledge.negative_balance_not_allowed` | `NegativeBalanceNotAllowed` | `account_id`                   | `Account (id=<id>, name=<name>) does not allow negative balance`      |
| `pledge.positive_balance_not_allowed` | `PositiveBalanceNotAllowed` | `account_id`                   | `Account (id=<id>, name=<name>) does not allow positive balance`      |
| `pledge.currency_mismatch`            | `CurrencyMismatch`          | `from_currency`, `to_currency` | `Cannot transfer between different currencies (<from> and <to>)`     |
| `pledge.transfer_conflict`            | `TransferConflict`          | `transfer_id`                  | `Transfer (id=<id>) already exists with different values`             |
| `pledge.duplicate_transfer_id`        | `DuplicateTransferId`       | `transfer_id`                  | `Transfer id (<id>) appears more than once in the request`            |
| `pledge.immutable`                    | `Immutable`                 | `table`                        | `pledge.<table>: rows cannot be removed (…)`, `pledge.<table>: <columns> cannot be changed` |

## Python

From `pledge`:

| Name                                     | What                                                       |
| ---------------------------------------- | ---------------------------------------------------------- |
| `AccountId`, `TransferId`, `EntryId`     | Typed ids. `TransferId.new()` makes a new one              |
| `LedgerError` and the classes above      | Ledger errors, with `.code`, `.message`, `.detail` (the parsed JSON) and the attributes above |
| `translate(exc)`                         | Turns a psycopg or Django database error into a `LedgerError`, or `None` |
| `VerificationFailed`                     | Raised by `assert_verified()`                              |

From `django_pledge.models`:

| Call                                             | Runs                       |
| ------------------------------------------------ | -------------------------- |
| `Account.objects.create(...)`                    | `create_account`           |
| `account.transfer(to, amount, id=, event_at=, metadata=)` | `create_transfer` |
| `Transfer.objects.create(...)`                   | `create_transfer`          |
| `Transfer.objects.bulk_create([...], event_at=, metadata=)` | `create_transfers` |
| `account.close()`                                | `close_account`            |
| `Account.objects.verify()`, `assert_verified()`  | `verify`                   |

Reading: `account.history()`, `account.transfers()`, `account.balance_at(when)`,
`account.is_closed`, `account.incoming`, `account.outgoing`, `account.entries`. Async versions
work too: `acreate`, `abulk_create`, `asave`.

From `django_pledge`: `AccountIdField`, `TransferIdField`, `EntryIdField`, `NumericField`.
From `django_pledge.json`: `LedgerJSONEncoder`.
From `django_pledge.admin`: `ReadOnlyAdmin`.
