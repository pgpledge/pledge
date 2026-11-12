<p align="center">
  <a href="https://pgpledge.github.io/pledge/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/public/logo-dark.svg">
      <img src="docs/public/logo.svg" alt="pledge logo" width="72" height="72">
    </picture>
  </a>
</p>

<h1 align="center">pledge</h1>

<p align="center">
  <strong>A double-entry ledger inside PostgreSQL.</strong><br>
  Create accounts, move money between them with plain SQL, and let the database keep the books straight.
</p>

<p align="center">
  <a href="https://pgpledge.github.io/pledge/"><strong>Documentation</strong></a> ·
  <a href="https://pgpledge.github.io/pledge/install/">Install</a> ·
  <a href="https://pgpledge.github.io/pledge/sql/transfers/">Transfers</a> ·
  <a href="https://pgpledge.github.io/pledge/sql/errors/">Errors</a> ·
  <a href="https://pgpledge.github.io/pledge/django/setup/">Django</a> ·
  <a href="python/example">Example</a> ·
  <a href="bench/README.md">Benchmarks</a>
</p>

<p align="center">
  <a href="https://github.com/pgpledge/pledge/actions/workflows/extension.yml"><img src="https://github.com/pgpledge/pledge/actions/workflows/extension.yml/badge.svg" alt="Extension"></a>
  <a href="https://github.com/pgpledge/pledge/actions/workflows/python.yml"><img src="https://github.com/pgpledge/pledge/actions/workflows/python.yml/badge.svg" alt="Python"></a>
  <a href="https://github.com/pgpledge/pledge/actions/workflows/docs.yml"><img src="https://github.com/pgpledge/pledge/actions/workflows/docs.yml/badge.svg" alt="Docs"></a>
</p>

> [!WARNING]
> **Alpha.** The SQL API, the stored schema and the Python package can still change between versions
> without upgrade scripts. Don't use it for real money yet. Feedback is welcome.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/public/pledge-social-dark-square.svg">
  <img src="docs/public/pledge-social-light-square.svg" alt="pledge">
</picture>

## A quick look

```sql
CREATE EXTENSION pledge;

SELECT id FROM pledge.create_account('bank', 'USD');
-- pla_01M730CQFEF2CR8VRK2DCTVZ7P
SELECT id FROM pledge.create_account('alice', 'USD', allow_negative_balance => false);
-- pla_01M730CQFHFJQ9TRR4FW8SYK2Y

SELECT * FROM pledge.create_transfer('pla_01M730CQFEF2CR8VRK2DCTVZ7P', 'pla_01M730CQFHFJQ9TRR4FW8SYK2Y', 100.00);

SELECT name, balance FROM pledge.accounts_view ORDER BY name;
--  alice |  100.00
--  bank  | -100.00
```

## Why pledge

- **Correct by default.** Every transfer writes two entries that sum to zero, and each entry
  stores the account's running balance.
- **Safe to retry.** Send your own transfer id; the same transfer twice is booked once.
- **Hard to tamper with.** The database refuses to delete ledger rows or change amounts.
- **Fast.** Written in Rust with [pgrx]; about 1.5 to 2 times faster than [pgledger] per
  transfer, and about 2 times faster for batches.
- **Clear errors.** Each failure has a stable code, like `pledge.negative_balance_not_allowed`.

Runs on PostgreSQL 17 and 18. For Django there is [django-pledge](python/).

## Develop

Everything runs in Docker through [just](https://github.com/casey/just):

```sh
just up            # PostgreSQL 18 with pledge
just psql          # open psql
just docker-test   # cargo pgrx test, clippy and fmt
just validate      # bank, model and Berka validation suites
just docker-bench  # pgbench: pledge vs pgledger
just example       # exchange example on http://localhost:8000
```

## License

[MIT](LICENSE). The Berka dataset in `tests/validation/data/berka` is the PKDD'99 release by
Petr Berka and Marta Sochorova and is not covered by this license.

[pgrx]: https://github.com/pgcentralfoundation/pgrx
[pgledger]: https://github.com/pgr0ss/pgledger
