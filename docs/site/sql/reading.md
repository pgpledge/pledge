---
title: Reading the ledger
description: Balances, account history and transfers, read from three views. Plus a health check.
---

You read the ledger through three views:

| View                    | One row per            |
| ----------------------- | ---------------------- |
| `pledge.accounts_view`  | account                |
| `pledge.transfers_view` | transfer               |
| `pledge.entries_view`   | side of a transfer: one row for the account money left, one for the account it went to |

## Balances

```sql
SELECT name, balance, version FROM pledge.accounts_view ORDER BY name;
```

```text
 name  | balance | version
-------+---------+---------
 alice |   80.00 |       4
 bank  | -110.00 |       3
 bob   |      30 |       1
```

`version` counts the transfers that touched the account.

## Account history

Each transfer writes two entries: minus the amount on one account, plus the amount on the other.
Every entry also stores the balance before and after. That's a bank statement:

```sql
SELECT account_version, amount, account_previous_balance, account_current_balance
FROM pledge.entries_view
WHERE account_id = 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y'
ORDER BY account_version;
```

```text
 account_version | amount | account_previous_balance | account_current_balance
-----------------+--------+--------------------------+-------------------------
               1 | 100.00 |                     0.00 |                  100.00
               2 |  12.50 |                   100.00 |                  112.50
               3 |    -30 |                   112.50 |                   82.50
               4 |  -2.50 |                    82.50 |                   80.00
```

Entries also carry the transfer's `transfer_id`, `created_at`, `event_at` and `metadata`.

### Balance on a date

Take the last entry on or before that date:

```sql
SELECT coalesce((
    SELECT account_current_balance FROM pledge.entries_view
    WHERE account_id = 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y' AND event_at <= '2025-06-30'
    ORDER BY account_version DESC LIMIT 1), 0) AS balance;
```

## Transfers

Find transfers by your own metadata:

```sql
SELECT id, amount, event_at FROM pledge.transfers_view
WHERE metadata->>'order' = '7';
```

All transfers in and out of one account, in order:

```sql
SELECT t.*
FROM pledge.entries_view e
JOIN pledge.transfers_view t ON t.id = e.transfer_id
WHERE e.account_id = 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y'
ORDER BY e.account_version;
```

:::tip[Go through entries]
To find an account's transfers, start from `entries_view` as above. It's indexed by account, so
it stays fast.
:::

## Check the books

`pledge.verify()` recomputes everything from scratch and tells you if anything is off:

```sql
SELECT * FROM pledge.verify();
```

```text
             check_name              | ok |           detail
-------------------------------------+----+-----------------------------
 transfers have two zero-sum entries | t  | 0 bad transfers
 every transfer has entries          | t  | 0 transfers without entries
 every entry has a transfer          | t  | 0 orphan entries
 balance and version match entries   | t  | 0 bad accounts
 running balances are contiguous     | t  | 0 bad entries
```

It reads the whole ledger, so run it in tests or a nightly job, not on every request.
