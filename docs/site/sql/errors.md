---
title: Errors
description: Every pledge error has a stable code. Match on the code, not the message.
---

When pledge refuses something, the error looks like this:

```text
ERROR:  Account (id=pla_01M730CQFHFJQ9TRR4FW8SYK2Y, name=alice) does not allow negative balance
DETAIL:  {"code": "pledge.negative_balance_not_allowed", "account_id": "pla_01M730CQFHFJQ9TRR4FW8SYK2Y"}
```

The `DETAIL` is a JSON object. Its `code` never changes, so match on that. The other keys are
the ids and values involved, listed below. The message is for people and may change. The
SQLSTATE is always `P0001`.

Nothing is written when a call fails. A batch fails as a whole.

## The codes

| Code                                  | When                                                  | What to do                        |
| ------------------------------------- | ----------------------------------------------------- | --------------------------------- |
| `pledge.negative_balance_not_allowed` | the account would go below zero                       | Tell the user: not enough money   |
| `pledge.positive_balance_not_allowed` | the account would go above zero                       | Check the account setup           |
| `pledge.account_closed`               | a transfer touches a closed account                   | Tell the user                     |
| `pledge.account_not_empty`            | `close_account` on an account that still holds money  | Move the money out first          |
| `pledge.account_already_closed`       | `close_account` on a closed account                   | Usually safe to ignore            |
| `pledge.account_not_found`            | no account with that id                               | Bug or stale id                   |
| `pledge.currency_mismatch`            | the two accounts use different currencies             | Bug: wrong account                |
| `pledge.invalid_request`              | amount is zero, negative or missing; same account twice | Bug: check input first          |
| `pledge.transfer_conflict`            | transfer id reused with different values              | Bug: see [safe retries](retries.md) |
| `pledge.duplicate_transfer_id`        | the same transfer id twice in one batch               | Bug in the batch                  |
| `pledge.immutable`                    | someone tried to delete or change ledger rows         | Alert someone                     |

The other keys in `DETAIL`:

| Code                                      | Keys                             |
| ----------------------------------------- | -------------------------------- |
| `negative_balance_not_allowed`, `positive_balance_not_allowed`, `account_closed`, `account_already_closed`, `account_not_found` | `account_id` |
| `account_not_empty`                       | `account_id`, `balance` (a numeric string) |
| `currency_mismatch`                       | `from_currency`, `to_currency`   |
| `transfer_conflict`, `duplicate_transfer_id` | `transfer_id`                 |
| `immutable`                               | `table`                          |
| `invalid_request`                         | none                             |

Retrying gives the same error every time. Only connection problems, deadlocks and
serialization failures are worth retrying, with the same [transfer ids](retries.md).

## Catch them in your app

Parse the `DETAIL` field of the database error as JSON. Most drivers have it.

::::tabs{sync="lang"}
:::tab[Python (psycopg)]
```python
import json

import psycopg

try:
    conn.execute("SELECT * FROM pledge.create_transfer(%s, %s, %s)", [a, b, amount])
except psycopg.errors.RaiseException as e:
    detail = json.loads(e.diag.message_detail)
    if detail["code"] == "pledge.negative_balance_not_allowed":
        ...  # not enough money on detail["account_id"]
    else:
        raise
```

With the `django-pledge` package, `django_pledge.translate(e)` turns it into a typed exception such as
`django_pledge.NegativeBalanceNotAllowed`, with the keys as attributes: `e.account_id`.
:::
:::tab[Node (pg)]
```js
try {
  await client.query("SELECT * FROM pledge.create_transfer($1, $2, $3)", [a, b, amount]);
} catch (e) {
  const detail = e.code === "P0001" ? JSON.parse(e.detail) : {};
  if (detail.code === "pledge.negative_balance_not_allowed") {
    // not enough money on detail.account_id
  } else {
    throw e;
  }
}
```
:::
:::tab[PL/pgSQL]
```sql
DO $$
DECLARE detail text;
BEGIN
    PERFORM pledge.create_transfer(...);
EXCEPTION WHEN raise_exception THEN
    GET STACKED DIAGNOSTICS detail = PG_EXCEPTION_DETAIL;
    IF detail::jsonb ->> 'code' = 'pledge.negative_balance_not_allowed' THEN
        RAISE NOTICE 'not enough money on %', detail::jsonb ->> 'account_id';
    ELSE
        RAISE;
    END IF;
END $$;
```
:::
::::

:::note[Inside a transaction]
A failed call aborts your transaction, like any PostgreSQL error. To carry on after a refused
transfer, wrap the call in a `SAVEPOINT`.
:::

## Other errors you may see

- **A bad id**, like a transfer id where an account id belongs:
  `invalid input syntax for type pledge.account_id` (SQLSTATE `22P02`).
- **Two requests racing with the same new transfer id** on different accounts: a unique
  violation on `transfers_pkey` (SQLSTATE `23505`), with no JSON `DETAIL`. Only one was
  booked. Treat it like `transfer_conflict`.
