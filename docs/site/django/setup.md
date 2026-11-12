---
title: Django setup
description: Install django-pledge, add the app and run the migration.
---

`django-pledge` gives you Django models for the ledger: `Account`, `Transfer` and `Entry`. You
use them like normal models. Behind the scenes they call pledge's SQL functions.

It needs Python 3.11+, Django 4.2+, psycopg 3 and a PostgreSQL with pledge
([install](../install.md)).

:::steps
1. **Install the package.**

   ```sh
   pip install django-pledge
   ```

2. **Add the app** to your settings.

   ```python title="settings.py"
   INSTALLED_APPS = [
       # ...
       "django_pledge",
   ]
   ```

3. **Run the migration.**

   ```sh
   python manage.py migrate pledge
   ```

   This runs `CREATE EXTENSION IF NOT EXISTS pledge`. That needs a superuser. If your migration
   role isn't one, create the extension by hand first and the migration skips it.

4. **Give your app role access.** If your app connects with its own role, run the
   [grants](../install.md) once.
:::

:::tip[Faster: let psycopg prepare queries]
Turn on server-side binding. psycopg then prepares a query on the server after it has run a few
times, so PostgreSQL stops planning it on every call. In our tests this made each transfer about a
fifth faster.

```python title="settings.py"
DATABASES = {
    "default": {
        # ...
        "OPTIONS": {"server_side_binding": True},
    }
}
```

Behind PgBouncer in transaction mode, this needs PgBouncer 1.21 or newer.
:::

Next: [use the models](usage.md).
