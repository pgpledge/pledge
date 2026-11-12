---
title: Your own models
description: Link your models to ledger accounts, add behaviour with a proxy model, and browse the ledger in the admin.
---

Ledger rows can't change. So keep your own data, like the customer or the wallet type, in your
own models, and point them at the ledger account.

:::tip[Already have an Account model?]
pledge's tables live in their own `pledge` schema and the app label is `pledge`, so nothing
clashes. Import with an alias if the names collide in your code:

```python
from django_pledge.models import Account as LedgerAccount
```
:::

## Link with a OneToOneField

```python
from django.db import models

class Wallet(models.Model):
    customer = models.OneToOneField(Customer, on_delete=models.PROTECT, related_name="wallet")
    account = models.OneToOneField(
        "pledge.Account", on_delete=models.PROTECT, db_constraint=False, related_name="wallet"
    )
```

Now you can go both ways, and filter on ledger fields:

```python
wallet.account.balance
account.wallet.customer
Wallet.objects.filter(account__currency="EUR", account__balance__gt=0)
```

:::caution[Always set db_constraint=False]
The pledge models sit on database views, and a view can't be the target of a foreign key
constraint. Django still joins and filters as usual.
:::

Use a `ForeignKey` instead when several of your rows share one account.

Create both in one transaction:

```python
from django.db import transaction

with transaction.atomic():
    customer = Customer.objects.create(name="Alice", email="alice@example.com")
    account = Account.objects.create(
        name=f"wallet:{customer.email}", currency="EUR", allow_negative_balance=False
    )
    Wallet.objects.create(customer=customer, account=account)
```

## Add methods with a proxy model

Want methods on the account itself? Use a proxy model:

```python
from django_pledge.models import Account

class WalletAccount(Account):
    class Meta:
        proxy = True

    def can_pay(self, amount):
        return not self.is_closed and self.balance >= amount
```

```python
WalletAccount.objects.get(pk=wallet.account_id).can_pay(Decimal("20"))
```

:::danger[No multi-table inheritance]
`class CustomerAccount(Account)` without `proxy = True` is refused with `ImproperlyConfigured`.
Use a OneToOneField for extra data, and a proxy for extra methods.
:::

## Store ids in your own tables

These fields store pledge ids with the right column type:

| Field             | Holds                                           |
| ----------------- | ----------------------------------------------- |
| `AccountIdField`  | an account id (`pla_…`)                         |
| `TransferIdField` | a transfer id (`plt_…`), handy for safe retries |
| `EntryIdField`    | an entry id (`ple_…`)                           |
| `NumericField`    | an amount, as unbounded `numeric` like pledge's |

```python
from django_pledge import TransferIdField
```

## Admin

`ReadOnlyAdmin` shows ledger rows in the admin without add, change or delete:

```python
from django.contrib import admin
from django_pledge.admin import ReadOnlyAdmin
from django_pledge.models import Account

@admin.register(Account)
class AccountAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ["id", "name", "currency", "balance", "closed_at"]
```
