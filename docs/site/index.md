---
title: pledge
description: A double-entry ledger inside PostgreSQL. Create accounts, move money between them, and let the database keep the books straight.
---

pledge is a double-entry ledger that runs inside PostgreSQL. You create accounts and move money
between them with plain SQL. Every transfer is booked twice, once out and once in, so the books
always add up.

::linkbutton[Get started]{href="install.md"}
::linkbutton[Use it from SQL]{href="sql/accounts.md" variant="secondary"}
::linkbutton[Use it from Django]{href="django/setup.md" variant="secondary"}

## A quick look

```sql
CREATE EXTENSION pledge;

-- Two accounts. Alice's wallet may never go below zero.
SELECT id FROM pledge.create_account('bank', 'USD');
-- pla_01M730CQFEF2CR8VRK2DCTVZ7P
SELECT id FROM pledge.create_account('alice', 'USD', allow_negative_balance => false);
-- pla_01M730CQFHFJQ9TRR4FW8SYK2Y

-- Move 100 from the bank to Alice.
SELECT * FROM pledge.create_transfer('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 100.00);

-- Check the balances.
SELECT name, balance FROM pledge.accounts_view ORDER BY name;
```

```text
 name  | balance
-------+---------
 alice |  100.00
 bank  | -100.00
```

Try to take out more than Alice has, and the database says no:

```sql
SELECT * FROM pledge.create_transfer('pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 'pla_01M730CQFEF2CR8VRK2DCTVZ7P', 1000);
```

```text
ERROR:  Account (id=pla_01M730CQFHFJQ9TRR4FW8SYK2Y, name=alice) does not allow negative balance
DETAIL:  {"code": "pledge.negative_balance_not_allowed", "account_id": "pla_01M730CQFHFJQ9TRR4FW8SYK2Y"}
```

## Why pledge

- **Correct by default.** Every transfer writes two entries that sum to zero. Each entry stores
  the account's running balance, so you can see the balance at any point in history.
- **Safe to retry.** Send your own transfer id. If you send the same transfer twice, it's booked
  once.
- **Hard to tamper with.** The database itself refuses to delete ledger rows or change amounts.
  You close accounts, you never delete them.
- **Fast.** Roughly 1.5 times faster than [pgledger](https://github.com/pgr0ss/pgledger) per
  transfer, and about 2 times faster for batches. See [Performance](performance.md).
- **Clear errors.** Each failure has a stable code, like `pledge.negative_balance_not_allowed`,
  that your app can match on.

pledge runs on PostgreSQL 17 and 18.

### Where it comes from

pledge grew out of [pgledger](https://github.com/pgr0ss/pgledger), [pgr0ss](https://github.com/pgr0ss)'s double-entry ledger
in plain SQL, which is already in use. pledge keeps its model, rewrites the hot path in Rust, and
still benchmarks against it.

pledge itself is young. It works and is validated, including a replay of a real bank's million
transactions, but the API may still change before 1.0. Pin the version you use, and
[tell us](https://github.com/pgpledge/pledge/issues) what you run into.

## Where next

::::cards
:::card[Install and set up]{href="install.md"}
Run Postgres with pledge, create the extension, give your app access.
:::

:::card[Accounts]{href="sql/accounts.md"}
Create accounts, stop overdrafts, close accounts.
:::

:::card[Transfers]{href="sql/transfers.md"}
Move money, one transfer or many at once.
:::

:::card[Django]{href="django/setup.md"}
Ledger models for your Django project.
:::

:::card[Build an exchange]{href="django/exchange.md"}
A step-by-step tour of a small currency exchange built on pledge.
:::
::::
