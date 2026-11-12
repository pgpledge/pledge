from decimal import Decimal

import pytest
from django.urls import reverse
from exchange.models import Status, Trade

from django_pledge import TransferId
from django_pledge.models import Account

pytestmark = pytest.mark.django_db


def post(client, url: str, body: dict | None = None):
    return client.post(url, body or {}, content_type="application/json")


@pytest.fixture
def sign_up(client):
    def sign_up(name: str, **deposits: str) -> dict:
        response = post(client, reverse("exchange:traders"), {"name": f"{name}-{TransferId.new()}"})
        assert response.status_code == 201, response.json()
        trader = response.json()
        for currency, amount in deposits.items():
            url = reverse("exchange:deposit", args=[trader["trader"]])
            response = post(client, url, {"currency": currency, "amount": amount})
            assert response.status_code == 201, response.json()
        return trader

    return sign_up


def balances(client, trader: dict) -> dict[str, Decimal]:
    response = client.get(reverse("exchange:portfolio", args=[trader["trader"]]))
    assert response.status_code == 200
    return {currency: Decimal(value) for currency, value in response.json()["balances"].items()}


def trade(client, trader: dict, request_id: str, sell: str, buy: str, amount: str):
    body = {"request_id": request_id, "sell": sell, "buy": buy, "amount": amount}
    return post(client, reverse("exchange:trade", args=[trader["trader"]]), body)


def withdraw(client, trader: dict, request_id: str, currency: str, amount: str):
    body = {"request_id": request_id, "currency": currency, "amount": amount}
    return post(client, reverse("exchange:withdraw", args=[trader["trader"]]), body)


def test_sign_up_and_portfolio(client, sign_up):
    trader = sign_up("alice")
    assert trader["balances"] == {"BTC": "0", "EUR": "0", "USD": "0"}
    assert trader["closed"] == [] and trader["value_eur"] == "0.00"
    assert Account.objects.filter(wallet__trader=trader["trader"]).count() == 3
    assert client.get(reverse("exchange:portfolio", args=[999_999])).status_code == 404
    taken = post(client, reverse("exchange:traders"), {"name": trader["name"]})
    assert taken.status_code == 400 and "taken" in taken.json()["message"]
    for body in ({}, {"name": ""}, {"name": "   "}, {"name": 5}):
        assert post(client, reverse("exchange:traders"), body).status_code == 400


def test_traders_list_answers_get(client, sign_up):
    trader = sign_up("listed")
    response = client.get(reverse("exchange:traders"))
    assert response.status_code == 200
    listed = {row["trader"]: row["name"] for row in response.json()["traders"]}
    assert listed[trader["trader"]] == trader["name"]


def test_exchange_root_redirects_to_the_home_page(client):
    response = client.get(reverse("exchange:index"))
    assert response.status_code == 302 and response["Location"] == reverse("pages:home")


def test_deposit_view_is_idempotent_with_a_transfer_id(client, sign_up):
    trader = sign_up("alice")
    url = reverse("exchange:deposit", args=[trader["trader"]])
    body = {"currency": "EUR", "amount": "12.50", "transfer_id": str(TransferId.new())}

    first, retry = post(client, url, body), post(client, url, body)

    assert first.status_code == retry.status_code == 201
    assert first.json()["transfer"] == retry.json()["transfer"]
    assert balances(client, trader)["EUR"] == Decimal("12.50")
    conflict = post(client, url, {**body, "amount": "13.00"})
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "pledge.transfer_conflict"
    assert conflict.json()["detail"]["transfer_id"] == body["transfer_id"]


@pytest.mark.parametrize(
    ("currency", "amount"),
    [
        ("EUR", None),
        ("EUR", 5),
        ("EUR", "abc"),
        ("EUR", "-1.00"),
        ("EUR", "0"),
        ("EUR", "1.234"),
        ("BTC", "0.000000001"),
        ("EUR", "NaN"),
        ("EUR", "Infinity"),
        ("XYZ", "1.00"),
    ],
)
def test_deposit_rejects_bad_input(client, sign_up, currency, amount):
    trader = sign_up("alice")
    url = reverse("exchange:deposit", args=[trader["trader"]])
    response = post(client, url, {"currency": currency, "amount": amount})
    assert response.status_code == 400
    assert set(balances(client, trader).values()) == {0}


def test_quote_view(client):
    response = client.get(
        reverse("exchange:quote"), {"sell": "EUR", "buy": "BTC", "amount": "100.00"}
    )
    assert response.status_code == 200
    assert response.json() | {"rate": None} == {
        "sell": "EUR",
        "buy": "BTC",
        "sell_amount": "100.00",
        "fee": "0.25",
        "rate": None,
        "buy_amount": "0.00171982",
    }
    bad = client.get(reverse("exchange:quote"), {"sell": "EUR", "buy": "EUR", "amount": "1.00"})
    assert bad.status_code == 400


def test_trade_and_retry(client, sign_up):
    trader = sign_up("alice", EUR="1000.00")

    first = trade(client, trader, "t-1", "EUR", "BTC", "500.00")
    retry = trade(client, trader, "t-1", "EUR", "BTC", "500.00")

    assert (first.status_code, retry.status_code) == (201, 200)
    assert first.json() == retry.json()
    body = first.json()
    assert body["trade"]["status"] == "completed"
    assert [t["amount"] for t in body["transfers"]] == ["498.75", "1.25", "0.00859913"]
    assert [t["metadata"]["leg"] for t in body["transfers"]] == ["sell", "fee", "buy"]
    assert balances(client, trader) == {
        "BTC": Decimal("0.00859913"),
        "EUR": Decimal("500.00"),
        "USD": 0,
    }
    conflict = trade(client, trader, "t-1", "EUR", "BTC", "400.00")
    assert conflict.status_code == 409 and conflict.json()["error"] == "request_conflict"


def test_trade_without_funds_is_rejected_atomically(client, sign_up):
    trader = sign_up("alice", USD="10.00")

    response = trade(client, trader, "t-1", "USD", "EUR", "10.01")

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "insufficient_funds"
    assert body["trade"]["status"] == "failed"
    assert not Trade.objects.get(pk=body["trade"]["id"]).ledger_transfers().exists()
    assert balances(client, trader) == {"BTC": 0, "EUR": 0, "USD": Decimal("10.00")}
    assert trade(client, trader, "t-1", "USD", "EUR", "10.01").json() == body


@pytest.mark.parametrize(
    "body",
    [
        {"sell": "EUR", "buy": "BTC", "amount": "1.00"},
        {"request_id": "", "sell": "EUR", "buy": "BTC", "amount": "1.00"},
        {"request_id": "x" * 65, "sell": "EUR", "buy": "BTC", "amount": "1.00"},
        {"request_id": "t", "sell": "EUR", "buy": "GBP", "amount": "1.00"},
        {"request_id": "t", "sell": "EUR", "buy": "BTC", "amount": "0.01"},
    ],
)
def test_trade_rejects_bad_requests(client, sign_up, body):
    trader = sign_up("alice", EUR="10.00")
    response = post(client, reverse("exchange:trade", args=[trader["trader"]]), body)
    assert response.status_code == 400
    assert not Trade.objects.filter(trader=trader["trader"]).exists()


def test_failed_withdrawal_then_retry(client, sign_up):
    trader = sign_up("alice", EUR="100.00")

    refused = withdraw(client, trader, "w-1", "EUR", "150.00")
    assert refused.status_code == 409
    assert refused.json()["error"] == "insufficient_funds"
    assert refused.json()["withdrawal"]["status"] == Status.FAILED
    assert withdraw(client, trader, "w-1", "EUR", "150.00").json() == refused.json()

    paid = withdraw(client, trader, "w-2", "EUR", "60.00")
    again = withdraw(client, trader, "w-2", "EUR", "60.00")
    assert (paid.status_code, again.status_code) == (201, 200)
    assert paid.json()["transfer"] == again.json()["transfer"]
    assert balances(client, trader)["EUR"] == Decimal("40.00")


def test_statement_view(client, sign_up):
    trader = sign_up("alice", EUR="50.00")
    trade_id = trade(client, trader, "t-1", "EUR", "USD", "20.00").json()["trade"]["id"]

    response = client.get(reverse("exchange:statement", args=[trader["trader"], "EUR"]))

    assert response.status_code == 200
    statement = response.json()
    assert statement["balance"] == "30.00"
    assert [
        (line["description"], line["amount"], line["balance"]) for line in statement["lines"]
    ] == [
        ("deposit by bank transfer", "50.00", "50.00"),
        (f"trade {trade_id} (sell)", "-19.95", "30.05"),
        (f"trade {trade_id} (fee)", "-0.05", "30.00"),
    ]
    unknown = client.get(reverse("exchange:statement", args=[trader["trader"], "GBP"]))
    assert unknown.status_code == 404


def test_close_wallet_view(client, sign_up):
    trader = sign_up("leaving", USD="8.00")
    close_url = reverse("exchange:close-wallet", args=[trader["trader"], "USD"])

    response = post(client, close_url)

    assert response.status_code == 200
    body = response.json()
    assert body["payout"]["amount"] == "8.00"
    assert body["portfolio"]["closed"] == ["USD"]
    assert body["portfolio"]["balances"]["USD"] == "0.00"
    deposit_url = reverse("exchange:deposit", args=[trader["trader"]])
    late = post(client, deposit_url, {"currency": "USD", "amount": "1.00"})
    assert late.status_code == 409 and late.json()["error"] == "pledge.account_closed"
    assert late.json()["detail"]["account_id"] == body["payout"]["from"]
    again = post(client, close_url)
    assert again.status_code == 409
    assert again.json()["error"] == "pledge.account_already_closed"


def test_zero_balances_render_in_plain_notation(client, sign_up):
    trader = sign_up("leaving", BTC="0.5")
    response = post(client, reverse("exchange:close-wallet", args=[trader["trader"], "BTC"]))
    assert response.json()["portfolio"]["balances"]["BTC"] == "0.00000000"


def test_house_view_sums_to_zero(client, sign_up):
    sign_up("alice", EUR="10.00")
    response = client.get(reverse("exchange:house"))
    assert response.status_code == 200
    totals = response.json()["totals"]
    assert set(totals) == {"BTC", "EUR", "USD"}
    assert all(Decimal(row["sum"]) == 0 for row in totals.values())


def test_card_deposit_and_payout_views(client, sign_up):
    trader = sign_up("carla")
    url = reverse("exchange:deposit", args=[trader["trader"]])
    response = post(client, url, {"currency": "USD", "amount": "200.00", "method": "card"})
    assert response.status_code == 201
    assert response.json()["transfer"]["metadata"] == {"kind": "deposit", "method": "card"}
    assert (
        post(client, url, {"currency": "USD", "amount": "1.00", "method": "cash"}).status_code
        == 400
    )

    payout = post(client, reverse("exchange:payout"), {"currency": "USD"})

    assert payout.status_code == 200
    assert [t["metadata"] for t in payout.json()["payouts"]] == [{"kind": "card_payout"}]
    totals = client.get(reverse("exchange:house")).json()["totals"]["USD"]
    assert Decimal(totals["card"]) == 0 and Decimal(totals["sum"]) == 0
    assert post(client, reverse("exchange:payout"), {"currency": "GBP"}).status_code == 400


def test_card_deposit_and_payout_pages(client, sign_up):
    trader = sign_up("dora")
    body = {"request_id": str(TransferId.new()), "currency": "EUR", "amount": "100.00"}
    deposit_url = reverse("pages:deposit", args=[trader["trader"]])

    response = client.post(deposit_url, {**body, "method": "card"}, follow=True)

    assert "by card; the broker pays 1.50 EUR card fee." in response.content.decode()
    page = client.get(reverse("pages:trader", args=[trader["trader"]])).content.decode()
    assert "deposit by card" in page and "Bank transfer" in page
    response = client.post(reverse("pages:payout"), follow=True)
    assert "The card provider paid out" in response.content.decode()
    assert "Card provider pays out" in response.content.decode()
    assert balances(client, trader)["EUR"] == Decimal("100.00")


def test_write_views_require_post(client, sign_up):
    trader = sign_up("alice")
    assert client.get(reverse("exchange:deposit", args=[trader["trader"]])).status_code == 405
    assert client.post(reverse("exchange:portfolio", args=[trader["trader"]])).status_code == 405


def test_api_accepts_posts_without_a_csrf_token(sign_up):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    response = post(strict, reverse("exchange:traders"), {"name": f"csrf-{TransferId.new()}"})
    assert response.status_code == 201


def test_deposit_rejects_a_non_string_transfer_id(client, sign_up):
    trader = sign_up("typed")
    url = reverse("exchange:deposit", args=[trader["trader"]])
    response = post(client, url, {"currency": "EUR", "amount": "1.00", "transfer_id": 123})
    assert response.status_code == 400
