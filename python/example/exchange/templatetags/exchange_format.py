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
