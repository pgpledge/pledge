---
title: Safe retries
description: Give each transfer your own id, and sending it twice books it only once.
---

Networks fail. Sometimes you send a transfer and never hear back. Did it go through? With a
transfer id of your own, you don't need to know: just send it again.

```sql
SELECT pledge.new_transfer_id();  -- plt_01M730CQGBEWN8YZFP2T9FKXRY

SELECT amount FROM pledge.create_transfer('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 5, id => 'plt_01M730CQGBEWN8YZFP2T9FKXRY');  -- books it
SELECT amount FROM pledge.create_transfer('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 5, id => 'plt_01M730CQGBEWN8YZFP2T9FKXRY');  -- same row back, no new money
```

The second call returns the transfer that was already booked. Alice gets 5, not 10.

## How to use it

:::steps
1. **Make the id first.** Use `pledge.new_transfer_id()` (or `TransferId.new()` in Python).
2. **Save it** with your own record, such as the order or payment.
3. **Send the transfer** with `id => ...`.
4. **On a timeout or crash, send it again** with the same id and the same values. Keep going
   until you get rows back or a ledger error.
:::

```sql
-- in your own table, before the transfer
INSERT INTO payments (amount, transfer_id) VALUES (25.00, pledge.new_transfer_id())
RETURNING transfer_id;

-- then, as often as needed
SELECT * FROM pledge.create_transfer('pla_01M730CQG2ERN9H87BPCCCNFXQ', 'pla_01M730CQFPFJASDMZCRS59HPJ6', 25.00, id => 'plt_01M730CQGBFCQVT21X7VW83C53');
```

Your `transfer_id` column can use the type `pledge.transfer_id`.

## Same id, different values

If a transfer with that id exists but with a different amount, accounts, `event_at` or
`metadata`, you get an error instead:

```sql
SELECT * FROM pledge.create_transfer('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 6, id => 'plt_01M730CQGBEWN8YZFP2T9FKXRY');
```

```text
ERROR:  Transfer (id=plt_01M730CQGBEWN8YZFP2T9FKXRY) already exists with different values
DETAIL:  {"code": "pledge.transfer_conflict", "transfer_id": "plt_01M730CQGBEWN8YZFP2T9FKXRY"}
```

That's a bug on your side: an id was reused, or a retry changed its values.

:::caution[Keep event_at the same]
A retry has to send the same `event_at` and `metadata` as the first try. If you left
`event_at` out the first time, leave it out on the retry too.
:::

## Batches

The same works for `create_transfers`. Give each request an id and retry the whole call.
Transfers that were already booked come back as they are. New ones are booked. Don't use the
same id twice in one call; that fails with `pledge.duplicate_transfer_id`.

## What a retry tells you

| You get              | It means                                                   |
| -------------------- | ---------------------------------------------------------- |
| Rows back            | Booked, now or by an earlier try                           |
| `transfer_conflict`  | The id is taken by a different transfer                    |
| Another ledger error | The transfer was refused. Retrying gives the same error    |
| Connection error     | Still unknown. Retry again                                 |
