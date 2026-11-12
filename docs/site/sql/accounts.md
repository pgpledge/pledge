---
title: Accounts
description: Create accounts, stop overdrafts with balance flags, and close accounts you no longer need.
---

## Create an account

```sql
SELECT id, name, currency, balance
FROM pledge.create_account('bank', 'USD');
```

```text
               id               | name | currency | balance
--------------------------------+------+----------+---------
 pla_01M730CQFEF2CR8VRK2DCTVZ7P | bank | USD      |       0
```

pledge picks the id. Account ids start with `pla_`. Store the id in your own tables to link
your data to the account.

You can attach your own data as JSON:

```sql
SELECT id FROM pledge.create_account('alice', 'USD', metadata => '{"user_id": 42}');
```

The currency is any text you like (`'USD'`, `'EUR'`, `'points'`). Money only moves between
accounts with the same currency.

## Stop overdrafts

Every account has two flags. Both are `true` unless you say otherwise.

| Flag                     | Set to `false` and…                  |
| ------------------------ | ------------------------------------ |
| `allow_negative_balance` | the balance can never go below zero |
| `allow_positive_balance` | the balance can never go above zero |

A customer wallet should never go below zero:

```sql
SELECT id FROM pledge.create_account('alice', 'USD', allow_negative_balance => false);
```

A transfer that breaks a flag fails with `pledge.negative_balance_not_allowed` (or
`positive_…`), and nothing is written.

:::tip[Which accounts can go negative?]
Money has to come from somewhere. Give the outside world an account that may go negative, like
`bank` or `world`. When a customer tops up, move money from `bank` to their wallet. The bank's
balance goes negative: that's the money that came into your system.
:::

## Close an account

Accounts are never deleted. Close them instead:

```sql
SELECT name, balance, closed_at FROM pledge.close_account('pla_01M730CQFZE8ZAPGZK6T3BVSQX');
```

A closed account keeps its full history. Any new transfer to or from it fails with
`pledge.account_closed`.

Only an account with a zero balance can be closed. If it still holds money, move it out first,
in the same transaction:

```sql
BEGIN;
SELECT balance FROM pledge.accounts_view WHERE id = 'pla_01M730CQFZE8ZAPGZK6T3BVSQX';      -- 42.10
SELECT * FROM pledge.create_transfer('pla_01M730CQFZE8ZAPGZK6T3BVSQX', 'pla_01M730CQFEF2CR8VRK2DCTVZ7P', 42.10);
SELECT * FROM pledge.close_account('pla_01M730CQFZE8ZAPGZK6T3BVSQX');
COMMIT;
```

If another transfer lands in between, `close_account` fails with `pledge.account_not_empty` and
the whole transaction rolls back. Nothing is half done. Just try again.
