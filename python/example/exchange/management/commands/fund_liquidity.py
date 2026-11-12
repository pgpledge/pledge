from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from exchange import services


class Command(BaseCommand):
    help = "Move owner capital from our bank account into the inventory for one currency."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("currency")
        parser.add_argument("amount")

    def handle(self, *args: Any, currency: str, amount: str, **options: Any) -> None:
        try:
            transfer = services.fund_liquidity(currency, Decimal(amount))
        except (InvalidOperation, ValueError) as exc:
            raise CommandError(f"cannot fund {amount} {currency}: {exc}") from exc
        self.stdout.write(f"funded {transfer.amount} {currency} liquidity ({transfer.id})")
