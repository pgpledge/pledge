import uuid
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum

import django_pledge as pledge
from django_pledge import TransferId
from django_pledge.models import Account, Transfer

from .models import (
    HouseAccount,
    Status,
    Trade,
    Trader,
    Wallet,
    Withdrawal,
    parse_account_number,
)

DEPOSIT_METHODS = {"bank": "Bank transfer", "card": "Card"}
# Money outside the ledger: these balance everything inside it, so they never go above zero.
EXTERNAL_KINDS = {HouseAccount.Kind.CARD, HouseAccount.Kind.BANK, HouseAccount.Kind.CARD_COSTS}
# Money paid out to cards can exceed what the provider holds for us: then we owe the provider.
MAY_GO_POSITIVE = {HouseAccount.Kind.CARD}
PAYOUT_METHODS = {"bank": "Bank account (free)", "card": "Card"}


class RequestConflict(Exception):
    """A request id was reused with different parameters."""


class Rejected(Exception):
    """The ledger refused a trade or withdrawal; the row records why."""

    def __init__(self, record: Trade | Withdrawal) -> None:
        super().__init__(f"{record} failed: {record.failure}")
        self.record = record


def currencies() -> list[str]:
    return list(settings.EXCHANGE_CURRENCIES)


def fiat_currencies() -> list[str]:
    return [c for c in currencies() if c not in settings.EXCHANGE_CRYPTO]


def check_fiat(currency: str) -> None:
    """Banks and cards move fiat money only; crypto is bought and sold on the exchange."""
    if currency in settings.EXCHANGE_CRYPTO:
        others = " or ".join(fiat_currencies())
        raise ValueError(f"{currency} can't move by bank or card; trade it for {others}")


def smallest_unit(currency: str) -> Decimal:
    try:
        places = settings.EXCHANGE_CURRENCIES[currency]
    except KeyError:
        raise ValueError(f"unsupported currency {currency!r}") from None
    return Decimal(1).scaleb(-places)


def check_amount(currency: str, amount: Decimal) -> Decimal:
    unit = smallest_unit(currency)
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(unit):
        raise ValueError(f"invalid {currency} amount {amount}")
    return amount.quantize(unit)


def house_account(kind: str, currency: str) -> Account:
    """One of the broker's own accounts for ``currency``, created on first use."""
    existing = HouseAccount.objects.select_related("account").filter(kind=kind, currency=currency)
    if found := existing.first():
        return found.account
    try:
        with transaction.atomic():
            is_external = kind in EXTERNAL_KINDS
            account = Account.objects.create(
                name=f"house:{kind}:{currency}",
                currency=currency,
                allow_negative_balance=is_external,
                allow_positive_balance=not is_external or kind in MAY_GO_POSITIVE,
                metadata={"house": kind},
            )
            HouseAccount.objects.create(kind=kind, currency=currency, account=account)
            return account
    except IntegrityError:
        # Another request created it first; our ledger account was rolled back with the row.
        return existing.get().account


def fund_liquidity(currency: str, amount: Decimal) -> Transfer:
    """The owner's capital arrives in our bank account and becomes the exchange's inventory."""
    amount = check_amount(currency, amount)
    bank = house_account(HouseAccount.Kind.BANK, currency)
    liquidity = house_account(HouseAccount.Kind.LIQUIDITY, currency)
    return bank.transfer(liquidity, amount, metadata={"kind": "funding"})


def sign_up(name: str, wallet_currencies: list[str] | None = None) -> Trader:
    """Create a trader with an empty wallet per currency; all currencies unless given."""
    name = name.strip()
    if not 0 < len(name) <= 100:
        raise ValueError("name must be 1 to 100 characters")
    try:
        with transaction.atomic():
            trader = Trader.objects.create(name=name)
            for currency in currencies() if wallet_currencies is None else wallet_currencies:
                open_wallet(trader, currency)
    except IntegrityError:
        raise ValueError(f"name {name!r} is taken") from None
    return trader


def open_wallet(trader: Trader, currency: str) -> Wallet:
    """Open the trader's ledger account in one more currency."""
    if currency not in currencies():
        raise ValueError(f"unknown currency {currency!r}")
    if trader.wallets.filter(currency=currency).exists():
        raise ValueError(f"{trader.name} already has a {currency} account")
    with transaction.atomic():
        account = Account.objects.create(
            name=f"trader:{trader.pk}:{currency}",
            currency=currency,
            allow_negative_balance=False,
            metadata={"trader": trader.pk},
        )
        return Wallet.objects.create(trader=trader, currency=currency, account=account)


def wallet(trader: Trader | int, currency: str) -> Wallet:
    return Wallet.objects.select_related("account").get(trader=trader, currency=currency)


def card_fee(currency: str, amount: Decimal) -> Decimal:
    rate = Decimal(settings.EXCHANGE_CARD_FEE_RATE)
    return (amount * rate).quantize(smallest_unit(currency), rounding=ROUND_UP)


def card_fee_id(transfer_id: TransferId) -> TransferId:
    # Derived from the deposit's id, so a retry sends the same fee id. The first 8 bytes keep
    # the deposit's timestamp; the rest come from a hash.
    digest = uuid.uuid5(transfer_id.uuid, "card_fee").bytes
    return TransferId.from_bytes(transfer_id.bytes[:8] + digest[8:])


def deposit(
    trader: Trader | int,
    currency: str,
    amount: Decimal,
    method: str = "bank",
    *,
    transfer_id: TransferId | None = None,
) -> Transfer:
    """Money paid in by bank transfer or card; pass the provider's payment id for safe retries.

    A card payment first sits at the card provider, which keeps a fee the broker pays.
    """
    amount = check_amount(currency, amount)
    check_fiat(currency)
    if method not in DEPOSIT_METHODS:
        raise ValueError(f"unknown deposit method {method!r}")
    account = wallet(trader, currency).account
    metadata = {"kind": "deposit", "method": method}
    if method == "bank":
        bank = house_account(HouseAccount.Kind.BANK, currency)
        return bank.transfer(account, amount, id=transfer_id, metadata=metadata)
    transfer_id = transfer_id or TransferId.new()
    card = house_account(HouseAccount.Kind.CARD, currency)
    legs = [
        Transfer(
            id=transfer_id, from_account=card, to_account=account, amount=amount, metadata=metadata
        )
    ]
    if fee := card_fee(currency, amount):
        legs.append(
            Transfer(
                id=card_fee_id(transfer_id),
                from_account=house_account(HouseAccount.Kind.CARD_COSTS, currency),
                to_account=card,
                amount=fee,
                metadata={"kind": "card_fee"},
            )
        )
    return Transfer.objects.bulk_create(legs)[0]


def payout(currency: str) -> Transfer | None:
    """Settle with the card provider: it pays out what it holds, or collects what we owe it."""
    if currency in settings.EXCHANGE_CRYPTO:
        return None
    house_account(HouseAccount.Kind.CARD, currency)
    bank = house_account(HouseAccount.Kind.BANK, currency)
    with transaction.atomic():
        # Concurrent payouts queue on this row, and each reads the balance only once it holds it.
        house = HouseAccount.objects.select_for_update().get(
            kind=HouseAccount.Kind.CARD, currency=currency
        )
        card = Account.objects.get(pk=house.account_id)
        if not card.balance:
            return None
        if card.balance < 0:
            return bank.transfer(card, -card.balance, metadata={"kind": "card_payout"})
        return card.transfer(bank, card.balance, metadata={"kind": "card_settlement"})


def payout_destination(currency: str, method: str) -> Account:
    """Where money leaving a wallet goes: our bank account, or the card provider."""
    if method not in PAYOUT_METHODS:
        raise ValueError(f"unknown payout method {method!r}")
    check_fiat(currency)
    kind = HouseAccount.Kind.CARD if method == "card" else HouseAccount.Kind.BANK
    return house_account(kind, currency)


def payout_metadata(currency: str, amount: Decimal, method: str) -> dict[str, str]:
    """For card payouts the provider keeps its fee, so the trader receives less."""
    if method != "card":
        return {"method": method}
    fee = card_fee(currency, amount)
    return {"method": method, "card_fee": str(fee), "received": str(amount - fee)}


def send(
    sender: Trader, to_account: str, amount: Decimal, *, transfer_id: TransferId | None = None
) -> Transfer:
    """Pay another trader. The recipient's account number fixes the currency."""
    recipients = Wallet.objects.select_related("trader", "account")
    try:
        recipient = recipients.get(number=parse_account_number(to_account))
    except Wallet.DoesNotExist:
        raise ValueError("no trader account with that number") from None
    if recipient.trader_id == sender.pk:
        raise ValueError("that is your own account; use a trade to convert between currencies")
    amount = check_amount(recipient.currency, amount)
    try:
        source = wallet(sender, recipient.currency).account
    except Wallet.DoesNotExist:
        raise ValueError(f"you have no {recipient.currency} account to pay from") from None
    metadata = {"kind": "payment", "from": sender.name, "to": recipient.trader.name}
    return source.transfer(recipient.account, amount, id=transfer_id, metadata=metadata)


@dataclass(frozen=True)
class Quote:
    sell_currency: str
    buy_currency: str
    sell_amount: Decimal
    fee: Decimal
    rate: Decimal
    buy_amount: Decimal

    @property
    def net_amount(self) -> Decimal:
        return self.sell_amount - self.fee


def quote(sell_currency: str, buy_currency: str, sell_amount: Decimal) -> Quote:
    """Price a conversion: the fee is taken from the sold amount, the rest is converted.

    Both roundings favour the exchange: the fee rounds up and the bought amount rounds down.
    """
    if sell_currency == buy_currency:
        raise ValueError("sell and buy currencies must differ")
    sell_amount = check_amount(sell_currency, sell_amount)
    buy_unit = smallest_unit(buy_currency)
    prices = settings.EXCHANGE_PRICES_EUR
    rate = Decimal(prices[sell_currency]) / Decimal(prices[buy_currency])
    fee_rate = Decimal(settings.EXCHANGE_FEE_RATE)
    fee = (sell_amount * fee_rate).quantize(smallest_unit(sell_currency), rounding=ROUND_UP)
    buy_amount = ((sell_amount - fee) * rate).quantize(buy_unit, rounding=ROUND_DOWN)
    if fee >= sell_amount or buy_amount <= 0:
        raise ValueError(f"{sell_amount} {sell_currency} is too small to trade")
    return Quote(sell_currency, buy_currency, sell_amount, fee, rate, buy_amount)


def place_trade(
    trader: Trader, request_id: str, sell_currency: str, buy_currency: str, sell_amount: Decimal
) -> tuple[Trade, list[Transfer]]:
    """Quote and execute a trade; sending the same ``request_id`` again replays the outcome."""
    priced = quote(sell_currency, buy_currency, sell_amount)
    trade, created = Trade.objects.get_or_create(
        trader=trader,
        request_id=request_id,
        defaults={
            "sell_currency": priced.sell_currency,
            "buy_currency": priced.buy_currency,
            "sell_amount": priced.sell_amount,
            "fee": priced.fee,
            "rate": priced.rate,
            "buy_amount": priced.buy_amount,
        },
    )
    # A retry keeps the original quote even if prices moved since.
    asked = (sell_currency, buy_currency, priced.sell_amount)
    if not created and (trade.sell_currency, trade.buy_currency, trade.sell_amount) != asked:
        raise RequestConflict(f"request {request_id!r} was already used for {trade}")
    return execute_trade(trade.pk)


def trade_legs(trade: Trade, trader_sell: Account, trader_buy: Account) -> list[Transfer]:
    """pledge never converts, so a trade is one transfer per currency plus the fee."""
    sell, buy = trade.sell_currency, trade.buy_currency
    metadata = {"kind": "trade", "trade": trade.pk}
    return [
        Transfer(
            id=trade.sell_transfer_id,
            from_account=trader_sell,
            to_account=house_account(HouseAccount.Kind.LIQUIDITY, sell),
            amount=trade.net_amount,
            metadata={**metadata, "leg": "sell"},
        ),
        Transfer(
            id=trade.fee_transfer_id,
            from_account=trader_sell,
            to_account=house_account(HouseAccount.Kind.FEES, sell),
            amount=trade.fee,
            metadata={**metadata, "leg": "fee"},
        ),
        Transfer(
            id=trade.buy_transfer_id,
            from_account=house_account(HouseAccount.Kind.LIQUIDITY, buy),
            to_account=trader_buy,
            amount=trade.buy_amount,
            metadata={**metadata, "leg": "buy"},
        ),
    ]


def failure_code(exc: pledge.LedgerError, own_accounts: set[pledge.AccountId]) -> str:
    if isinstance(exc, pledge.NegativeBalanceNotAllowed):
        return "insufficient_funds" if exc.account_id in own_accounts else "insufficient_liquidity"
    if isinstance(exc, pledge.PositiveBalanceNotAllowed):
        # Paying out would take more from our bank account than has arrived there.
        return "insufficient_bank_balance"
    return exc.code.removeprefix("pledge.")


def execute_trade(trade_id: int) -> tuple[Trade, list[Transfer]]:
    """Book all legs in one atomic batch, or none of them.

    Safe to call again: a completed trade sends the same transfer ids, so pledge returns the
    original transfers instead of booking twice. A failed trade stays failed.
    """
    transfers: list[Transfer] = []
    with transaction.atomic():
        trade = Trade.objects.select_for_update().get(pk=trade_id)
        if trade.status != Status.FAILED:
            trader_sell = wallet(trade.trader_id, trade.sell_currency).account
            trader_buy = wallet(trade.trader_id, trade.buy_currency).account
            try:
                # Each pledge call runs in a savepoint, so a refusal leaves this transaction usable.
                transfers = Transfer.objects.bulk_create(trade_legs(trade, trader_sell, trader_buy))
            except (pledge.NegativeBalanceNotAllowed, pledge.AccountClosed) as exc:
                trade.status = Status.FAILED
                trade.failure = failure_code(exc, {trader_sell.id, trader_buy.id})
            else:
                trade.status = Status.COMPLETED
            trade.save(update_fields=["status", "failure"])
    if trade.status == Status.FAILED:
        raise Rejected(trade)
    return trade, transfers


def request_withdrawal(
    trader: Trader, request_id: str, currency: str, amount: Decimal, method: str = "bank"
) -> tuple[Withdrawal, Transfer]:
    amount = check_amount(currency, amount)
    payout_destination(currency, method)
    withdrawal, created = Withdrawal.objects.get_or_create(
        trader=trader,
        request_id=request_id,
        defaults={"currency": currency, "amount": amount, "method": method},
    )
    if not created and (withdrawal.currency, withdrawal.amount, withdrawal.method) != (
        currency,
        amount,
        method,
    ):
        raise RequestConflict(f"request {request_id!r} was already used for {withdrawal}")
    return execute_withdrawal(withdrawal.pk)


def execute_withdrawal(withdrawal_id: int) -> tuple[Withdrawal, Transfer]:
    """Pay out from our bank account; retrying is idempotent like ``execute_trade``."""
    with transaction.atomic():
        withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal_id)
        if withdrawal.status != Status.FAILED:
            account = wallet(withdrawal.trader_id, withdrawal.currency).account
            destination = payout_destination(withdrawal.currency, withdrawal.method)
            metadata = {
                "kind": "withdrawal",
                "withdrawal": withdrawal.pk,
                **payout_metadata(withdrawal.currency, withdrawal.amount, withdrawal.method),
            }
            try:
                transfer = account.transfer(
                    destination, withdrawal.amount, id=withdrawal.transfer_id, metadata=metadata
                )
            except (
                pledge.NegativeBalanceNotAllowed,
                pledge.PositiveBalanceNotAllowed,
                pledge.AccountClosed,
            ) as exc:
                withdrawal.status = Status.FAILED
                withdrawal.failure = failure_code(exc, {account.id})
            else:
                withdrawal.status = Status.COMPLETED
            withdrawal.save(update_fields=["status", "failure"])
    if withdrawal.status == Status.FAILED:
        raise Rejected(withdrawal)
    return withdrawal, transfer


def close_wallet(trader: Trader | int, currency: str, method: str = "bank") -> Transfer | None:
    """Pay out any remaining balance and close the account, all or nothing."""
    if method not in PAYOUT_METHODS:
        raise ValueError(f"unknown payout method {method!r}")
    with transaction.atomic():
        account = wallet(trader, currency).account
        payout = None
        if account.balance > 0:
            if currency in settings.EXCHANGE_CRYPTO:
                others = " or ".join(fiat_currencies())
                raise ValueError(
                    f"sell your {format(account.balance, 'f')} {currency} for {others} first, "
                    "then close the wallet"
                )
            destination = payout_destination(currency, method)
            metadata = {
                "kind": "closing payout",
                **payout_metadata(currency, account.balance, method),
            }
            payout = account.transfer(destination, account.balance, metadata=metadata)
        # close() checks the balance under its row lock: a late deposit raises AccountNotEmpty.
        account.close()
    return payout


@dataclass(frozen=True)
class Holding:
    currency: str
    balance: Decimal
    value_eur: Decimal
    closed: bool


def portfolio(trader: Trader | int) -> list[Holding]:
    prices = settings.EXCHANGE_PRICES_EUR
    cent = smallest_unit("EUR")
    return [
        Holding(
            currency=w.currency,
            balance=w.account.balance,
            value_eur=(w.account.balance * Decimal(prices[w.currency])).quantize(cent),
            closed=w.account.is_closed,
        )
        for w in Wallet.objects.filter(trader=trader).select_related("account")
    ]


def ledger_totals() -> dict[str, dict[str, Decimal]]:
    """Per currency: all customers together, each house account, and the ledger sum (always 0)."""
    columns = ["customers", *HouseAccount.Kind.values, "sum"]
    totals = {currency: dict.fromkeys(columns, Decimal(0)) for currency in currencies()}
    for row in HouseAccount.objects.values("currency", "kind", "account__balance"):
        totals[row["currency"]][row["kind"]] = row["account__balance"]
    customers = Wallet.objects.values("currency").annotate(total=Sum("account__balance"))
    for row in customers:
        totals[row["currency"]]["customers"] = row["total"]
    for row in Account.objects.values("currency").annotate(total=Sum("balance")):
        totals.setdefault(row["currency"], dict.fromkeys(columns, Decimal(0)))
        totals[row["currency"]]["sum"] = row["total"]
    return totals
