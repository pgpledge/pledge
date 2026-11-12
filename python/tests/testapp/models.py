from django.db import models

from django_pledge import AccountIdField


class Wallet(models.Model):
    label = models.TextField()
    account = models.ForeignKey(
        "pledge.Account", models.PROTECT, db_constraint=False, related_name="wallets"
    )
    other_id = AccountIdField(null=True, blank=True)
