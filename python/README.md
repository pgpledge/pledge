# django-pledge

Django models for [pledge](https://github.com/pgpledge/pledge), a double-entry ledger inside
PostgreSQL. Create accounts and transfers with the usual Django API; the database keeps the books
straight.

[Documentation](https://pgpledge.github.io/pledge/django/setup/) ·
[Source](https://github.com/pgpledge/pledge/tree/main/python) ·
[Example app](https://github.com/pgpledge/pledge/tree/main/python/example)

> [!WARNING]
> **Alpha.** The API can still change between versions without upgrade scripts. Don't use it for
> real money yet. Feedback is welcome.

Requires Python 3.11+, Django 4.2+, psycopg 3 and a PostgreSQL database with the pledge
extension available.

## Install

```sh
pip install django-pledge
```

```python
INSTALLED_APPS = [
    # ...
    "django_pledge",  # app label "pledge"
]
```

```sh
python manage.py migrate pledge
```

The first migration runs `CREATE EXTENSION IF NOT EXISTS pledge`, which needs a superuser. If
your app role isn't one, create the extension by hand and give the role the
[grants from the docs](https://pgpledge.github.io/pledge/install/).

## Usage

```python
from decimal import Decimal

from django_pledge.models import Account, Transfer

cash = Account.objects.create(name="cash", currency="EUR")
alice = Account.objects.create(name="alice", currency="EUR", allow_negative_balance=False)

Transfer.objects.create(from_account=cash, to_account=alice, amount=Decimal("10.00"))
cash.transfer(alice, 5)  # the same, shorter

# one atomic batch, one statement
Transfer.objects.bulk_create(
    [
        Transfer(from_account=alice, to_account=cash, amount=Decimal("2.50")),
        Transfer(from_account=cash, to_account=alice, amount=1, metadata={"line": 2}),
    ]
)

alice.refresh_from_db()
alice.balance  # Decimal('13.50')
alice.history()  # entries with running balances, oldest first
alice.balance_at(some_datetime)
Transfer.objects.filter(from_account__name="cash", amount__gte=5)

Account.objects.assert_verified()  # raises if the ledger is inconsistent
```

Amounts are `Decimal`, `int` or a numeric string; `float` raises `ValidationError`. Writes run in
`transaction.atomic()`, follow `using()` / `db_manager()` and database routers, and have async
variants (`acreate`, `abulk_create`, `asave`).

Ledger rows are append-only: updating or deleting a stored row through the ORM raises
`ReadOnlyError`. Accounts are closed with `account.close()` (needs a zero balance), never deleted.

## Safe retries

Store a transfer id with your own record before you call the ledger, and send the same id when
you retry. The same transfer twice is booked once; different values raise `TransferConflict`.

```python
from django_pledge import TransferId

payment.transfer_id = TransferId.new()
payment.save()
cash.transfer(alice, payment.amount, id=payment.transfer_id)  # safe to retry
```

## Linking your models

Point a `ForeignKey` or `OneToOneField` at `pledge.Account` with `db_constraint=False` (the target
is a database view):

```python
from django.db import models


class Wallet(models.Model):
    user = models.OneToOneField("auth.User", models.CASCADE)
    account = models.OneToOneField(
        "pledge.Account", models.PROTECT, db_constraint=False, related_name="wallet"
    )


Wallet.objects.filter(account__currency="EUR", account__balance__gt=0)
```

Proxy models of `Account` are fine for extra methods; multi-table inheritance is rejected.

## Errors

Ledger rule violations raise subclasses of `django_pledge.LedgerError`, with the ids involved as
typed attributes:

```python
import django_pledge

try:
    cash.transfer(alice, amount)
except django_pledge.NegativeBalanceNotAllowed as e:
    e.account_id  # AccountId of the account that would go negative
except django_pledge.LedgerError as e:
    e.code, e.message
```

`django_pledge.translate(exc)` does the same for errors from raw SQL. All error classes are in
the [docs](https://pgpledge.github.io/pledge/django/usage/).

## Also included

- Typed ids: `AccountId`, `TransferId`, `EntryId`, and model fields for them.
- `django_pledge.admin.ReadOnlyAdmin` to browse the ledger in the admin.
- `django_pledge.json.LedgerJSONEncoder`, which writes Decimals in plain notation.

## Development

From the repository root, start PostgreSQL with pledge (`docker compose up -d --wait db`), then
in `python/`:

```sh
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run --group lint ty check
```

## License

[MIT](LICENSE)
