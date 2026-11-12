from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, NamedTuple

from django.db import DatabaseError, transaction

from .errors import translate
from .ids import AccountId, TransferId


class Check(NamedTuple):
    """One row of ``pledge.verify()``."""

    check_name: str
    ok: bool
    detail: str


@contextmanager
def atomic(using: str) -> Generator[None]:
    """``transaction.atomic`` that re-raises ledger errors as LedgerError subclasses."""
    try:
        with transaction.atomic(using=using):
            yield
    except DatabaseError as exc:
        error = translate(exc)
        if error is None:
            raise
        raise error from exc


def coerce_account_id(ref: Any) -> AccountId:
    """An AccountId from an Account, an AccountId, a ``pla_`` string or a UUID."""
    pk = getattr(ref, "pk", ref)
    if pk is None:
        raise ValueError(f"{ref!r} has no account id")
    return AccountId(pk)


def ensure_transfer_id(ref: Any) -> TransferId:
    """The given transfer id, or a fresh one so the caller always knows it."""
    return TransferId.new() if ref is None else TransferId(ref)


# Ids go as text or uuid and are cast in SQL, so no psycopg adapters are needed.
CREATE_ACCOUNT = "SELECT * FROM pledge.create_account(%s, %s, %s, %s, %s::jsonb)"

CLOSE_ACCOUNT = "SELECT * FROM pledge.close_account(%s::pledge.account_id)"

CREATE_TRANSFER = (
    "SELECT * FROM pledge.create_transfer(%s::pledge.account_id, %s::pledge.account_id,"
    " %s::numeric, %s::timestamptz, %s::jsonb, %s::pledge.transfer_id)"
)

# One fixed statement for any batch size, so it is planned once.
# NULL per-request event_at and metadata fall back to the batch values.
CREATE_TRANSFERS = """
SELECT * FROM pledge.create_transfers(
    ARRAY(
        SELECT pledge.request(r.f::pledge.account_id, r.t::pledge.account_id, r.amount,
                              r.id::pledge.transfer_id, r.ev, r.md)
        FROM unnest(%s::uuid[], %s::uuid[], %s::numeric[], %s::uuid[], %s::timestamptz[],
                    %s::jsonb[])
             WITH ORDINALITY AS r (f, t, amount, id, ev, md, n)
        ORDER BY r.n
    ),
    %s::timestamptz, %s::jsonb)
"""

VERIFY = "SELECT check_name, ok, detail FROM pledge.verify()"
