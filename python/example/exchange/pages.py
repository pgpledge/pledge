from collections.abc import Callable
from decimal import Decimal
from functools import wraps
from typing import Any
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

import django_pledge as pledge
from django_pledge import TransferId
from django_pledge.models import Account, Entry, Transfer

from . import services
from .models import HouseAccount, Trade, Trader, WalletAccount, Withdrawal, describe_transfer
from .templatetags.exchange_format import account_number, plain


def new_request_id() -> str:
    # A transfer id also works as a trade or withdrawal request id, so one kind serves all forms.
    return str(TransferId.new())


def currency_choices() -> list[tuple[str, str]]:
    return [(currency, currency) for currency in services.currencies()]


class TraderForm(forms.Form):
    request_id = forms.CharField(max_length=64)
    name = forms.CharField(max_length=100)


class DepositForm(forms.Form):
    request_id = forms.CharField(max_length=64)
    currency = forms.ChoiceField(choices=currency_choices)
    amount = forms.DecimalField()
    method = forms.ChoiceField(choices=list(services.DEPOSIT_METHODS.items()))


class SendForm(forms.Form):
    request_id = forms.CharField(max_length=64)
    to = forms.CharField(max_length=40)
    amount = forms.DecimalField()


class TradeForm(forms.Form):
    request_id = forms.CharField(max_length=64)
    sell = forms.ChoiceField(choices=currency_choices)
    buy = forms.ChoiceField(choices=currency_choices)
    amount = forms.DecimalField()


class WithdrawForm(forms.Form):
    request_id = forms.CharField(max_length=64)
    currency = forms.ChoiceField(choices=currency_choices)
    amount = forms.DecimalField()
    method = forms.ChoiceField(choices=list(services.PAYOUT_METHODS.items()))


class CloseForm(forms.Form):
    currency = forms.ChoiceField(choices=currency_choices)
    method = forms.ChoiceField(choices=list(services.PAYOUT_METHODS.items()))


def payout_message(transfer: Transfer) -> str:
    metadata = transfer.metadata or {}
    if metadata.get("method") == "card":
        return (
            f"to your card: you receive {metadata['received']},"
            f" the card provider keeps {metadata['card_fee']}"
        )
    return "to your bank account"


def cleaned(form_class: type[forms.Form], data: Any) -> dict[str, Any]:
    form = form_class(data)
    if not form.is_valid():
        problems = [
            f"{form[field].label}: {' '.join(errors)}" for field, errors in form.errors.items()
        ]
        raise ValueError(" ".join(problems))
    return form.cleaned_data


RESUBMITTED = "This form was already used with other values. Reload the page and try again."


def explain_rejection(record: Trade | Withdrawal) -> str:
    if isinstance(record, Trade):
        what, paying, receiving = "Trade", record.sell_currency, record.buy_currency
    else:
        what, paying, receiving = "Withdrawal", record.currency, record.currency
    reasons = {
        "insufficient_funds": f"not enough {paying} in the wallet",
        "insufficient_liquidity": f"the exchange does not hold enough {receiving} right now",
        "account_closed": "a wallet involved is closed",
        "insufficient_bank_balance": f"the broker's {paying} bank account is short right now",
    }
    return f"{what} refused: {reasons.get(record.failure, record.failure)}."


def explain(exc: Exception) -> str:
    """A readable sentence for every error a form can run into."""
    if isinstance(exc, services.Rejected):
        return explain_rejection(exc.record)
    if isinstance(exc, (services.RequestConflict, pledge.TransferConflict)):
        return RESUBMITTED
    if isinstance(exc, pledge.AccountAlreadyClosed):
        return "That wallet is already closed."
    if isinstance(exc, pledge.AccountClosed):
        return "That wallet is closed."
    if isinstance(exc, pledge.AccountNotEmpty):
        return "Money arrived while closing the wallet. Try again."
    if isinstance(exc, pledge.NegativeBalanceNotAllowed):
        return "Not enough funds."
    if isinstance(exc, pledge.LedgerError):
        return exc.message
    if isinstance(exc, ObjectDoesNotExist):
        return "That wallet does not exist."
    message = str(exc)
    return message[:1].upper() + message[1:]


def ledger_action(
    action: Callable[[HttpRequest, Trader], None],
) -> Callable[..., HttpResponse]:
    """A POST that changes the ledger, then redirects back to the trader page with a message."""

    @require_POST
    @wraps(action)
    def view(request: HttpRequest, trader_id: int) -> HttpResponse:
        trader = get_object_or_404(Trader, pk=trader_id)
        try:
            action(request, trader)
        except (
            ValueError,
            ObjectDoesNotExist,
            services.Rejected,
            services.RequestConflict,
            pledge.LedgerError,
        ) as exc:
            messages.error(request, explain(exc))
        return redirect("pages:trader", trader.pk)

    return view


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    checks = Account.objects.verify()
    context = {
        "traders": Trader.objects.order_by("-created_at")[:5],
        "trader_count": Trader.objects.count(),
        "totals": services.ledger_totals(),
        "failed_checks": [check for check in checks if not check.ok],
        "check_count": len(checks),
        "request_id": new_request_id(),
    }
    return render(request, "exchange/home.html", context)


@require_GET
def broker(request: HttpRequest) -> HttpResponse:
    houses = HouseAccount.objects.select_related("account")
    sections = []
    for currency in services.currencies():
        accounts = [house for house in houses if house.currency == currency]
        entries = (
            Entry.objects.filter(account_id__in=[house.account_id for house in accounts])
            .select_related("transfer")
            .order_by("-created_at", "-account_version")[:15]
        )
        names = {house.account_id: house.get_kind_display() for house in accounts}
        sections.append(
            {
                "currency": currency,
                "accounts": accounts,
                # The card account goes negative by what the provider holds for us.
                "card_holds": -sum(
                    (h.account.balance for h in accounts if h.kind == HouseAccount.Kind.CARD),
                    Decimal(0),
                ),
                "movements": [
                    {
                        "at": entry.created_at,
                        "account": names[entry.account_id],
                        "description": describe_transfer(entry.transfer),
                        "amount": entry.amount,
                        "balance": entry.account_current_balance,
                    }
                    for entry in entries
                ],
            }
        )
    context = {
        "sections": sections,
        "crypto": settings.EXCHANGE_CRYPTO,
        "card_fee_percent": plain((Decimal(settings.EXCHANGE_CARD_FEE_RATE) * 100).normalize()),
    }
    return render(request, "exchange/broker.html", context)


@require_POST
def payout(request: HttpRequest) -> HttpResponse:
    chosen = request.POST.get("currency")
    if chosen and chosen not in services.currencies():
        messages.error(request, f"unknown currency {chosen!r}")
        return redirect("pages:broker")
    selected = [chosen] if chosen else services.currencies()
    paid = {c: transfer for c in selected if (transfer := services.payout(c))}
    if paid:
        amounts = ", ".join(f"{plain(t.amount)} {c}" for c, t in paid.items())
        messages.success(request, f"The card provider paid out {amounts} to our bank account.")
    else:
        messages.info(request, "The card provider holds nothing to pay out.")
    return redirect("pages:broker")


@require_GET
def traders(request: HttpRequest) -> HttpResponse:
    traders = Trader.objects.annotate(wallet_count=Count("wallets")).order_by("name")
    return render(request, "exchange/traders.html", {"traders": traders})


@require_http_methods(["GET", "POST"])
def create_trader(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        context = {"request_id": new_request_id()}
        return render(request, "exchange/new_trader.html", context)
    try:
        data = cleaned(TraderForm, request.POST)
    except ValueError as exc:
        messages.error(request, explain(exc))
        return redirect("pages:create-trader")
    # The session remembers which request created which trader, so a double submit is harmless.
    created: dict[str, int] = request.session.get("created_traders", {})
    if trader_id := created.get(data["request_id"]):
        messages.info(request, "This trader was already created.")
        return redirect("pages:trader", trader_id)
    try:
        trader = services.sign_up(data["name"], wallet_currencies=["EUR"])
    except ValueError as exc:
        messages.error(request, explain(exc))
        return redirect("pages:create-trader")
    request.session["created_traders"] = {**created, data["request_id"]: trader.pk}
    messages.success(request, f"Welcome, {trader.name}. Your EUR account is open.")
    return redirect("pages:trader", trader.pk)


def wallet_overview(trader: Trader) -> list[dict[str, Any]]:
    accounts = WalletAccount.objects.filter(wallet__trader=trader).select_related("wallet")
    return [
        {
            "currency": account.currency,
            "number": account.wallet.number,
            "account": account,
            "statement": account.statement(),
        }
        for account in sorted(accounts, key=lambda account: account.currency)
    ]


def trade_preview(params: Any) -> tuple[services.Quote | None, str]:
    if not params.get("amount"):
        return None, ""
    try:
        data = cleaned(TradeForm, {**params.dict(), "request_id": "preview"})
        return services.quote(data["sell"], data["buy"], data["amount"]), ""
    except ValueError as exc:
        return None, explain(exc)


@require_POST
def preview_trade(request: HttpRequest, trader_id: int) -> HttpResponse:
    # Redirect so the page URL carries only the trade inputs, not the CSRF token.
    query = urlencode({key: request.POST.get(key, "") for key in ("sell", "buy", "amount")})
    return redirect(f"{reverse('pages:trader', args=[trader_id])}?{query}#trade")


@require_POST
def add_account(request: HttpRequest, trader_id: int) -> HttpResponse:
    trader = get_object_or_404(Trader, pk=trader_id)
    currency = request.POST.get("currency", "")
    try:
        wallet = services.open_wallet(trader, currency)
    except ValueError as exc:
        messages.error(request, explain(exc))
    else:
        number = account_number(wallet.number)
        messages.success(request, f"Your {currency} wallet is open: account {number}.")
    return redirect("pages:trader", trader.pk)


@require_GET
def trader_page(request: HttpRequest, trader_id: int) -> HttpResponse:
    trader = get_object_or_404(Trader, pk=trader_id)
    preview, preview_error = trade_preview(request.GET)
    wallets = wallet_overview(trader)
    context = {
        "trader": trader,
        "wallets": wallets,
        "currencies": [wallet["currency"] for wallet in wallets],
        "fiat_currencies": services.fiat_currencies(),
        "deposit_methods": services.DEPOSIT_METHODS.items(),
        "fee_percent": plain((Decimal(settings.EXCHANGE_FEE_RATE) * 100).normalize()),
        "card_fee_percent": plain((Decimal(settings.EXCHANGE_CARD_FEE_RATE) * 100).normalize()),
        "missing_currencies": [
            currency
            for currency in services.currencies()
            if currency not in {wallet["currency"] for wallet in wallets}
        ],
        "trades": trader.trades.order_by("-created_at")[:10],
        "withdrawals": trader.withdrawals.order_by("-created_at")[:10],
        "preview": preview,
        "preview_error": preview_error,
        "trade_input": {
            "sell": request.GET.get("sell", "EUR"),
            # default to a wallet the trader has that differs from the sell side
            "buy": request.GET.get("buy")
            or next((w["currency"] for w in wallets if w["currency"] != "EUR"), "BTC"),
            "amount": request.GET.get("amount", ""),
        },
        "request_id": new_request_id(),
    }
    return render(request, "exchange/trader.html", context)


@ledger_action
def deposit(request: HttpRequest, trader: Trader) -> None:
    data = cleaned(DepositForm, request.POST)
    transfer_id = TransferId(data["request_id"])
    seen = Transfer.objects.filter(pk=transfer_id).exists()
    currency, method = data["currency"], data["method"]
    transfer = services.deposit(trader, currency, data["amount"], method, transfer_id=transfer_id)
    if seen:
        messages.info(request, "This deposit was already booked; nothing changed.")
    elif method == "card":
        fee = services.card_fee(currency, transfer.amount)
        messages.success(
            request,
            f"Deposited {plain(transfer.amount)} {currency} by card;"
            f" the broker pays {plain(fee)} {currency} card fee.",
        )
    else:
        messages.success(request, f"Deposited {plain(transfer.amount)} {currency}.")


@ledger_action
def send(request: HttpRequest, trader: Trader) -> None:
    data = cleaned(SendForm, request.POST)
    # The form carries a fresh transfer id, so pledge itself makes a double submit harmless.
    transfer_id = TransferId(data["request_id"])
    seen = Transfer.objects.filter(pk=transfer_id).exists()
    transfer = services.send(trader, data["to"].strip(), data["amount"], transfer_id=transfer_id)
    if seen:
        messages.info(request, "This payment was already sent; nothing changed.")
    else:
        recipient = transfer.metadata["to"]
        messages.success(request, f"Sent {plain(transfer.amount)} to {recipient}.")


@ledger_action
def trade(request: HttpRequest, trader: Trader) -> None:
    data = cleaned(TradeForm, request.POST)
    seen = trader.trades.filter(request_id=data["request_id"]).exists()
    placed, _ = services.place_trade(
        trader, data["request_id"], data["sell"], data["buy"], data["amount"]
    )
    if seen:
        messages.info(request, f"Trade {placed.pk} was already booked; nothing changed.")
        return
    messages.success(
        request,
        f"Sold {plain(placed.sell_amount)} {placed.sell_currency}"
        f" (fee {plain(placed.fee)}) for {plain(placed.buy_amount)} {placed.buy_currency}.",
    )


@ledger_action
def withdraw(request: HttpRequest, trader: Trader) -> None:
    data = cleaned(WithdrawForm, request.POST)
    seen = trader.withdrawals.filter(request_id=data["request_id"]).exists()
    withdrawal, transfer = services.request_withdrawal(
        trader, data["request_id"], data["currency"], data["amount"], data["method"]
    )
    if seen:
        messages.info(request, f"Withdrawal {withdrawal.pk} was already paid; nothing changed.")
    else:
        messages.success(
            request,
            f"Withdrew {plain(withdrawal.amount)} {withdrawal.currency}"
            f" {payout_message(transfer)}.",
        )


@ledger_action
def close_wallet(request: HttpRequest, trader: Trader) -> None:
    data = cleaned(CloseForm, request.POST)
    currency = data["currency"]
    payout = services.close_wallet(trader, currency, data["method"])
    paid = ""
    if payout:
        paid = f" and paid out {plain(payout.amount)} {currency} {payout_message(payout)}"
    messages.success(request, f"Closed the {currency} wallet{paid}.")
