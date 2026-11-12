---
title: Install and set up
description: Run PostgreSQL with pledge, create the extension and give your app role access.
---

:::caution[Alpha]
pledge is alpha software: the SQL API, the stored schema and the Python package can still change between versions without upgrade scripts, so don't use it for real money yet. Feedback is welcome.
:::

:::::steps
1. **Get a PostgreSQL with pledge.** Pick one:

   ::::tabs{sync="install"}
   :::tab[Docker]
   The repository comes with a Docker setup: PostgreSQL 18 with pledge already installed.
   From the repository root, run:

   ```sh
   docker compose up -d --wait db
   ```

   The first run builds the image, which takes a few minutes.

   It listens on `localhost:5432`. User, password and database are all `pledge`.
   :::
   :::tab[From source]
   You need Rust and [cargo-pgrx](https://github.com/pgcentralfoundation/pgrx) 0.19.3. Then
   build and install into your own PostgreSQL 18:

   ```sh
   cargo pgrx init --pg18 $(which pg_config)
   cargo pgrx install --release --pg-config $(which pg_config)
   ```

   For PostgreSQL 17, use `--pg17` and add `--no-default-features --features pg17`.
   :::
   ::::

2. **Create the extension.** Connect as a superuser and run:

   ```sql
   CREATE EXTENSION pledge;
   ```

   Everything pledge adds lives in the `pledge` schema. Your own tables are not touched.

3. **Give your app access.** Your app should connect with its own role, not a superuser. Run
   this once per database, replacing `app` with your role name:

   ```sql
   GRANT USAGE ON SCHEMA pledge TO app;
   GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA pledge TO app;
   GRANT SELECT ON pledge.accounts_view, pledge.transfers_view, pledge.entries_view TO app;
   GRANT SELECT, INSERT, UPDATE ON pledge.accounts, pledge.transfers, pledge.entries TO app;
   ```

4. **Check it works.**

   ```sql
   SELECT * FROM pledge.verify();
   ```

   Every row should say `ok = t`.
:::::

:::note[No DELETE grant]
Don't grant `DELETE` or `TRUNCATE`. Ledger rows are never removed, and pledge rejects it anyway.
:::

:::tip[Using Django?]
The Django app can run `CREATE EXTENSION` for you in a migration. See
[Django setup](django/setup.md).
:::

Next: [create some accounts](sql/accounts.md).
