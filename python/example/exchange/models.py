import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import models

from django_pledge import NumericField, TransferId, TransferIdField
from django_pledge.models import Account, Transfer


class Trader(models.Model):
    """A customer of the exchange; not a Django auth user, which is kept for the admin site."""

    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


def luhn_check_digit(digits: str) -> str:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char) * (2 if position % 2 == 0 else 1)
        total += value - 9 if value > 9 else value
    return str(-total % 10)


def new_account_number() -> str:
    """Ten digits, the last a Luhn check digit, so a mistyped number is caught before lookup."""
    body = str(secrets.randbelow(9 * 10**8) + 10**8)
    return body + luhn_check_digit(body)


def parse_account_number(text: str) -> str:
    digits = "".join(char for char in text if char not in " -")
    if len(digits) != 10 or not digits.isdigit() or luhn_check_digit(digits[:9]) != digits[9]:
        raise ValueError("that is not a valid account number; check it for typos")
    return digits


class Wallet(models.Model):
    """A trader's ledger account in one currency."""

    trader = models.ForeignKey(Trader, models.PROTECT, related_name="wallets")
    currency = models.CharField(max_length=5)
    account = models.OneToOneField(
        "pledge.Account", models.PROTECT, db_constraint=False, related_name="wallet"
    )
    # What traders see and type; the ledger's own id stays internal.
    number = models.CharField(
        max_length=10, unique=True, default=new_account_number, editable=False
    )
    opened_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["trader", "currency"], name="one_wallet_per_currency")
        ]
        ordering = ["trader_id", "currency"]

    def __str__(self) -> str:
        return f"{self.trader} {self.currency}"

    def ledger_account(self) -> "WalletAccount":
        return WalletAccount.objects.get(pk=self.account_id)


class HouseAccount(models.Model):
    """The exchange's own ledger accounts, one of each kind per currency."""

    class Kind(models.TextChoices):
        LIQUIDITY = "liquidity", "Liquidity (the exchange's inventory)"
        FEES = "fees", "Fee revenue"
        CARD = "card", "Card provider (not yet paid out)"
        BANK = "bank", "Our bank account"
        CARD_COSTS = "card_costs", "Card processing costs"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    currency = models.CharField(max_length=5)
    account = models.OneToOneField(
        "pledge.Account", models.PROTECT, db_constraint=False, related_name="house_account"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "currency"], name="one_house_account_per_kind")
        ]
        ordering = ["currency", "kind"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.currency}"


class Status(models.TextChoices):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class Trade(models.Model):
    """A conversion at a quoted rate: the trader sells one currency and buys another."""

    trader = models.ForeignKey(Trader, models.PROTECT, related_name="trades")
    # Chosen by the client, so a retried request finds this trade instead of making a new one.
    request_id = models.CharField(max_length=64)
    sell_currency = models.CharField(max_length=5)
    buy_currency = models.CharField(max_length=5)
    sell_amount = NumericField()
    fee = NumericField()
    rate = NumericField()
    buy_amount = NumericField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    failure = models.CharField(max_length=40, blank=True)
    # Stored before pledge is called, so every retry sends the same transfer ids.
    sell_transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
    fee_transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
    buy_transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["trader", "request_id"], name="unique_trade_request")
        ]

    def __str__(self) -> str:
        return f"trade {self.pk}: {self.sell_amount} {self.sell_currency} -> {self.buy_currency}"

    @property
    def net_amount(self) -> Decimal:
        return self.sell_amount - self.fee

    def transfer_ids(self) -> list[TransferId]:
        return [self.sell_transfer_id, self.fee_transfer_id, self.buy_transfer_id]

    def ledger_transfers(self) -> models.QuerySet[Transfer]:
        return Transfer.objects.filter(pk__in=self.transfer_ids())


class Withdrawal(models.Model):
    trader = models.ForeignKey(Trader, models.PROTECT, related_name="withdrawals")
    request_id = models.CharField(max_length=64)
    currency = models.CharField(max_length=5)
    amount = NumericField()
    method = models.CharField(max_length=10, default="bank")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    failure = models.CharField(max_length=40, blank=True)
    transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trader", "request_id"], name="unique_withdrawal_request"
            )
        ]

    def __str__(self) -> str:
        return f"withdrawal {self.pk}: {self.amount} {self.currency}"


@dataclass(frozen=True)
class StatementLine:
    at: datetime
    description: str
    amount: Decimal
    balance: Decimal
    transfer_id: TransferId


class WalletAccount(Account):
    class Meta:
        proxy = True

    def can_spend(self, amount: Decimal) -> bool:
        return not self.is_closed and self.balance >= amount

    def statement(self) -> list[StatementLine]:
        entries = self.history().select_related("transfer")
        return [
            StatementLine(
                at=entry.created_at,
                description=describe_transfer(entry.transfer),
                amount=entry.amount,
                balance=entry.account_current_balance,
                transfer_id=entry.transfer_id,
            )
            for entry in entries
        ]


def payout_note(metadata: dict[str, str]) -> str:
    if metadata.get("method") == "card":
        return f" to card: you receive {metadata['received']}, card fee {metadata['card_fee']}"
    return " to bank account"


FIXED_DESCRIPTIONS = {
    "card_fee": "card processing fee",
    "card_payout": "card provider payout",
    "card_settlement": "settlement with the card provider",
}


def describe_transfer(transfer: Transfer) -> str:
    metadata = transfer.metadata or {}
    kind = metadata.get("kind", "transfer")
    match kind:
        case "trade":
            return f"trade {metadata['trade']} ({metadata['leg']})"
        case "deposit":
            by = "card" if metadata.get("method") == "card" else "bank transfer"
            return f"deposit by {by}"
        case "withdrawal":
            return f"withdrawal {metadata['withdrawal']}{payout_note(metadata)}"
        case "closing payout":
            return f"closing payout{payout_note(metadata)}"
        case "payment":
            return f"payment {metadata['from']} → {metadata['to']}"
    return FIXED_DESCRIPTIONS.get(kind, kind)
