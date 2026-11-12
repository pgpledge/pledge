<p align="center">
  <a href="https://pgpledge.github.io/pledge/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/pgpledge/pledge/main/docs/public/logo-dark.svg">
      <img src="https://raw.githubusercontent.com/pgpledge/pledge/main/docs/public/logo.svg" alt="pledge logo" width="72" height="72">
    </picture>
  </a>
</p>

<h1 align="center">django-pledge</h1>

<p align="center">
  <strong>Django support for pledge, a double-entry ledger inside PostgreSQL.</strong>
</p>

> [!WARNING]
> **Alpha.** The SQL API, the stored schema and the Python API can still change between
> versions without upgrade scripts. Don't use it for real money yet. Feedback is welcome.

`django_pledge` gives you `Account`/`Transfer`/`Entry` models over pledge's views, written through
the extension's functions behind the usual Django API (`objects.create()`, `save()`,
`bulk_create()`), plus typed ids (`AccountId`, `TransferId`, `EntryId`), ledger errors, model
fields and a migration operation.

Requires Python 3.11+ and a database with the pledge extension available.

## Install

```sh
pip install django-pledge
```

django-pledge pulls in Django and psycopg 3, Django's PostgreSQL driver, as usual for a
Postgres project (psycopg2 is not supported: error translation reads psycopg 3's diagnostics).
No psycopg adapters need to be registered; ids are sent as text and cast in SQL.

## Database setup

The extension is installed by a superuser, either by hand (`CREATE EXTENSION pledge;`) or by
the app's first migration (`pledge.0001_initial` runs `CREATE EXTENSION IF NOT EXISTS pledge`).

The ledger functions run with the caller's rights (they are not `SECURITY DEFINER`), so the
application role needs these grants. Run them as the extension owner, in each database:

```sql
GRANT USAGE ON SCHEMA pledge TO app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA pledge TO app;
-- reading: the public views
GRANT SELECT ON pledge.accounts_view, pledge.transfers_view, pledge.entries_view TO app;
-- writing through pledge.create_account/create_transfer(s)/close_account
GRANT SELECT, INSERT, UPDATE ON pledge.accounts, pledge.transfers, pledge.entries TO app;
```

Do not grant `DELETE` or `TRUNCATE`: ledger rows are never removed (triggers reject it anyway).
No `search_path` setup is needed: the id comparison operators live in `pg_catalog`, so
`=`, `IN`, joins and `ORDER BY` on id columns work for any role.

## Django

```python
INSTALLED_APPS = [
    # ...
    "django_pledge",  # app label "pledge"
]
```

```sh
python manage.py migrate pledge
```

The models are unmanaged and read the public views. Writes call the extension's functions
through the familiar Django entry points:

```python
from decimal import Decimal
from django_pledge.models import Account, Entry, Transfer

cash = Account.objects.create(name="cash", currency="EUR", allow_negative_balance=False)
alice = Account.objects.create(name="alice", currency="EUR", metadata={"user": 42})

t = Transfer.objects.create(
    from_account=cash, to_account=alice, amount=Decimal("10.00"), metadata={"order": 7}
)
t.id  # TransferId('plt_01K9...'), t.created_at, t.event_at are filled in
cash.transfer(alice, 5)  # shortcut for the same thing
Transfer.objects.create(from_account=cash.id, to_account="pla_01K9...", amount=1)  # ids work too

t = Transfer(from_account=alice, to_account=cash, amount=1)
t.save()  # a new instance is created the same way

# batch: atomic, one statement, returns the same instances populated, in order
Transfer.objects.bulk_create(
    [
        Transfer(from_account=alice, to_account=cash, amount=Decimal("2.50")),
        Transfer(from_account=cash, to_account=alice, amount=1, metadata={"line": 2}),
    ],
    metadata={"batch": "2025-01"},  # defaults for instances that don't set their own
    event_at=None,  # likewise for event_at
)

alice.refresh_from_db()
alice.balance  # Decimal('...')
alice.history()  # entries, oldest first (running balances)
alice.transfers()  # transfers in/out, in the order they hit the account
alice.balance_at(some_datetime)  # balance as of a point in time
alice.outgoing.all(), alice.incoming.all(), alice.entries.all()
Transfer.objects.filter(from_account__name="cash", amount__gte=5)
Entry.objects.select_related("account", "transfer").filter(account=alice)

alice.close()  # keeps history, rejects new transfers; sets closed_at
Account.objects.verify()  # [Check(check_name, ok, detail), ...]
Account.objects.assert_verified()  # raises django_pledge.VerificationFailed on a failed check
```

| Django call                                      | runs                         |
|--------------------------------------------------|------------------------------|
| `Account.objects.create(...)`, new `Account.save()` | `pledge.create_account`   |
| `Transfer.objects.create(...)`, new `Transfer.save()`, `account.transfer(...)` | `pledge.create_transfer` |
| `Transfer.objects.bulk_create([...])`            | `pledge.create_transfers`    |
| `account.close()`                                | `pledge.close_account`       |
| `Account.objects.verify()` / `assert_verified()` | `pledge.verify()`            |

Amounts are `Decimal`, `int` or a numeric string; `float` and `bool` raise `ValidationError`. Every write runs in
`transaction.atomic(using=...)`, so a failure inside your own transaction rolls back only its
savepoint. Database routing works as usual: `Account.objects.db_manager("ledger").create(...)`,
`Transfer.objects.using("ledger").bulk_create(...)`, `obj.save(using="ledger")`, or your
router's `db_for_write`; `account.transfer()`/`close()` use the account's own database. The async
variants (`acreate`, `abulk_create`, `asave`) work too.

### Append-only

Ledger rows are never updated or deleted through the ORM. Saving an instance that is already
stored (or `save(update_fields=...)`), `delete()`, `QuerySet.update()`/`delete()`,
`bulk_update()`, `get_or_create()`, `update_or_create()` and `loaddata` raise
`django_pledge.models.ReadOnlyError`. `Entry` rows are written by the transfer functions only.

An account's balance and version change only through transfers, and closing is
`account.close()`. Account ids are assigned by the database (leave `id` unset). The extension
does allow renaming an account; that is deliberately not exposed through `save()`, so do it in
SQL if you need it (`UPDATE pledge.accounts SET name = %s, updated_at = now() WHERE id = %s`).

### Idempotent retries

Transfer ids are generated client-side. Generate one before the call, store it with your own
record, and send the same id when you retry; a retry with the same values returns the stored
transfer and moves no money, different values raise `TransferConflict`:

```python
from django_pledge import TransferId

payment.transfer_id = TransferId.new()
payment.save()
cash.transfer(alice, payment.amount, id=payment.transfer_id)  # safe to retry
```

When `id` is omitted a fresh `TransferId.new()` is set on the instance before the call, so the
transfer always has an id you can record, and calling `save()` (or `bulk_create()` with the same
instances) again after a failure is itself an idempotent retry.

### Linking your models to accounts

Use a normal `ForeignKey` to `pledge.Account` with `db_constraint=False` (the target is a view,
which cannot carry a foreign key constraint):

```python
from django.db import models
from django_pledge import AccountIdField


class Wallet(models.Model):
    user = models.OneToOneField("auth.User", models.CASCADE)
    account = models.ForeignKey(
        "pledge.Account", models.PROTECT, db_constraint=False, related_name="wallets"
    )


Wallet.objects.filter(account__currency="EUR", account__balance__gt=0)
Account.objects.filter(wallets__user=request.user)
```

The column type is `pledge.account_id`. `AccountIdField`, `TransferIdField` and `EntryIdField`
can also be used directly, and `NumericField` is an unbounded `numeric` (no precision/scale),
like pledge's amounts.

`django_pledge.admin.ReadOnlyAdmin` is a `ModelAdmin` mixin for browsing the ledger in the admin.

## Errors

Ledger rule violations raise `django_pledge.LedgerError` subclasses (from the model write methods, or
via `django_pledge.translate(exc)` for any Django/psycopg database error, e.g. from raw SQL). They are
matched on the `code` in the JSON object the extension puts in the error DETAIL, not on the
message. The other keys of that object become typed attributes:

| class                       | code                                  | attributes                       |
|-----------------------------|---------------------------------------|----------------------------------|
| `InvalidRequest`            | `pledge.invalid_request`              |                                  |
| `AccountNotFound`           | `pledge.account_not_found`            | `account_id`                     |
| `AccountClosed`             | `pledge.account_closed`               | `account_id`                     |
| `AccountAlreadyClosed`      | `pledge.account_already_closed`       | `account_id`                     |
| `AccountNotEmpty`           | `pledge.account_not_empty`            | `account_id`, `balance`          |
| `NegativeBalanceNotAllowed` | `pledge.negative_balance_not_allowed` | `account_id`                     |
| `PositiveBalanceNotAllowed` | `pledge.positive_balance_not_allowed` | `account_id`                     |
| `CurrencyMismatch`          | `pledge.currency_mismatch`            | `from_currency`, `to_currency`   |
| `TransferConflict`          | `pledge.transfer_conflict`            | `transfer_id`                    |
| `DuplicateTransferId`       | `pledge.duplicate_transfer_id`        | `transfer_id`                    |
| `Immutable`                 | `pledge.immutable`                    | `table`                          |

`account_id` is an `AccountId`, `transfer_id` a `TransferId`, `balance` a `Decimal` and the
others are `str`. A class only has the attributes in its row. `TransferConflict.transfer_id` is
`None` when two transactions raced to create the same new id: Postgres reports that as a unique
violation on `transfers_pkey`, which has no JSON DETAIL. `e.detail` is the parsed DETAIL as a
dict.

```python
import django_pledge

try:
    cash.transfer(alice, amount, id=tid)
except django_pledge.NegativeBalanceNotAllowed as e:
    e.account_id  # the AccountId that would go negative
except django_pledge.LedgerError as e:
    e.code, e.message, e.detail
```

## JSON

Balances keep the scale of their transfers, so a zero BTC balance is `Decimal('0E-8')` and
`DjangoJSONEncoder` writes `"0E-8"`. `django_pledge.json.LedgerJSONEncoder` writes Decimals in
plain notation (`"0.00000000"`). The models' `metadata` fields use it; pass it to `JsonResponse`
or `json.dumps(..., cls=LedgerJSONEncoder)` for your own output.

## Ids

```python
from django_pledge import AccountId

i = AccountId.new()  # UUIDv7: millisecond timestamp + random
str(i)  # 'pla_01K9FKX2S1FGYSWKX1PB1X9SFP'
AccountId("pla_01k9fkx2s1fgyswkx1pb1x9sfp") == i  # lowercase, O->0, I/L->1 accepted
i.uuid, i.bytes, i.timestamp
```

Ids of different kinds never compare equal and cannot be ordered against each other.

## Development

```sh
docker compose up -d --wait db     # from the repo root: Postgres with pledge on :5432
uv sync --all-groups
uv run pytest
uv run ruff check src tests example && uv run --group lint ty check
```

Tests create a fresh database (`PLEDGE_TEST_DB`, default `pledge_python_test`) and a role
`pledge_app` with only the grants above, using the superuser DSN in `PLEDGE_TEST_DSN`
(default `host=localhost port=5432 user=pledge password=pledge dbname=pledge`).

## Example project

`example/` is a small currency exchange (`core` settings, `exchange` app) built on the
public API. Start with `example/exchange/services.py`.

Run it with Docker from the repository root, no local Python needed:

```sh
just example            # http://localhost:8000/  and  /admin/ (admin / admin)
just example-simulate   # 20 traders, 500 random trades, then checks every currency sums to 0
```

The first start creates the `exchange` database, funds the exchange's liquidity and creates the
admin user. Open http://localhost:8000/ for a small web interface: create a trader, then deposit
by bank transfer or card, preview and place trades, withdraw, close wallets and read each wallet's
statement. The home page shows the broker's accounts per currency, a button for the card provider
to pay out, and whether `Account.objects.verify()` passes. Every form carries a request id
rendered with the page, so a double submit or a refresh books nothing twice.

The same exchange is also a JSON API under `/exchange/`:

```sh
curl -X POST localhost:8000/exchange/traders/ -d '{"name": "alice"}'
curl -X POST localhost:8000/exchange/traders/1/deposits/ -d '{"currency": "EUR", "amount": "1000.00"}'
curl -X POST localhost:8000/exchange/traders/1/deposits/ \
     -d '{"currency": "EUR", "amount": "250.00", "method": "card"}'
curl -X POST localhost:8000/exchange/house/payouts/ -d '{"currency": "EUR"}'
curl -X POST localhost:8000/exchange/traders/1/trades/ \
     -d '{"request_id": "t1", "sell": "EUR", "buy": "BTC", "amount": "500.00"}'
curl localhost:8000/exchange/traders/1/
curl localhost:8000/exchange/traders/
```

- Customers are the app's own `Trader` model; Django's auth users only log in to the admin.
- Every trader gets one ledger account per currency, EUR, USD and BTC with 8 decimals, linked by
  a `Wallet` model with `OneToOneField("pledge.Account", db_constraint=False)`. Trader accounts
  can't go negative.
- Per currency the broker owns a liquidity account holding its inventory and a fee revenue
  account, plus three accounts for money outside the exchange, which never go above zero: `card`
  (money held at the card provider, not yet paid out), `bank` (the broker's own bank account) and
  `card_costs` (the card provider's fees, an expense). Ledger names look like `house:card:EUR`.
- A bank deposit moves money from `bank` to the trader. A card deposit is one atomic batch of
  `card` → trader for the full amount and `card_costs` → `card` for the 1.5% card fee
  (`EXCHANGE_CARD_FEE_RATE`, rounded up). The fee leg's id is derived from the deposit's id, so a
  retried card deposit books nothing twice. When the card provider pays out, one `bank` → `card`
  transfer brings `card` back to zero. Withdrawals pay out from `bank`, and `fund_liquidity` moves
  the owner's capital from `bank` to liquidity.
- A trade converts at a quoted rate: pledge never crosses currencies, so it is one atomic
  `bulk_create` batch of trader → house in the sold currency, house → trader in the bought one
  and a fee leg, each with `{"kind": "trade", "trade": id, "leg": ...}` metadata. If any leg
  fails because the trader or the exchange runs short or an account is closed, nothing is booked.
- Trades and withdrawals carry a client `request_id` and store their transfer ids before pledge
  is called, so a retried request replays the original outcome instead of booking twice.
- Closing a wallet pays out its balance and closes the account in one transaction. There's a
  portfolio, a statement from `account.history()`, a proxy model, the web pages, JSON views and
  a read-only admin for the ledger.

`uv run pytest` runs its tests too, in a child process with their own database
(`PLEDGE_EXAMPLE_TEST_DB`, default `pledge_example_test`, as role `pledge_app`); run them
directly with `uv run pytest example`.

To run it by hand, point `PLEDGE_DSN` at a database with the extension and, from `example/`:

```sh
python manage.py migrate
python manage.py fund_liquidity BTC 10
python manage.py simulate --traders 20 --trades 500
python manage.py runserver
```

`simulate` signs up traders, runs random card and bank deposits, trades (some retried),
withdrawals and card payouts, then runs `Account.objects.assert_verified()` and prints per
currency what the customers hold and each house account, which always sum to zero.

## Extending accounts with your own data

Ledger rows are immutable and only come from pledge's functions, so keep your own data next to
them instead of inside them:

```python
class Wallet(models.Model):  # your mutable data, your table
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    kind = models.CharField(max_length=20)
    account = models.OneToOneField(
        "pledge.Account", on_delete=models.PROTECT, db_constraint=False, related_name="wallet"
    )


wallet.account.balance, account.wallet.customer
```

For extra *behaviour* (methods, managers, a different admin), a proxy model is fine:

```python
class Pot(Account):
    class Meta:
        proxy = True

    def is_empty(self) -> bool:
        return self.balance == 0
```

Multi-table inheritance (`class CustomerAccount(Account)` without `proxy = True`) is rejected
with `ImproperlyConfigured`: Django would write a parent row through the ORM.

## Names

Everything pledge stores lives in its own PostgreSQL schema `pledge` (`pledge.accounts`,
`pledge.transfers`, `pledge.entries`, read through `pledge.*_view`), so it can't collide with
your tables in `public`, including your own `Account` model. In Django the models are
`pledge.Account` / `pledge.Transfer` / `pledge.Entry` (app label `pledge`); import with an alias
if you also have an `Account` (`from django_pledge.models import Account as LedgerAccount`). The
only names pledge claims are the schema `pledge` and the Django app label `pledge`.
