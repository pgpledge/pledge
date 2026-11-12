import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

import django_pledge as pledge
from django_pledge.json import LedgerJSONEncoder
from django_pledge.models import Transfer

from . import services
from .models import Trade, Trader, Withdrawal


def respond(data: dict[str, Any], status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status, encoder=LedgerJSONEncoder)


class BadRequest(Exception):
    pass


def json_api(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    # A JSON API for non-browser clients: it uses no cookies, so CSRF tokens don't apply.
    @csrf_exempt
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            return view(request, *args, **kwargs)
        except (BadRequest, ValueError) as exc:
            return respond({"error": "bad_request", "message": str(exc)}, status=400)
        except ObjectDoesNotExist as exc:
            return respond({"error": "not_found", "message": str(exc)}, status=404)
        except services.RequestConflict as exc:
            return respond({"error": "request_conflict", "message": str(exc)}, status=409)
        except services.Rejected as exc:
            return respond({"error": exc.record.failure, **record_json(exc.record)}, status=409)
        except pledge.NegativeBalanceNotAllowed:
            return respond({"error": "insufficient_funds"}, status=409)
        except pledge.LedgerError as exc:
            return respond(
                {"error": exc.code, "message": exc.message, "detail": exc.detail}, status=409
            )

    return wrapper


def read_body(request: HttpRequest) -> dict[str, Any]:
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        raise BadRequest("body must be JSON") from exc
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    return body


def parse_amount(value: Any) -> Decimal:
    # Amounts travel as strings so no float rounding happens on the way in.
    if not isinstance(value, str):
        raise BadRequest("amount must be a decimal string such as '10.00'")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise BadRequest(f"invalid amount {value!r}") from exc


def parse_request_id(body: dict[str, Any]) -> str:
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 64:
        raise BadRequest("request_id must be a string of 1 to 64 characters")
    return request_id


def transfer_json(transfer: Transfer) -> dict[str, Any]:
    return {
        "id": str(transfer.id),
        "from": str(transfer.from_account_id),
        "to": str(transfer.to_account_id),
        "amount": transfer.amount,
        "metadata": transfer.metadata,
        "created_at": transfer.created_at,
    }


def record_json(record: Trade | Withdrawal) -> dict[str, Any]:
    if isinstance(record, Trade):
        return {"trade": trade_json(record)}
    return {"withdrawal": withdrawal_json(record)}


def trade_json(trade: Trade) -> dict[str, Any]:
    return {
        "id": trade.pk,
        "request_id": trade.request_id,
        "sell": trade.sell_currency,
        "buy": trade.buy_currency,
        "sell_amount": trade.sell_amount,
        "fee": trade.fee,
        "rate": trade.rate,
        "buy_amount": trade.buy_amount,
        "status": trade.status,
        "failure": trade.failure,
    }


def withdrawal_json(withdrawal: Withdrawal) -> dict[str, Any]:
    return {
        "id": withdrawal.pk,
        "request_id": withdrawal.request_id,
        "currency": withdrawal.currency,
        "amount": withdrawal.amount,
        "method": withdrawal.method,
        "status": withdrawal.status,
        "failure": withdrawal.failure,
    }


def trader_json(trader: Trader) -> dict[str, Any]:
    return {"trader": trader.pk, "name": trader.name, "created_at": trader.created_at}


def portfolio_json(trader: Trader) -> dict[str, Any]:
    holdings = services.portfolio(trader)
    return {
        "trader": trader.pk,
        "name": trader.name,
        "balances": {h.currency: h.balance for h in holdings},
        "closed": [h.currency for h in holdings if h.closed],
        "value_eur": sum((h.value_eur for h in holdings), Decimal("0.00")),
    }


def get_trader(trader_id: int) -> Trader:
    return get_object_or_404(Trader, pk=trader_id)


@require_http_methods(["GET", "POST"])
@json_api
def traders(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return respond({"traders": [trader_json(t) for t in Trader.objects.all()]})
    name = read_body(request).get("name")
    if not isinstance(name, str):
        raise BadRequest("name is required")
    return respond(portfolio_json(services.sign_up(name)), status=201)


@require_GET
@json_api
def portfolio(request: HttpRequest, trader_id: int) -> HttpResponse:
    return respond(portfolio_json(get_trader(trader_id)))


@require_POST
@json_api
def deposit(request: HttpRequest, trader_id: int) -> HttpResponse:
    body = read_body(request)
    raw_transfer_id = body.get("transfer_id")
    if raw_transfer_id is not None and not isinstance(raw_transfer_id, str):
        raise BadRequest("transfer_id must be a plt_ id string")
    transfer_id = pledge.TransferId(raw_transfer_id) if raw_transfer_id else None
    transfer = services.deposit(
        get_trader(trader_id),
        str(body.get("currency")),
        parse_amount(body.get("amount")),
        str(body.get("method", "bank")),
        transfer_id=transfer_id,
    )
    return respond({"transfer": transfer_json(transfer)}, status=201)


@require_GET
@json_api
def quote(request: HttpRequest) -> HttpResponse:
    priced = services.quote(
        request.GET.get("sell", ""),
        request.GET.get("buy", ""),
        parse_amount(request.GET.get("amount")),
    )
    return respond(
        {
            "sell": priced.sell_currency,
            "buy": priced.buy_currency,
            "sell_amount": priced.sell_amount,
            "fee": priced.fee,
            "rate": priced.rate,
            "buy_amount": priced.buy_amount,
        }
    )


@require_POST
@json_api
def trade(request: HttpRequest, trader_id: int) -> HttpResponse:
    body = read_body(request)
    request_id = parse_request_id(body)
    trader = get_trader(trader_id)
    first_attempt = not Trade.objects.filter(trader=trader, request_id=request_id).exists()
    placed, transfers = services.place_trade(
        trader,
        request_id,
        str(body.get("sell")),
        str(body.get("buy")),
        parse_amount(body.get("amount")),
    )
    return respond(
        {"trade": trade_json(placed), "transfers": [transfer_json(t) for t in transfers]},
        status=201 if first_attempt else 200,
    )


@require_POST
@json_api
def withdraw(request: HttpRequest, trader_id: int) -> HttpResponse:
    body = read_body(request)
    request_id = parse_request_id(body)
    trader = get_trader(trader_id)
    first_attempt = not Withdrawal.objects.filter(trader=trader, request_id=request_id).exists()
    withdrawal, transfer = services.request_withdrawal(
        trader,
        request_id,
        str(body.get("currency")),
        parse_amount(body.get("amount")),
        str(body.get("method", "bank")),
    )
    return respond(
        {"withdrawal": withdrawal_json(withdrawal), "transfer": transfer_json(transfer)},
        status=201 if first_attempt else 200,
    )


@require_GET
@json_api
def statement(request: HttpRequest, trader_id: int, currency: str) -> HttpResponse:
    account = services.wallet(get_trader(trader_id), currency).ledger_account()
    lines = [
        {
            "at": line.at,
            "description": line.description,
            "amount": line.amount,
            "balance": line.balance,
            "transfer": str(line.transfer_id),
        }
        for line in account.statement()
    ]
    return respond(
        {
            "currency": currency,
            "account": str(account.id),
            "balance": account.balance,
            "lines": lines,
        }
    )


@require_POST
@json_api
def close_wallet(request: HttpRequest, trader_id: int, currency: str) -> HttpResponse:
    trader = get_trader(trader_id)
    payout = services.close_wallet(trader, currency)
    return respond(
        {
            "portfolio": portfolio_json(trader),
            "payout": transfer_json(payout) if payout else None,
        }
    )


@require_GET
@json_api
def house(request: HttpRequest) -> HttpResponse:
    return respond({"totals": services.ledger_totals()})


@require_POST
@json_api
def payout(request: HttpRequest) -> HttpResponse:
    currency = read_body(request).get("currency")
    chosen = services.currencies() if currency is None else [str(currency)]
    if not set(chosen) <= set(services.currencies()):
        raise BadRequest(f"unknown currency {currency!r}")
    payouts = [transfer for c in chosen if (transfer := services.payout(c))]
    return respond({"payouts": [transfer_json(transfer) for transfer in payouts]})
