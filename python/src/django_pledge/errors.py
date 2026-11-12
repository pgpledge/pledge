from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, ClassVar

from .ids import AccountId, TransferId

__all__ = [
    "AccountAlreadyClosed",
    "AccountClosed",
    "AccountNotEmpty",
    "AccountNotFound",
    "CurrencyMismatch",
    "DuplicateTransferId",
    "Immutable",
    "InvalidRequest",
    "LedgerError",
    "NegativeBalanceNotAllowed",
    "PositiveBalanceNotAllowed",
    "TransferConflict",
    "VerificationFailed",
    "translate",
]

SQLSTATE = "P0001"
_UNIQUE_VIOLATION = "23505"


class LedgerError(Exception):
    """A ledger rule violation.

    ``detail`` is the parsed JSON DETAIL of the database error. The values it carries are also
    set as typed attributes, listed in ``fields`` per subclass.
    """

    code: ClassVar[str] = ""
    fields: ClassVar[dict[str, Callable[[str], Any]]] = {}

    def __init__(self, message: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = dict(detail) if detail else {"code": self.code}
        for name, convert in self.fields.items():
            value = self.detail.get(name)
            setattr(self, name, None if value is None else convert(value))


class InvalidRequest(LedgerError):
    """Malformed request: null ids, same from/to account, non-positive or non-finite amount."""

    code = "pledge.invalid_request"


class AccountNotFound(LedgerError):
    code = "pledge.account_not_found"
    fields = {"account_id": AccountId}
    account_id: AccountId


class AccountClosed(LedgerError):
    """A transfer touched a closed account."""

    code = "pledge.account_closed"
    fields = {"account_id": AccountId}
    account_id: AccountId


class AccountAlreadyClosed(LedgerError):
    code = "pledge.account_already_closed"
    fields = {"account_id": AccountId}
    account_id: AccountId


class AccountNotEmpty(LedgerError):
    """Only accounts with a zero balance can be closed."""

    code = "pledge.account_not_empty"
    fields = {"account_id": AccountId, "balance": Decimal}
    account_id: AccountId
    balance: Decimal


class NegativeBalanceNotAllowed(LedgerError):
    code = "pledge.negative_balance_not_allowed"
    fields = {"account_id": AccountId}
    account_id: AccountId


class PositiveBalanceNotAllowed(LedgerError):
    code = "pledge.positive_balance_not_allowed"
    fields = {"account_id": AccountId}
    account_id: AccountId


class CurrencyMismatch(LedgerError):
    code = "pledge.currency_mismatch"
    fields = {"from_currency": str, "to_currency": str}
    from_currency: str
    to_currency: str


class TransferConflict(LedgerError):
    """A transfer id was reused with different values.

    ``transfer_id`` is ``None`` when two transactions created the same new id at once.
    """

    code = "pledge.transfer_conflict"
    fields = {"transfer_id": TransferId}
    transfer_id: TransferId | None


class DuplicateTransferId(LedgerError):
    """The same transfer id appears more than once in one batch."""

    code = "pledge.duplicate_transfer_id"
    fields = {"transfer_id": TransferId}
    transfer_id: TransferId


class Immutable(LedgerError):
    """An attempt to delete ledger rows or change their money or identity columns."""

    code = "pledge.immutable"
    fields = {"table": str}
    table: str


ERRORS: dict[str, type[LedgerError]] = {
    cls.code: cls
    for cls in (
        InvalidRequest,
        AccountNotFound,
        AccountClosed,
        AccountAlreadyClosed,
        AccountNotEmpty,
        NegativeBalanceNotAllowed,
        PositiveBalanceNotAllowed,
        CurrencyMismatch,
        TransferConflict,
        DuplicateTransferId,
        Immutable,
    )
}


def _parse_detail(detail: str | None) -> dict[str, Any] | None:
    try:
        parsed = json.loads(detail or "")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class VerificationFailed(AssertionError):
    """``pledge.verify()`` reported failed checks; ``failures`` holds them."""

    def __init__(self, failures: list[Any]) -> None:
        self.failures = failures
        lines = "\n".join(f"  {failure.check_name}: {failure.detail}" for failure in failures)
        super().__init__(f"pledge.verify() failed:\n{lines}")


def translate(exc: BaseException) -> LedgerError | None:
    """The LedgerError for a driver error or anything chaining one, else ``None``."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LedgerError):
            return current
        diag = getattr(current, "diag", None)
        sqlstate = getattr(current, "sqlstate", None)
        if diag is not None and sqlstate == SQLSTATE:
            detail = _parse_detail(diag.message_detail) or {}
            code = detail.get("code")
            error_class = ERRORS.get(code) if isinstance(code, str) else None
            if error_class is None or any(name not in detail for name in error_class.fields):
                return None
            return error_class(diag.message_primary or str(current), detail)
        if diag is not None and sqlstate == _UNIQUE_VIOLATION:
            if diag.constraint_name == "transfers_pkey":
                # A concurrent create of the same new id lost the race; nothing was booked twice.
                # Postgres puts the key only in a localized message, so transfer_id stays None.
                return TransferConflict(diag.message_primary or str(current))
            return None
        # Only the direct cause: __context__ could be an older ledger error that was being
        # handled when this one (e.g. a serialization failure) happened.
        current = current.__cause__
    return None
