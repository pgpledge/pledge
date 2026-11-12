# Benchmarks: pledge vs pgledger

Runs the same workload against [pgledger](https://github.com/pgr0ss/pgledger) (PL/pgSQL)
and pledge (Rust/pgrx) on the **same Postgres server**, with `pgbench`. Every
target/scenario/run gets a fresh database, so size growth is comparable.

## In Docker (no local Postgres needed)

```sh
just docker-bench PROFILE=quick              # ~1 min: does it run, roughly how fast
just docker-bench                            # standard, ~4 min
just docker-bench PROFILE=scale              # ~2 min: TPS at 1/4/16/32 clients
just docker-bench PROFILE=full               # ~20 min: low+high contention, 3 runs
just bench-ladder                            # quick -> scale -> full in a row
just docker-bench ACCOUNTS=10 RUNS=3         # explicit variables override the profile
```

Runs `run.sh` in a client container against the `db` service (postgres:18 + pledge,
built from the repo `Dockerfile`). Results land in `bench/results/` on the host.

## Prerequisites (local run)

- A running Postgres (17 or 18) with pledge installed into it:
  `cargo pgrx install --release --pg-config <path/to/pg_config>`
- `psql`, `pgbench`, `createdb`, `dropdb` from that same Postgres on your `PATH`
- pgledger source: uses `../plans/reference/pgledger` if present (or `PGLEDGER_DIR`), otherwise clones it
  into `bench/.cache/pgledger`

## Run

```sh
just bench                                   # defaults below
ACCOUNTS=10 just bench                       # high contention
TARGETS=pgledger DURATION=10 just bench      # sanity-check the harness alone
SCENARIOS=batch RUNS=3 just bench
```

| Variable | Default | Meaning |
|---|---|---|
| `PROFILE` | `standard` | `quick` / `standard` / `scale` / `full`: defaults for the rows below |
| `ACCOUNTS` | 50 | accounts to pick from (fewer = more lock contention); list allowed |
| `CLIENTS` / `THREADS` | 20 / 4 | pgbench `-c` / `-j`; `CLIENTS` list allowed, `-j` capped at `-c` |
| `DURATION` | 30 | seconds per run |
| `RUNS` | 1 | repeat each scenario (use 3 before claiming a win) |
| `SCENARIOS` | `transfer batch idempotent` | see below |
| `TARGETS` | `pgledger pledge` | |

## Scenarios

- **transfer**: one transfer between two random distinct accounts
- **batch**: one `create_transfers` call with two transfers (four entries)
- **idempotent**: one transfer with a fresh idempotency token: an `idempotency_key` on pgledger (advisory lock + key insert), a client transfer id on pledge (existence check after the account locks)

The random account lookup (`bench_accounts`) is identical on both sides, so it cancels out.

## Output

`bench/results/<timestamp>-<profile>.csv` with TPS, average and p99 latency, transfers, bytes per transfer,
and an invariant check computed straight from the tables (zero-sum per transfer, balance and
version equal the entries). A run with `check = FAIL` is a bug, not a result.

## Reading the numbers

Most of a transfer's cost is row locks, index maintenance and WAL, which are the same in both.
Expect wins from smaller rows/indexes (binary IDs), fewer statements per transfer and cached
plans. Expect them to be modest; say so when they are.
