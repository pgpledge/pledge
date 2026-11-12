from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from exchange import models, services
from exchange.models import HouseAccount, Status, Trade, Wallet, WalletAccount, Withdrawal

import django_pledge as pledge
from django_pledge import AccountClosed, TransferId
from django_pledge.models import Account, Transfer

Kind = HouseAccount.Kind

pytestmark = pytest.mark.django_db


def balance(account: Account) -> Decimal:
    account.refresh_from_db()
    return account.balance


def trader_balance(trader, currency: str) -> Decimal:
    return balance(services.wallet(trader, currency).account)


def house(kind: str, currency: str) -> Decimal:
    return balance(services.house_account(kind, currency))


def test_migrations_up_to_date():
    call_command("makemigrations", "exchange", "--check", "--dry-run", verbosity=0)


def test_sign_up_opens_one_wallet_per_currency(make_trader):
    trader = make_trader("alice")
    wallets = Wallet.objects.filter(trader=trader).select_related("account")
    assert [w.currency for w in wallets] == ["BTC", "EUR", "USD"]
    for w in wallets:
        assert isinstance(w.account.id, pledge.AccountId)
        assert (w.account.currency, w.account.balance) == (w.currency, 0)
        assert not w.account.allow_negative_balance
        assert w.account.metadata == {"trader": trader.pk}
    assert Account.objects.filter(wallet__trader=trader).count() == 3


def test_house_accounts_are_created_once_with_the_right_limits():
    bank = services.house_account(Kind.BANK, "BTC")
    assert bank == services.house_account(Kind.BANK, "BTC")
    assert bank.name == "house:bank:BTC"
    for kind in (Kind.BANK, Kind.CARD_COSTS):
        account = services.house_account(kind, "BTC")
        assert account.allow_negative_balance and not account.allow_positive_balance
    # paying out to cards can leave us owing the provider, so the card account may go positive
    card = services.house_account(Kind.CARD, "BTC")
    assert card.allow_negative_balance and card.allow_positive_balance
    for kind in (Kind.LIQUIDITY, Kind.FEES):
        account = services.house_account(kind, "BTC")
        assert not account.allow_negative_balance and account.allow_positive_balance
    assert HouseAccount.objects.filter(currency="BTC").count() == 5


def test_deposit_moves_money_from_the_bank(make_trader):
    trader = make_trader()
    bank_before = house(Kind.BANK, "USD")
    transfer = services.deposit(trader, "USD", Decimal("12.34"))
    assert transfer.from_account_id == services.house_account(Kind.BANK, "USD").id
    assert transfer.metadata == {"kind": "deposit", "method": "bank"}
    assert trader_balance(trader, "USD") == Decimal("12.34")
    assert house(Kind.BANK, "USD") == bank_before - Decimal("12.34")


def test_deposit_with_provider_id_is_idempotent(make_trader):
    trader = make_trader()
    payment_id = TransferId.new()
    first = services.deposit(trader, "EUR", Decimal("10.00"), transfer_id=payment_id)
    again = services.deposit(trader, "EUR", Decimal("10.00"), transfer_id=payment_id)
    assert first.id == again.id == payment_id
    assert first.created_at == again.created_at
    assert trader_balance(trader, "EUR") == Decimal("10.00")
    with pytest.raises(pledge.TransferConflict):
        services.deposit(trader, "EUR", Decimal("11.00"), transfer_id=payment_id)


def card_balances(currency: str) -> tuple[Decimal, Decimal, Decimal]:
    return (
        house(Kind.CARD, currency),
        house(Kind.BANK, currency),
        house(Kind.CARD_COSTS, currency),
    )


def test_card_deposit_books_the_card_fee_in_the_same_batch(make_trader):
    trader = make_trader()
    card, bank, costs = card_balances("EUR")

    transfer = services.deposit(trader, "EUR", Decimal("100.01"), "card")

    assert transfer.metadata == {"kind": "deposit", "method": "card"}
    assert transfer.from_account_id == services.house_account(Kind.CARD, "EUR").id
    fee = Transfer.objects.get(pk=services.card_fee_id(transfer.id))
    # 1.5% of 100.01 is 1.50015, rounded up to the cent
    assert (fee.amount, fee.metadata) == (Decimal("1.51"), {"kind": "card_fee"})
    assert fee.id.timestamp == transfer.id.timestamp
    assert trader_balance(trader, "EUR") == Decimal("100.01")
    assert card_balances("EUR") == (
        card - Decimal("100.01") + Decimal("1.51"),
        bank,
        costs - Decimal("1.51"),
    )


def test_card_deposit_retry_books_nothing_twice(make_trader):
    trader = make_trader()
    payment_id = TransferId.new()
    first = services.deposit(trader, "EUR", Decimal("10.00"), "card", transfer_id=payment_id)
    card, bank, costs = card_balances("EUR")

    again = services.deposit(trader, "EUR", Decimal("10.00"), "card", transfer_id=payment_id)

    assert first.id == again.id == payment_id and first.created_at == again.created_at
    assert card_balances("EUR") == (card, bank, costs)
    assert trader_balance(trader, "EUR") == Decimal("10.00")
    with pytest.raises(pledge.TransferConflict):
        services.deposit(trader, "EUR", Decimal("20.00"), "card", transfer_id=payment_id)


def test_crypto_never_moves_through_bank_or_card(make_trader):
    trader = make_trader(EUR="100.00")
    services.place_trade(trader, "t-1", "EUR", "BTC", Decimal("50.00"))
    for method in ("bank", "card"):
        with pytest.raises(ValueError, match="trade it for EUR or USD"):
            services.deposit(trader, "BTC", Decimal("0.001"), method)
        with pytest.raises(ValueError, match="trade it for EUR or USD"):
            services.request_withdrawal(trader, f"w-{method}", "BTC", Decimal("0.0001"), method)
        with pytest.raises(ValueError, match="for EUR or USD first, then close the wallet"):
            services.close_wallet(trader, "BTC", method)
    assert services.payout("BTC") is None
    assert not Withdrawal.objects.filter(trader=trader).exists()
    assert services.close_wallet(make_trader(), "BTC") is None  # an empty wallet closes fine


def test_deposit_rejects_unknown_methods(make_trader):
    with pytest.raises(ValueError, match="unknown deposit method"):
        services.deposit(make_trader(), "EUR", Decimal("1.00"), "cash")


def test_payout_moves_card_money_to_our_bank(make_trader):
    services.deposit(make_trader(), "USD", Decimal("80.00"), "card")
    card, bank, costs = card_balances("USD")
    assert card < 0

    transfer = services.payout("USD")

    assert transfer is not None and transfer.metadata == {"kind": "card_payout"}
    assert card_balances("USD") == (Decimal(0), bank + card, costs)
    assert services.payout("USD") is None
    assert services.ledger_totals()["USD"]["sum"] == 0


@pytest.mark.parametrize(
    ("sell", "buy", "amount", "fee", "buy_amount"),
    [
        # fee 0.25% rounded up, then (100.00 - 0.25) / 58000 rounded down to 8 places
        ("EUR", "BTC", "100.00", "0.25", "0.00171982"),
        ("BTC", "EUR", "0.01000000", "0.00002500", "578.55"),
        ("USD", "EUR", "10.00", "0.03", "9.17"),
        ("EUR", "USD", "0.40", "0.01", "0.42"),
    ],
)
def test_quote_math(sell, buy, amount, fee, buy_amount):
    priced = services.quote(sell, buy, Decimal(amount))
    assert priced.fee == Decimal(fee)
    assert priced.buy_amount == Decimal(buy_amount)
    assert priced.net_amount == Decimal(amount) - Decimal(fee)


@pytest.mark.parametrize(
    ("sell", "buy", "amount"),
    [
        ("EUR", "EUR", "10.00"),
        ("EUR", "BTC", "0.01"),
        ("EUR", "BTC", "1.001"),
        ("EUR", "XYZ", "10.00"),
        ("EUR", "BTC", "-5.00"),
        ("EUR", "BTC", "NaN"),
    ],
)
def test_quote_rejects_bad_requests(sell, buy, amount):
    with pytest.raises(ValueError):
        services.quote(sell, buy, Decimal(amount))


def test_trade_books_all_legs_in_one_batch(make_trader):
    trader = make_trader(EUR="1000.00")
    before = {
        (kind, currency): house(kind, currency)
        for kind in (Kind.LIQUIDITY, Kind.FEES)
        for currency in ("EUR", "BTC")
    }

    trade, (sell_leg, fee_leg, buy_leg) = services.place_trade(
        trader, "t-1", "EUR", "BTC", Decimal("100.00")
    )

    assert trade.status == Status.COMPLETED
    assert (trade.fee, trade.buy_amount) == (Decimal("0.25"), Decimal("0.00171982"))
    assert [sell_leg.id, fee_leg.id, buy_leg.id] == trade.transfer_ids()
    assert (sell_leg.amount, fee_leg.amount, buy_leg.amount) == (
        Decimal("99.75"),
        Decimal("0.25"),
        Decimal("0.00171982"),
    )
    assert [t.metadata for t in (sell_leg, fee_leg, buy_leg)] == [
        {"kind": "trade", "trade": trade.pk, "leg": leg} for leg in ("sell", "fee", "buy")
    ]
    # One batch commits once, so all legs share the transaction timestamp.
    assert sell_leg.created_at == fee_leg.created_at == buy_leg.created_at
    assert trader_balance(trader, "EUR") == Decimal("900.00")
    assert trader_balance(trader, "BTC") == Decimal("0.00171982")
    assert house(Kind.LIQUIDITY, "EUR") == before[Kind.LIQUIDITY, "EUR"] + Decimal("99.75")
    assert house(Kind.FEES, "EUR") == before[Kind.FEES, "EUR"] + Decimal("0.25")
    assert house(Kind.LIQUIDITY, "BTC") == before[Kind.LIQUIDITY, "BTC"] - Decimal("0.00171982")
    assert house(Kind.FEES, "BTC") == before[Kind.FEES, "BTC"]
    assert set(trade.ledger_transfers()) == {sell_leg, fee_leg, buy_leg}


def test_trade_retry_replays_the_same_transfers(make_trader):
    trader = make_trader(EUR="500.00")
    trade, first = services.place_trade(trader, "t-1", "EUR", "USD", Decimal("200.00"))
    again, retry = services.place_trade(trader, "t-1", "EUR", "USD", Decimal("200.00"))
    assert again.pk == trade.pk
    assert [t.id for t in retry] == [t.id for t in first]
    assert [t.created_at for t in retry] == [t.created_at for t in first]
    eur = services.wallet(trader, "EUR").account
    assert eur.balance == Decimal("300.00")
    assert eur.version == 3  # the deposit plus the sell and fee legs, once
    assert Trade.objects.filter(trader=trader).count() == 1
    with pytest.raises(services.RequestConflict):
        services.place_trade(trader, "t-1", "EUR", "USD", Decimal("250.00"))


def test_trade_without_funds_is_rejected_and_stays_rejected(make_trader):
    trader = make_trader(EUR="50.00")
    with pytest.raises(services.Rejected) as excinfo:
        services.place_trade(trader, "t-1", "EUR", "BTC", Decimal("60.00"))
    trade = excinfo.value.record
    assert (trade.status, trade.failure) == (Status.FAILED, "insufficient_funds")
    assert not trade.ledger_transfers().exists()
    assert trader_balance(trader, "EUR") == Decimal("50.00")

    services.deposit(trader, "EUR", Decimal("50.00"))
    with pytest.raises(services.Rejected):
        services.place_trade(trader, "t-1", "EUR", "BTC", Decimal("60.00"))
    assert not trade.ledger_transfers().exists()
    _, legs = services.place_trade(trader, "t-2", "EUR", "BTC", Decimal("60.00"))
    assert len(legs) == 3


def test_failing_buy_leg_leaves_nothing_booked(make_trader):
    # The trader can pay, but the exchange holds too little BTC: the third leg fails.
    inventory = house(Kind.LIQUIDITY, "BTC")
    eur_needed = (inventory * 58_000 * 2).quantize(Decimal("0.01"))
    trader = make_trader(EUR=eur_needed)
    eur_liquidity, eur_fees = house(Kind.LIQUIDITY, "EUR"), house(Kind.FEES, "EUR")

    with pytest.raises(services.Rejected) as excinfo:
        services.place_trade(trader, "too-big", "EUR", "BTC", eur_needed)

    trade = excinfo.value.record
    assert trade.failure == "insufficient_liquidity"
    assert not trade.ledger_transfers().exists()
    assert trader_balance(trader, "EUR") == eur_needed
    assert trader_balance(trader, "BTC") == 0
    assert house(Kind.LIQUIDITY, "EUR") == eur_liquidity
    assert house(Kind.FEES, "EUR") == eur_fees
    assert house(Kind.LIQUIDITY, "BTC") == inventory
    assert Trade.objects.get(pk=trade.pk).status == Status.FAILED


def test_withdrawal_and_idempotent_retry(make_trader):
    trader = make_trader(USD="80.00")
    bank_before = house(Kind.BANK, "USD")
    withdrawal, transfer = services.request_withdrawal(trader, "w-1", "USD", Decimal("30.00"))
    again, retry = services.request_withdrawal(trader, "w-1", "USD", Decimal("30.00"))
    assert again.pk == withdrawal.pk and withdrawal.status == Status.COMPLETED
    assert transfer.id == retry.id == withdrawal.transfer_id
    expected = {"kind": "withdrawal", "withdrawal": withdrawal.pk, "method": "bank"}
    assert transfer.metadata == expected
    assert trader_balance(trader, "USD") == Decimal("50.00")
    assert house(Kind.BANK, "USD") == bank_before + Decimal("30.00")
    with pytest.raises(services.RequestConflict):
        services.request_withdrawal(trader, "w-1", "EUR", Decimal("30.00"))


def test_withdrawal_without_funds_is_rejected(make_trader):
    trader = make_trader(USD="20.00")
    with pytest.raises(services.Rejected) as excinfo:
        services.request_withdrawal(trader, "w-1", "USD", Decimal("20.01"))
    assert excinfo.value.record.failure == "insufficient_funds"
    assert Withdrawal.objects.get(trader=trader).status == Status.FAILED
    assert trader_balance(trader, "USD") == Decimal("20.00")
    assert not Transfer.objects.filter(pk=excinfo.value.record.transfer_id).exists()


def test_close_wallet_pays_out_and_rejects_new_transfers(make_trader):
    trader = make_trader(USD="50.00", EUR="100.00")
    payout = services.close_wallet(trader, "USD")
    assert payout is not None and payout.amount == Decimal("50.00")
    assert payout.to_account_id == services.house_account(Kind.BANK, "USD").id
    account = services.wallet(trader, "USD").account
    assert account.is_closed and account.balance == 0

    with pytest.raises(AccountClosed):
        services.deposit(trader, "USD", Decimal("1"))
    with pytest.raises(services.Rejected) as excinfo:
        services.place_trade(trader, "buy-usd", "EUR", "USD", Decimal("10.00"))
    assert excinfo.value.record.failure == "account_closed"
    assert trader_balance(trader, "EUR") == Decimal("100.00")
    with pytest.raises(pledge.AccountAlreadyClosed):
        services.close_wallet(trader, "USD")


def test_close_empty_wallet_needs_no_payout(make_trader):
    trader = make_trader()
    assert services.close_wallet(trader, "USD") is None
    assert services.wallet(trader, "USD").account.is_closed


def test_portfolio_values_holdings_in_eur(make_trader):
    trader = make_trader(EUR="1000.00", USD="10.00")
    services.place_trade(trader, "t-1", "EUR", "BTC", Decimal("900.00"))
    holdings = {h.currency: h for h in services.portfolio(trader)}
    btc = holdings["BTC"].balance
    assert btc > 0 and holdings["BTC"].value_eur == (btc * 58000).quantize(Decimal("0.01"))
    assert holdings["USD"].value_eur == Decimal("9.20")
    assert holdings["EUR"].value_eur == Decimal("100.00")
    assert not any(h.closed for h in holdings.values())


# real commits: within one transaction every movement shares one timestamp
@pytest.mark.django_db(transaction=True)
def test_statement_lists_every_movement_with_running_balance(make_trader):
    trader = make_trader(EUR="100.00")
    trade, _ = services.place_trade(trader, "t-1", "EUR", "BTC", Decimal("40.00"))
    withdrawal, _ = services.request_withdrawal(trader, "w-1", "EUR", Decimal("10.00"))

    lines = services.wallet(trader, "EUR").ledger_account().statement()

    assert [(line.description, line.amount, line.balance) for line in lines] == [
        ("deposit by bank transfer", Decimal("100.00"), Decimal("100.00")),
        (f"trade {trade.pk} (sell)", Decimal("-39.90"), Decimal("60.10")),
        (f"trade {trade.pk} (fee)", Decimal("-0.10"), Decimal("60.00")),
        (f"withdrawal {withdrawal.pk} to bank account", Decimal("-10.00"), Decimal("50.00")),
    ]
    account = services.wallet(trader, "EUR").account
    assert account.balance_at(lines[0].at) == Decimal("100.00")
    assert account.balance_at(lines[0].at.replace(year=2000)) == 0


def test_wallet_account_proxy(make_trader):
    trader = make_trader(EUR="10.00")
    account = services.wallet(trader, "EUR").ledger_account()
    assert (
        isinstance(account, WalletAccount)
        and account.pk == services.wallet(trader, "EUR").account_id
    )
    assert account.can_spend(Decimal("10.00")) and not account.can_spend(Decimal("10.01"))
    assert WalletAccount.objects.filter(wallet__trader=trader).count() == 3
    services.close_wallet(trader, "EUR")
    account.refresh_from_db()
    assert not account.can_spend(Decimal("0.00"))


def test_every_currency_sums_to_zero(make_trader):
    trader = make_trader(EUR="12.34", USD="5.00")
    services.deposit(trader, "EUR", Decimal("50.00"), "card")
    services.place_trade(trader, "t-1", "EUR", "BTC", Decimal("20.00"))
    services.payout("EUR")
    for currency, row in services.ledger_totals().items():
        assert row["sum"] == 0, currency
        inside = row["liquidity"] + row["fees"] + row["customers"]
        assert row["card"] + row["bank"] + row["card_costs"] == -inside, currency


def test_simulate_command():
    out = StringIO()
    call_command("simulate", traders=4, trades=60, seed=7, stdout=out)
    output = out.getvalue()
    assert "simulated 4 traders and 60 trades" in output
    assert "trades completed" in output
    assert "every currency sums to 0 and pledge.verify() passed" in output


def test_fund_liquidity_command():
    before = house(Kind.LIQUIDITY, "USD")
    out = StringIO()
    call_command("fund_liquidity", "USD", "25.00", stdout=out)
    assert "funded 25.00 USD" in out.getvalue()
    assert house(Kind.LIQUIDITY, "USD") == before + Decimal("25.00")


def test_ledger_verifies_clean():
    checks = Account.objects.verify()
    assert checks and all(check.ok for check in checks), checks


def test_withdrawal_to_card_shows_the_fee_and_settles_with_the_provider():
    trader = services.sign_up("carla")
    services.deposit(trader, "EUR", Decimal("100.00"))
    withdrawal, transfer = services.request_withdrawal(
        trader, "w-card", "EUR", Decimal("40.00"), method="card"
    )
    card = services.house_account(Kind.CARD, "EUR")
    assert transfer.to_account_id == card.id and withdrawal.method == "card"
    assert transfer.metadata["card_fee"] == "0.60" and transfer.metadata["received"] == "39.40"
    assert card.balance == Decimal("40.00"), "we owe the provider what it paid out to the card"
    settlement = services.payout("EUR")
    assert settlement.from_account_id == card.id
    card.refresh_from_db()
    assert card.balance == 0
    with pytest.raises(services.RequestConflict):
        services.request_withdrawal(trader, "w-card", "EUR", Decimal("40.00"), method="bank")


def test_closing_a_wallet_can_pay_out_to_card():
    trader = services.sign_up("cora")
    services.deposit(trader, "EUR", Decimal("10.00"))
    payout = services.close_wallet(trader, "EUR", method="card")
    assert payout.metadata["method"] == "card" and payout.metadata["received"] == "9.85"
    with pytest.raises(ValueError):
        services.close_wallet(services.sign_up("cleo"), "EUR", method="cash")


def test_send_by_account_number(make_trader):
    sender, recipient = make_trader(EUR="50.00"), make_trader()
    number = services.wallet(recipient, "EUR").number
    assert len(number) == 10 and models.parse_account_number(number) == number
    spaced = f"{number[:4]} {number[4:8]} {number[8:]}"
    transfer = services.send(sender, spaced, Decimal("20.00"))
    assert transfer.to_account_id == services.wallet(recipient, "EUR").account_id
    typo = number[:-1] + str((int(number[-1]) + 1) % 10)
    with pytest.raises(ValueError, match="not a valid account number"):
        services.send(sender, typo, Decimal("1.00"))
    with pytest.raises(ValueError, match="your own account"):
        services.send(sender, services.wallet(sender, "EUR").number, Decimal("1.00"))
