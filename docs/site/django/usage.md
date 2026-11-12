---
title: Using the models
description: Create accounts, move money, read history and handle errors from Django.
---

```python
from decimal import Decimal
from django_pledge.models import Account, Entry, Transfer
```

## Create accounts

```python
bank = Account.objects.create(name="bank", currency="EUR")
alice = Account.objects.create(
    name="alice", currency="EUR", allow_negative_balance=False, metadata={"user": 42}
)

alice.id        # AccountId('pla_01K9...')
```

Leave `id` unset. The database picks it.

## Move money

```python
bank.transfer(alice, Decimal("100.00"))

# the same thing, spelled out
Transfer.objects.create(from_account=bank, to_account=alice, amount=Decimal("100.00"))
```

Add your own data with `metadata`, and the real date with `event_at`:

```python
t = bank.transfer(alice, Decimal("12.50"), metadata={"order": 7})
t.id            # TransferId('plt_01K9...')
t.created_at    # filled in
```

:::caution[Use Decimal]
Amounts must be `Decimal` or `int`. A `float` raises `ValidationError`.
:::

## Many at once

`bulk_create` books a list of transfers in one call. All or nothing.

```python
Transfer.objects.bulk_create(
    [
        Transfer(from_account=alice, to_account=seller, amount=Decimal("9.00")),
        Transfer(from_account=alice, to_account=revenue, amount=Decimal("1.00")),
    ],
    metadata={"order": 7},   # for every transfer that doesn't set its own
)
```

It returns the same instances, filled in, in order.

## Safe retries

Make the transfer id first and store it on your own model. Then retrying is safe: the same id
is booked once.

```python
from django_pledge import TransferId

order.transfer_id = TransferId.new()
order.save()

alice.transfer(shop, order.amount, id=order.transfer_id)   # safe to run again
```

Tip: give your model a field with a default, and every new row gets an id for free:

```python
from django_pledge import TransferIdField

class Order(models.Model):
    transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
```

See [safe retries](../sql/retries.md) for the rules.

## Read balances and history

```python
alice.refresh_from_db()
alice.balance                     # Decimal('97.50')
alice.is_closed                   # False

alice.history()                   # entries, oldest first, with running balances
alice.transfers()                 # transfers in and out, in order
alice.balance_at(some_datetime)   # balance at that moment

Transfer.objects.filter(metadata__order=7)
Entry.objects.filter(account=alice).select_related("transfer")
```

Balances keep the scale of the transfers, so an empty BTC account shows `Decimal('0E-8')`.
`DjangoJSONEncoder` writes that as `"0E-8"`. `django_pledge.json.LedgerJSONEncoder` writes
`"0.00000000"` instead. The `metadata` fields use it, and you can pass it to your own responses:

```python
from django_pledge.json import LedgerJSONEncoder

JsonResponse({"balance": alice.balance}, encoder=LedgerJSONEncoder)
```

A statement for a customer:

```python
for entry in alice.history().select_related("transfer"):
    print(entry.created_at, entry.amount, entry.account_current_balance)
```

## Close an account

```python
from django.db import transaction

with transaction.atomic():
    alice.refresh_from_db()
    if alice.balance > 0:
        alice.transfer(bank, alice.balance)
    alice.close()
```

`close()` needs a zero balance. If money lands in between, it raises `AccountNotEmpty` and the
whole block rolls back.

## Handle errors

Ledger errors are raised as `django_pledge.LedgerError` subclasses:

```python
import django_pledge

try:
    alice.transfer(shop, amount)
except django_pledge.NegativeBalanceNotAllowed as e:
    e.account_id  # the account without enough money
except django_pledge.LedgerError as e:
    e.code, e.message, e.detail
```

Each [error code](../sql/errors.md) has a class: `AccountClosed`, `AccountNotEmpty`,
`CurrencyMismatch`, `TransferConflict` and so on. The ids and values involved are typed
attributes, like `e.account_id`, `e.transfer_id` or `e.balance`. The full list is in the
[reference](../reference.md#errors).

Every write runs in its own savepoint, so a refused transfer doesn't break your surrounding
transaction.

## What you can't do

Ledger rows can't be changed or deleted. These raise `ReadOnlyError`:

- `save()` on an existing row, `delete()`
- `QuerySet.update()`, `QuerySet.delete()`, `bulk_update()`
- `get_or_create()`, `update_or_create()`

To undo a transfer, book one the other way.

## Tests

Check the books at the end of a test:

```python
Account.objects.assert_verified()   # raises if anything is off
```
