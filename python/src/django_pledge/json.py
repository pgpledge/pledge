from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder


class LedgerJSONEncoder(DjangoJSONEncoder):
    """Writes Decimals in plain notation."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return format(o, "f")
        return super().default(o)
