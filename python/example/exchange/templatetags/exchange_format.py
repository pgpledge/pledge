from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def plain(amount: Decimal | None) -> str:
    """Plain notation, so a zero BTC balance shows as 0.00000000 rather than 0E-8."""
    return "" if amount is None else format(amount, "f")


@register.filter
def short_rate(rate: Decimal) -> str:
    # Rates are exact quotients with many digits; eight significant digits are enough to read.
    return format(rate.quantize(Decimal(1).scaleb(rate.adjusted() - 7)), "f")


CURRENCY_NAMES = {"EUR": "Euro", "USD": "US dollar", "BTC": "Bitcoin"}


@register.filter
def currency_name(code: str) -> str:
    return CURRENCY_NAMES.get(code, code)


@register.filter
def account_number(number: str) -> str:
    """4821 0937 15: grouped like a bank account number."""
    return f"{number[:4]} {number[4:8]} {number[8:]}"


@register.filter
def signed(amount: Decimal) -> str:
    """Incoming amounts get a plus sign, like a bank statement."""
    return f"+{plain(amount)}" if amount > 0 else plain(amount)


@register.filter
def negative(amount: Decimal) -> str:
    """CSS class for money outside the exchange."""
    return "negative" if amount < 0 else ""
