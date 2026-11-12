---
title: Transfers
description: Move money between accounts, one transfer at a time or many in one call.
---

## Move money

Use the ids `create_account` returns. The examples below use `bank` = `pla_01M730CQFEF2CR8VRK2DCTVZ7P`,
`alice` = `pla_01M730CQFHFJQ9TRR4FW8SYK2Y`, `bob` = `pla_01M730CQFMENFR9M7TNTR98C4S` and
`shop` = `pla_01M730CQFPFJASDMZCRS59HPJ6`.

```sql
SELECT * FROM pledge.create_transfer('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 100.00);
```

This moves 100.00 from `bank` to `alice`. The bank goes down by 100, Alice goes up by 100.

The rules:

- The amount must be greater than zero.
- Both accounts must exist, be open, and use the same currency.
- The [balance flags](accounts.md#stop-overdrafts) of both accounts must still hold afterwards.

If any rule fails, you get an [error](errors.md) and nothing is written.

## Add your own data

Use `metadata` for anything you want to keep with the transfer, like an order number. Use
`event_at` for when it really happened, if that's not now.

```sql
SELECT amount, event_at, metadata
FROM pledge.create_transfer('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 12.50,
                            event_at => '2025-06-01',
                            metadata => '{"order": 7}');
```

```text
 amount |        event_at        |   metadata
--------+------------------------+--------------
  12.50 | 2025-06-01 00:00:00+00 | {"order": 7}
```

Without `event_at`, it's the time of the transfer. `created_at` is always the time pledge booked
it.

## Many transfers at once

Use `create_transfers` with a list of `pledge.request(...)`. It's all or nothing: if one transfer
fails, none are booked.

```sql
SELECT * FROM pledge.create_transfers(ARRAY[
    pledge.request('pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 'pla_01M730CQFMENFR9M7TNTR98C4S', 30),
    pledge.request('pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 'pla_01M730CQFPFJASDMZCRS59HPJ6', 2.50, metadata => '{"fee": true}')
]);
```

You get one row back per request, in the same order.

`pledge.request` takes the same optional fields as a single transfer:

```sql
pledge.request(from_account_id, to_account_id, amount, id, event_at, metadata)
```

Batches are also the fastest way to book many transfers. One call with 100 transfers is much
cheaper than 100 calls.

:::details[Order inside a batch]
Transfers run in the order you list them. So a later transfer can spend money that an earlier
one in the same batch brought in:

```sql
-- Alice starts at 0 and may not go negative.
SELECT * FROM pledge.create_transfers(ARRAY[
    pledge.request('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 5),    -- alice: 5
    pledge.request('pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 'pla_01M730CQFMENFR9M7TNTR98C4S', 5)      -- alice: 0, fine
]);
```

Swap the two lines and the batch fails.
:::

### Defaults for the whole batch

Pass `event_at` and `metadata` after the list. They fill in for every request that doesn't set
its own:

```sql
SELECT metadata->>'order' AS "order", event_at::date
FROM pledge.create_transfers(ARRAY[
        pledge.request('pla_01M730CQFSF7ZSF5J115593B6R', 'pla_01M730CQFWFK3BEDJ1HS72AX9E', 1, metadata => '{"order": 1}', event_at => '2024-01-01'),
        pledge.request('pla_01M730CQFSF7ZSF5J115593B6R', 'pla_01M730CQFWFK3BEDJ1HS72AX9E', 2, metadata => '{"order": 2}'),
        pledge.request('pla_01M730CQFSF7ZSF5J115593B6R', 'pla_01M730CQFWFK3BEDJ1HS72AX9E', 3)],
     event_at => '2025-06-01',
     metadata => '{"order": "default"}');
```

```text
  order  |  event_at
---------+------------
 1       | 2024-01-01
 2       | 2025-06-01
 default | 2025-06-01
```

:::details[Building a batch from your app]
Don't glue `ARRAY[...]` strings together in code. Send plain arrays as parameters and build the
requests in SQL. The query text stays the same for any batch size:

```sql
SELECT * FROM pledge.create_transfers(
    ARRAY(
        SELECT pledge.request(r.f::pledge.account_id, r.t::pledge.account_id, r.amount)
        FROM unnest($1::text[], $2::text[], $3::numeric[]) WITH ORDINALITY AS r (f, t, amount, n)
        ORDER BY r.n));
```

Here `$1` and `$2` are lists of account ids (`pla_…`), and `$3` the amounts.
:::

## Make it fast

- **Batch when you can.** One `create_transfers` call with many requests is much faster than many
  single calls.
- **Use prepared statements.** Send the call as a prepared statement, so PostgreSQL plans it once
  instead of on every call. psycopg does this by itself after a query has run a few times; with
  other drivers, prepare it yourself (`PREPARE`, or your driver's prepare option).

## Can I undo a transfer?

No. Booked transfers can't be changed or deleted. To undo one, book a new transfer the other
way:

```sql
SELECT * FROM pledge.create_transfer('pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 'pla_01M730CQFEF2CR8VRK2DCTVZ7P', 12.50,
                                     metadata => '{"refund_of": "order 7"}');
```
