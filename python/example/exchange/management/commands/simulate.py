import random
import time
import uuid
from collections import Counter
from decimal import ROUND_DOWN, Decimal
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_pledge.models import Account
from exchange import services
from exchange.models import HouseAccount, Trader

LIQUIDITY_TARGET_EUR = Decimal(1_000_000)
DEPOSIT_RANGE_EUR = (20, 5_000)


class Command(BaseCommand):
    help = (
        "Create traders, run random card and bank deposits, trades, withdrawals and card"
        " payouts, then check the books."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        # --users is the old name, still used by the justfile.
        parser.add_argument("--traders", "--users", dest="traders", type=int, default=20)
        parser.add_argument("--trades", type=int, default=500)
        parser.add_argument("--seed", type=int, default=None)

    def handle(
        self, *args: Any, traders: int, trades: int, seed: int | None, **options: Any
    ) -> None:
        if traders < 1 or trades < 0:
            raise CommandError("need at least one trader and a non-negative number of trades")
        self.rng = random.Random(seed)
        self.counts: Counter[str] = Counter()
        started = time.monotonic()

        self.top_up_liquidity()
        # Names stay unique across runs against the same database.
        run = uuid.uuid4().hex[:8]
        people = [services.sign_up(f"sim-{run}-{number}") for number in range(traders)]
        for person in people:
            self.deposit(person)
        for _ in range(trades):
            person = self.rng.choice(people)
            roll = self.rng.random()
            if roll < 0.15:
                self.deposit(person)
            elif roll < 0.25:
                self.withdraw(person)
            elif roll < 0.28:
                self.payout()
            self.trade(person)

        Account.objects.assert_verified()
        elapsed = time.monotonic() - started
        self.report(traders, trades, elapsed)

    def price(self, currency: str) -> Decimal:
        return Decimal(settings.EXCHANGE_PRICES_EUR[currency])

    def random_amount(self, currency: str, low: Decimal, high: Decimal) -> Decimal:
        unit = services.smallest_unit(currency)
        amount = Decimal(self.rng.uniform(float(low), float(high))).quantize(unit, ROUND_DOWN)
        return max(amount, unit)

    def balances(self, person: Trader) -> dict[str, Decimal]:
        return {h.currency: h.balance for h in services.portfolio(person) if not h.closed}

    def top_up_liquidity(self) -> None:
        for currency in services.currencies():
            target = (LIQUIDITY_TARGET_EUR / self.price(currency)).quantize(
                services.smallest_unit(currency)
            )
            liquidity = services.house_account(HouseAccount.Kind.LIQUIDITY, currency)
            if liquidity.balance < target:
                services.fund_liquidity(currency, target - liquidity.balance)

    def deposit(self, person: Trader) -> None:
        currency = self.rng.choice(services.fiat_currencies())
        low, high = (Decimal(bound) / self.price(currency) for bound in DEPOSIT_RANGE_EUR)
        method = self.rng.choice(list(services.DEPOSIT_METHODS))
        services.deposit(person, currency, self.random_amount(currency, low, high), method)
        self.counts[f"deposits by {method}"] += 1

    def payout(self) -> None:
        for currency in services.currencies():
            if services.payout(currency):
                self.counts["card payouts"] += 1

    def withdraw(self, person: Trader) -> None:
        fiat = services.fiat_currencies()
        funded = {c: b for c, b in self.balances(person).items() if b > 0 and c in fiat}
        if not funded:
            return
        currency = self.rng.choice(sorted(funded))
        # Sometimes more than the balance, so some withdrawals are refused.
        high = funded[currency] * Decimal("1.3")
        amount = self.random_amount(currency, funded[currency] / 10, high)
        try:
            services.request_withdrawal(person, uuid.uuid4().hex, currency, amount)
        except services.Rejected as exc:
            self.counts[f"withdrawals {exc.record.failure}"] += 1
        else:
            self.counts["withdrawals completed"] += 1

    def trade(self, person: Trader) -> None:
        balances = self.balances(person)
        funded = sorted(c for c, b in balances.items() if b > 0)
        if not funded:
            return
        sell = self.rng.choice(funded)
        buy = self.rng.choice([c for c in services.currencies() if c != sell])
        amount = self.random_amount(sell, balances[sell] / 20, balances[sell] * Decimal("1.1"))
        request_id = uuid.uuid4().hex
        try:
            trade, transfers = services.place_trade(person, request_id, sell, buy, amount)
        except ValueError:
            self.counts["trades too small"] += 1
            return
        except services.Rejected as exc:
            self.counts[f"trades {exc.record.failure}"] += 1
            return
        self.counts["trades completed"] += 1
        if self.rng.random() < 0.1:
            # A client that timed out sends the same request again: nothing is booked twice.
            _, replayed = services.place_trade(person, request_id, sell, buy, amount)
            if [t.created_at for t in replayed] != [t.created_at for t in transfers]:
                raise CommandError(f"retry of {trade} booked new transfers")
            self.counts["trades retried"] += 1

    def report(self, traders: int, trades: int, elapsed: float) -> None:
        write = self.stdout.write
        write(f"simulated {traders} traders and {trades} trades in {elapsed:.1f}s")
        for name, count in sorted(self.counts.items()):
            write(f"  {name:<36} {count:>6}")
        header = ["currency", "customers", *HouseAccount.Kind.values, "sum"]
        write("".join(f"{column:>20}" for column in header))
        totals = services.ledger_totals()
        for currency, row in totals.items():
            values = [row[column] for column in header[1:]]
            write(f"{currency:>20}" + "".join(f"{value:>20f}" for value in values))
        unbalanced = [currency for currency, row in totals.items() if row["sum"] != 0]
        if unbalanced:
            raise CommandError(f"balances do not sum to zero for {', '.join(unbalanced)}")
        write("every currency sums to 0 and pledge.verify() passed")
