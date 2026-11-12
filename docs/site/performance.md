---
title: Performance
description: How fast pledge books transfers compared to pgledger, and how it was tested for correctness.
---

pledge is benchmarked against [pgledger](https://github.com/pgr0ss/pgledger), a double-entry
ledger written in PL/pgSQL, with pgbench. Both run in the same PostgreSQL 18 container on the same
laptop, with the same accounts and the same workload. Higher is better.

## Transfers per second

One client, `synchronous_commit` off so the numbers show the work each ledger does, not the disk:

:::bench[Single transfer, 1 client]{highlight="pledge"}
pledge: 5361
pgledger: 3547
:::

:::bench[Batch of two transfers, 1 client]{highlight="pledge"}
pledge: 6841
pgledger: 4327
:::

:::bench[Retry of an existing transfer, 1 client]{highlight="pledge"}
pledge: 4606
pgledger: 3141
:::

Eight clients at once, same settings:

:::bench[Single transfer, 8 clients]{highlight="pledge"}
pledge: 19517
pgledger: 13579
:::

:::bench[Batch of two transfers, 8 clients]{highlight="pledge"}
pledge: 18005
pgledger: 10089
:::

## Bigger batches

With durable commits on, larger batches pay off most: `create_transfers` writes a whole batch in
one statement. Transfers per second at 8 clients, median of three runs:

:::bench[Batches of 10 transfers]{highlight="pledge"}
pledge: 7956
pgledger: 5362
:::

:::bench[Batches of 100 transfers]{highlight="pledge"}
pledge: 10633
pgledger: 4913
:::

## Storage

Each transfer and its two entries take about 16% less space:

:::bench[Bytes per transfer]{highlight="pledge"}
pledge: 627 B
pgledger: 748 B
:::

## Where the speed comes from

- **One statement per call.** Locking the accounts, checking balances and writing the transfer
  and both entries happen in one SQL statement, prepared once per connection.
- **No replanning.** The write function always uses its cached plan; before, PostgreSQL replanned
  every call, which cost about 30% of the server time.
- **16-byte ids.** Ids are stored as UUIDv7 values and shown as text like `pla_01K9...` only when
  you read them.

## Tested for correctness

Speed only counts if the books stay straight. Besides the unit tests, three validation suites run
against a real database (`just validate`):

| Suite | What it does | Result |
|-------|--------------|--------|
| Bank | Concurrent transfers while connections are killed at random | 18,726 transfers, 197 killed connections, 0 violations |
| Model | Random operations checked against a simple in-memory ledger | 8,000 operations, 0 mismatches |
| Berka | Replays the PKDD'99 Czech bank dataset | 1,056,304 transfers, 904,603 running balances match the dataset exactly |

After each suite `pledge.verify()` passes, every currency sums to zero and every balance matches
the expected value.

## Run it yourself

```sh
just docker-bench PROFILE=quick   # about a minute
just docker-bench PROFILE=cpu     # the numbers above
just validate                     # the three validation suites
```

Results are written to `bench/results/`. See
[bench/README.md](https://github.com/pgpledge/pledge/blob/main/bench/README.md) for every option.
