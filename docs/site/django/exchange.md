---
title: Build an exchange
description: A step-by-step tour of the example currency exchange. Your own models next to the ledger, deposits, trades, payments and payouts, and how to trust the books.
---

This tutorial walks through the example app in
[python/example](https://github.com/pgpledge/pledge/tree/main/python/example): a small currency
exchange built on `django-pledge`. Traders hold wallets in EUR, USD and BTC. They deposit money,
trade between currencies, pay each other and withdraw. The broker earns a fee on every trade.

You don't need to know double-entry bookkeeping. One rule is enough: **money never appears or
disappears, it only moves from one account to another.** Every step below shows which accounts
move.

The code excerpts are real but trimmed. `# ...` marks the cuts.

## Run it

:::steps
1. **Start the example.** From the repository root:

   ```sh
   docker compose --profile example up --build example
   ```

   The start script creates a database `exchange`, runs the migrations, funds the exchange with
   money to trade, and creates an admin user (`admin` / `admin`).

2. **Open** [http://localhost:8000](http://localhost:8000). The admin is at `/admin/`.
:::

## 1. Set up

The project is a normal Django project. `django_pledge` is one more app, next to your own:

```python title="core/settings.py"
INSTALLED_APPS = [
    # ...
    "django_pledge",
    "exchange",
]
```

`python manage.py migrate` creates the extension. If your app connects with its own database
role, give it the [grants](../install.md) once. See [Django setup](setup.md) for the details.

The exchange keeps its business rules in settings:

```python title="core/settings.py"
# Currency code -> decimal places of its smallest unit.
EXCHANGE_CURRENCIES = {"EUR": 2, "USD": 2, "BTC": 8}
# Only bought and sold on the exchange: no bank or card deposits and payouts.
EXCHANGE_CRYPTO = {"BTC"}
# Reference prices in EUR; a real exchange would read these from a price feed.
EXCHANGE_PRICES_EUR = {"EUR": "1", "USD": "0.92", "BTC": "58000"}
EXCHANGE_FEE_RATE = "0.0025"
# What the card provider charges the broker per card deposit.
EXCHANGE_CARD_FEE_RATE = "0.015"
```

All ledger work lives in one module, `exchange/services.py`. The views only parse forms and call it.

## 2. Model your data next to the ledger

Ledger rows can't change, so your own data goes in your own models. A `Trader` is a plain model
with a name. A `Wallet` links a trader to one ledger account per currency:

```python title="exchange/models.py"
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
```

`db_constraint=False` is required: the pledge models sit on database views, and a view can't be
the target of a foreign key. Joins and filters still work, like
`Wallet.objects.filter(account__balance__gt=0)`.

The `number` is what traders see and type when they pay each other. It has ten digits, and the
last one is a [Luhn](https://en.wikipedia.org/wiki/Luhn_algorithm) check digit, so a typo is caught
before any lookup.

### The broker's own accounts

Every transfer has two sides. When Alice deposits 100 EUR, the money has to come *from* an
account. So the exchange keeps its own accounts, five per currency:

```python title="exchange/models.py"
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
```

| Kind | What it is | Balance |
| --- | --- | --- |
| `liquidity` | The currency the exchange holds to sell to traders | zero or more |
| `fees` | What the broker earned on trades | zero or more |
| `bank` | Money that came in from, or went out to, real bank accounts | zero or less |
| `card` | Card payments the card provider still holds for us | usually less than zero |
| `card_costs` | Fees the card provider charged the broker | zero or less |

The last three stand for money **outside** the ledger. When 100 EUR arrives from Alice's bank,
`bank` goes to −100 and Alice's wallet to +100. The negative balance says: 100 EUR came in from
outside. That is how every currency sums to zero.

`house_account()` creates each one on first use, with balance rules that match the table:

```python title="exchange/services.py"
# Money outside the ledger: these balance everything inside it, so they never go above zero.
EXTERNAL_KINDS = {HouseAccount.Kind.CARD, HouseAccount.Kind.BANK, HouseAccount.Kind.CARD_COSTS}
# Money paid out to cards can exceed what the provider holds for us: then we owe the provider.
MAY_GO_POSITIVE = {HouseAccount.Kind.CARD}

def house_account(kind: str, currency: str) -> Account:
    # ...
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
```

pledge enforces these rules on every transfer. Liquidity can't go below zero, so the exchange can
never sell BTC it doesn't have. The bank account can't go above zero, so the exchange can never
pay out more than came in.

Before anyone trades, the owner puts capital in. The start script runs
`python manage.py fund_liquidity EUR 1000000` (and the same for USD and BTC):

```python title="exchange/services.py"
def fund_liquidity(currency: str, amount: Decimal) -> Transfer:
    """The owner's capital arrives in our bank account and becomes the exchange's inventory."""
    amount = check_amount(currency, amount)
    bank = house_account(HouseAccount.Kind.BANK, currency)
    liquidity = house_account(HouseAccount.Kind.LIQUIDITY, currency)
    return bank.transfer(liquidity, amount, metadata={"kind": "funding"})
```

```text
bank EUR  →  liquidity EUR   1000000.00   funding
```

## 3. Open a trader

On **Traders › New trader**, type a name.

::image[The New trader form with the name Alice]{src="img/exchange/01-new-trader.png" caption="Opening a trader account."}

Alice gets a EUR wallet straight away, and buttons to add the other currencies. A trade needs
two wallets, so the **Exchange** tab asks her to open another one first.

::image[Alice's page with one empty EUR wallet, buttons to add US dollar and Bitcoin wallets, and an Exchange tab that asks for a second wallet]{src="img/exchange/02-trader-opened.png" caption="A new trader with one EUR wallet."}

Each wallet is a new ledger account. Both rows are created in one transaction, so you never get a
wallet without an account or the other way round:

```python title="exchange/services.py"
def open_wallet(trader: Trader, currency: str) -> Wallet:
    """Open the trader's ledger account in one more currency."""
    # ...
    with transaction.atomic():
        account = Account.objects.create(
            name=f"trader:{trader.pk}:{currency}",
            currency=currency,
            allow_negative_balance=False,
            metadata={"trader": trader.pk},
        )
        return Wallet.objects.create(trader=trader, currency=currency, account=account)
```

**In the ledger:** one new account, balance 0. No money moves. `allow_negative_balance=False`
means pledge itself refuses any transfer that would overdraw the wallet, however your code calls it.

## 4. Deposit money

On the **Deposit** tab, Alice deposits 1,000 EUR by bank transfer, then 250 EUR by card.

::image[Alice's EUR wallet at 1250.00 with two green incoming deposits in the activity list]{src="img/exchange/03-deposits.png" caption="Two deposits: one by bank, one by card."}

A bank deposit is one transfer. A card deposit is two: the card provider keeps 1.5%, and the
broker pays that fee, not Alice. Both legs go into one `bulk_create`, so they're booked together
or not at all:

```python title="exchange/services.py"
def deposit(
    trader: Trader | int,
    currency: str,
    amount: Decimal,
    method: str = "bank",
    *,
    transfer_id: TransferId | None = None,
) -> Transfer:
    # ...
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
```

**In the ledger:**

```text
bank EUR        →  Alice EUR   1000.00   deposit by bank transfer
card EUR        →  Alice EUR    250.00   deposit by card
card_costs EUR  →  card EUR       3.75   card processing fee
```

The card provider now holds 246.25 EUR for us: 250 minus its fee. It shows as `card` at −246.25.

:::tip[Safe retries]
The deposit form carries a fresh transfer id in a hidden field, and the view passes it as
`transfer_id`. If the form is posted twice, pledge sees the same id and books it once. A real app
would use the payment provider's id.

The fee leg needs its own id that is just as stable. `card_fee_id()` derives it from the deposit id
with `uuid.uuid5`, so a retry sends the same pair of ids.
:::

## 5. Trade

Alice adds a Bitcoin wallet and opens the **Exchange** tab. She sells 100 EUR for BTC and clicks
**Preview** first.

::image[The Exchange tab with a quote: fee 0.25 EUR, you get 0.00171982 BTC]{src="img/exchange/04-trade-preview.png" caption="Preview of selling 100 EUR for BTC."}

The preview is a plain GET with the inputs in the URL, like `/traders/1/?sell=EUR&buy=BTC&amount=100`.
It calls `quote()`, which books nothing:

```python title="exchange/services.py"
def quote(sell_currency: str, buy_currency: str, sell_amount: Decimal) -> Quote:
    """Price a conversion: the fee is taken from the sold amount, the rest is converted.

    Both roundings favour the exchange: the fee rounds up and the bought amount rounds down.
    """
    # ...
    rate = Decimal(prices[sell_currency]) / Decimal(prices[buy_currency])
    fee_rate = Decimal(settings.EXCHANGE_FEE_RATE)
    fee = (sell_amount * fee_rate).quantize(smallest_unit(sell_currency), rounding=ROUND_UP)
    buy_amount = ((sell_amount - fee) * rate).quantize(buy_unit, rounding=ROUND_DOWN)
    # ...
```

She clicks **Exchange**.

::image[Alice now holds 0.00171982 BTC and 1150.00 EUR; the EUR activity shows Trade 1 sell and fee]{src="img/exchange/05-trade-done.png" caption="After the trade: two EUR legs out, BTC in."}

pledge never converts currencies: a transfer always stays within one currency. So a trade is
three transfers, and the exchange's liquidity accounts sit in the middle. Alice pays EUR into the
exchange's EUR inventory, and the exchange pays BTC out of its BTC inventory:

```python title="exchange/services.py"
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
```

**In the ledger:**

```text
Alice EUR      →  liquidity EUR   99.75        sell
Alice EUR      →  fees EUR         0.25        fee
liquidity BTC  →  Alice BTC        0.00171982  buy
```

Each currency still sums to zero on its own. The three legs go into one `bulk_create`: if Alice
lacks the EUR, or the exchange lacks the BTC, nothing is booked.

### Retries and failures

A `Trade` row stores the quote and the three transfer ids *before* pledge is called:

```python title="exchange/models.py"
class Trade(models.Model):
    trader = models.ForeignKey(Trader, models.PROTECT, related_name="trades")
    # Chosen by the client, so a retried request finds this trade instead of making a new one.
    request_id = models.CharField(max_length=64)
    # ...
    # Stored before pledge is called, so every retry sends the same transfer ids.
    sell_transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
    fee_transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
    buy_transfer_id = TransferIdField(default=TransferId.new, unique=True, editable=False)
```

`place_trade()` finds or creates the trade by `request_id`, then runs it:

```python title="exchange/services.py"
def execute_trade(trade_id: int) -> tuple[Trade, list[Transfer]]:
    # ...
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
    # ...
```

Send the same request again and pledge receives the same three ids. It returns the transfers it
already booked and moves no money. A refused trade is saved as failed, with a reason:
`insufficient_funds` when Alice's wallet is short, `insufficient_liquidity` when the exchange's is.

## 6. Pay another trader

Bob signs up and adds a US dollar wallet. Each wallet has its own account number, and that
number is all a trader ever sees. The ledger's own account id stays internal.

::image[Bob's page after opening a USD wallet: a banner with the new account number, and empty EUR and USD wallets]{src="img/exchange/06-bob-wallets.png" caption="Bob has two wallets, each with its own number."}

Alice types Bob's EUR account number on the **Send** tab.

::image[The Send tab with Bob's account number and the amount 50]{src="img/exchange/07-send-form.png" caption="Sending 50 to Bob's EUR account."}

The number decides the currency. The Luhn check runs first, so a typo is reported as such, not as
"no such account":

```python title="exchange/services.py"
def send(
    sender: Trader, to_account: str, amount: Decimal, *, transfer_id: TransferId | None = None
) -> Transfer:
    """Pay another trader. The recipient's account number fixes the currency."""
    recipients = Wallet.objects.select_related("trader", "account")
    try:
        recipient = recipients.get(number=parse_account_number(to_account))
    except Wallet.DoesNotExist:
        raise ValueError("no trader account with that number") from None
    # ...
    source = wallet(sender, recipient.currency).account
    metadata = {"kind": "payment", "from": sender.name, "to": recipient.trader.name}
    return source.transfer(recipient.account, amount, id=transfer_id, metadata=metadata)
```

**In the ledger:**

```text
Alice EUR  →  Bob EUR   50.00   payment Alice → Bob
```

One transfer between two customers. The exchange's accounts don't move.

::image[Bob's EUR wallet at 50.00 with an incoming payment from Alice]{src="img/exchange/08-bob-received.png" caption="Bob's statement shows the payment."}

The statement comes from the ledger itself. `Account.history()` returns the account's entries,
and each entry carries the balance right after it:

```python title="exchange/models.py"
class WalletAccount(Account):
    class Meta:
        proxy = True

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
```

## 7. Withdraw and close a wallet

On the **Withdraw** tab, Alice takes 200 EUR out to her bank account.

::image[Alice's EUR statement: deposits, trade legs, payment to Bob and a 200 EUR withdrawal, balance 900.00]{src="img/exchange/09-withdrawn.png" caption="The full EUR statement after the withdrawal."}

A withdrawal works like a trade: a `Withdrawal` row with a `request_id` and a stored
`transfer_id`, then one transfer out of the wallet:

```python title="exchange/services.py"
def execute_withdrawal(withdrawal_id: int) -> tuple[Withdrawal, Transfer]:
    # ...
            account = wallet(withdrawal.trader_id, withdrawal.currency).account
            destination = payout_destination(withdrawal.currency, withdrawal.method)
            # ...
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
```

**In the ledger:**

```text
Alice EUR  →  bank EUR   200.00   withdrawal to bank account
```

The bank account goes back up by 200, toward zero: that money left the exchange.
`PositiveBalanceNotAllowed` is the bank's own rule from step 2. A withdrawal that would push it
above zero is refused as `insufficient_bank_balance`.

Withdrawing to a card goes to the `card` account instead. The provider keeps its 1.5%, so Alice
receives less. That split is only noted in the transfer's metadata; the ledger moves the full
amount.

Closing a wallet pays out what's left, then closes the account, in one transaction. BTC can't
leave by bank or card, so Alice has to sell it first:

::image[Error: Sell your 0.00171982 BTC for EUR or USD first, then close the wallet]{src="img/exchange/10-close-refused.png" caption="Closing a BTC wallet that still holds BTC is refused."}

```python title="exchange/services.py"
def close_wallet(trader: Trader | int, currency: str, method: str = "bank") -> Transfer | None:
    """Pay out any remaining balance and close the account, all or nothing."""
    # ...
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
            # ...
            payout = account.transfer(destination, account.balance, metadata=metadata)
        # close() checks the balance under its row lock: a late deposit raises AccountNotEmpty.
        account.close()
    return payout
```

A closed account stays in the ledger with its full history. pledge never deletes ledger rows.

## 8. The broker's view

The **Overview** page shows one row per currency: all traders together, each broker account, and
the sum.

::image[The overview table: EUR, USD and BTC rows, each with a sum of zero, and a verified ledger badge]{src="img/exchange/11-overview.png" caption="Every currency sums to zero."}

Check the EUR row: Alice's 900 plus Bob's 50 gives 950 for all traders. Add liquidity
1,000,099.75 and fees 0.25, and subtract what came from outside: bank 1,000,800, card 246.25 and
card costs 3.75. The result is 0.00. It always is, because every transfer adds to one account
exactly what it takes from another.

The table is three queries over the ledger:

```python title="exchange/services.py"
def ledger_totals() -> dict[str, dict[str, Decimal]]:
    """Per currency: all customers together, each house account, and the ledger sum (always 0)."""
    # ...
    for row in HouseAccount.objects.values("currency", "kind", "account__balance"):
        totals[row["currency"]][row["kind"]] = row["account__balance"]
    customers = Wallet.objects.values("currency").annotate(total=Sum("account__balance"))
    for row in customers:
        totals[row["currency"]]["customers"] = row["total"]
    for row in Account.objects.values("currency").annotate(total=Sum("balance")):
        # ...
        totals[row["currency"]]["sum"] = row["total"]
    return totals
```

The **Broker** page lists the five accounts per currency, their recent movements, and what the
card provider holds.

::image[Broker accounts for EUR: bank, card provider, card costs, fees and liquidity, with a Card provider panel holding 246.25 EUR]{src="img/exchange/12-broker.png" caption="The broker's EUR accounts. The card provider holds 246.25 EUR."}

**Pay out EUR to our bank** settles with the card provider:

```python title="exchange/services.py"
def payout(currency: str) -> Transfer | None:
    """Settle with the card provider: it pays out what it holds, or collects what we owe it."""
    # ...
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
```

**In the ledger:**

```text
bank EUR  →  card EUR   246.25   card provider payout
```

::image[After the payout the card provider account is at 0.00 and the bank at -1001046.25]{src="img/exchange/13-payout.png" caption="After the payout, the card provider holds nothing."}

The card account is back at zero. The money now shows in `bank`, which only means it moved from
the provider to our real bank account. Nobody's wallet changed.

## 9. Trust the books

pledge can check its own books. `pledge.verify()` recomputes every balance from the entries and
checks every transfer. From Django:

```python
from django_pledge.models import Account

Account.objects.verify()            # a list of checks, each with .ok
Account.objects.assert_verified()   # raises VerificationFailed if any check fails
```

The overview page runs `verify()` on every load and shows the **verified** badge. You'd run it
from a scheduled job in a real app.

The `simulate` command creates random traders and runs hundreds of deposits, trades, withdrawals,
retries and payouts against the running example. It then checks that every currency sums to zero
and calls `assert_verified()`:

```sh
just example-simulate
# or: docker compose --profile example run --rm example python example/manage.py simulate
```

The test suite checks the ledger after every single test, with an autouse fixture:

```python title="tests/conftest.py"
@pytest.fixture(autouse=True)
def ledger_stays_consistent(house_liquidity):
    yield
    from django_pledge.models import Account

    Account.objects.assert_verified()
```

Add this fixture to your own project's tests. Any bug that breaks the ledger then fails a test
straight away.

## Where next

::::cards
:::card[Using the models]{href="usage.md"}
Transfers, batches, retries, history and errors in detail.
:::

:::card[Your own models]{href="models.md"}
Link models to accounts, proxy models and the admin.
:::

:::card[Safe retries]{href="../sql/retries.md"}
How pledge handles a transfer id it has seen before.
:::

:::card[Errors]{href="../sql/errors.md"}
Every error code and what it means.
:::
::::
