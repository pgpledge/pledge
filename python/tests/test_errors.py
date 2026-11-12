from decimal import Decimal

import psycopg
import pytest
from conftest import APP_DSN
from django.db import DatabaseError, connection, transaction

import django_pledge as pledge
from django_pledge import AccountId, TransferId, errors, sql
from django_pledge.models import Account, Transfer

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "other"])


def create_account_pair(currency="USD", **options):
    return (
        Account.objects.create(name="a", currency=currency, **options),
        Account.objects.create(name="b", currency="USD", **options),
    )


def test_error_registry_covers_all_codes():
    assert set(errors.ERRORS) == {
        "pledge.invalid_request",
        "pledge.account_not_found",
        "pledge.account_closed",
        "pledge.account_already_closed",
        "pledge.account_not_empty",
        "pledge.negative_balance_not_allowed",
        "pledge.positive_balance_not_allowed",
        "pledge.currency_mismatch",
        "pledge.transfer_conflict",
        "pledge.duplicate_transfer_id",
        "pledge.immutable",
    }


def test_invalid_transfer_raises_invalid_request():
    a, _ = create_account_pair()
    with pytest.raises(pledge.InvalidRequest) as exc_info:
        a.transfer(a, 1)
    assert exc_info.value.detail == {"code": "pledge.invalid_request"}
    assert not hasattr(exc_info.value, "account_id")
    assert isinstance(exc_info.value.__cause__, DatabaseError)
    for amount in (0, -1, Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(pledge.InvalidRequest):
            a.transfer(AccountId.new(), amount)


def test_unknown_account_raises_account_not_found():
    a, _ = create_account_pair()
    missing = AccountId.new()
    with pytest.raises(pledge.AccountNotFound) as exc_info:
        a.transfer(missing, 1)
    assert exc_info.value.account_id == missing
    assert isinstance(exc_info.value.account_id, AccountId)
    with pytest.raises(pledge.AccountNotFound) as exc_info:
        Account(id=missing).close()
    assert exc_info.value.account_id == missing


def test_closed_account_rejects_transfer_and_second_close():
    a, b = create_account_pair()
    b.close()
    with pytest.raises(pledge.AccountClosed) as exc_info:
        a.transfer(b, 1)
    assert exc_info.value.account_id == b.id
    with pytest.raises(pledge.AccountAlreadyClosed) as exc_info:
        b.close()
    assert exc_info.value.account_id == b.id


def test_balance_limits_raise_not_allowed_errors():
    a, b = create_account_pair(allow_negative_balance=False)
    with pytest.raises(pledge.NegativeBalanceNotAllowed) as exc_info:
        a.transfer(b, 1)
    assert exc_info.value.account_id == a.id
    capped = Account.objects.create(name="c", currency="USD", allow_positive_balance=False)
    source = Account.objects.create(name="d", currency="USD")
    with pytest.raises(pledge.PositiveBalanceNotAllowed) as exc_info:
        source.transfer(capped, 1)
    assert exc_info.value.account_id == capped.id


def test_cross_currency_transfer_raises_currency_mismatch():
    a, b = create_account_pair(currency="EUR")
    with pytest.raises(pledge.CurrencyMismatch) as exc_info:
        a.transfer(b, 1)
    assert (exc_info.value.from_currency, exc_info.value.to_currency) == ("EUR", "USD")


def test_reused_or_duplicate_transfer_id_raises():
    a, b = create_account_pair()
    transfer_id = TransferId.new()
    a.transfer(b, 1, id=transfer_id)
    with pytest.raises(pledge.TransferConflict) as exc_info:
        a.transfer(b, 2, id=transfer_id)
    assert exc_info.value.transfer_id == transfer_id
    assert isinstance(exc_info.value.transfer_id, TransferId)
    duplicate_id = TransferId.new()
    with pytest.raises(pledge.DuplicateTransferId) as exc_info:
        Transfer.objects.bulk_create(
            [
                Transfer(from_account=a, to_account=b, amount=1, id=duplicate_id),
                Transfer(from_account=a, to_account=b, amount=1, id=duplicate_id),
            ]
        )
    assert exc_info.value.transfer_id == duplicate_id
    # the failed batch moved nothing
    assert Account.objects.get(pk=a.id).balance == -1


def test_updating_ledger_table_raises_immutable():
    a, b = create_account_pair()
    transfer = a.transfer(b, 1)
    with (
        pytest.raises(pledge.Immutable) as exc_info,
        sql.atomic("default"),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE pledge.transfers SET amount = 2 WHERE id = %s", [str(transfer.id)])
    assert exc_info.value.table == "transfers"


def test_translate_handles_psycopg_and_django_errors():
    with psycopg.connect(APP_DSN, autocommit=True) as conn:
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute(
                "SELECT pledge.close_account(%s::pledge.account_id)", [str(AccountId.new())]
            )
        error = pledge.translate(exc_info.value)
        assert type(error) is pledge.AccountNotFound
        assert error.message == exc_info.value.diag.message_primary
        # not ledger errors
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute("SELECT 1/0")
        assert pledge.translate(exc_info.value) is None
        with pytest.raises(psycopg.Error) as exc_info:
            conn.execute("DO $$ BEGIN RAISE EXCEPTION 'mine'; END $$")
        assert pledge.translate(exc_info.value) is None

    with pytest.raises(DatabaseError) as exc_info, connection.cursor() as cursor:
        cursor.execute("SELECT pledge.close_account(%s::pledge.account_id)", [str(AccountId.new())])
    assert type(pledge.translate(exc_info.value)) is pledge.AccountNotFound
    assert pledge.translate(ValueError("x")) is None


def test_all_error_classes_subclass_ledger_error():
    for error_class in errors.ERRORS.values():
        assert issubclass(error_class, pledge.LedgerError)


def test_closing_an_account_with_money_raises_account_not_empty():
    a, b = create_account_pair()
    a.transfer(b, Decimal("5.25"))
    with pytest.raises(pledge.AccountNotEmpty) as exc_info:
        b.close()
    assert exc_info.value.account_id == b.id
    assert exc_info.value.balance == Decimal("5.25")
    assert isinstance(exc_info.value.balance, Decimal)


def test_move_balance_out_and_close_in_one_transaction():
    a, b = create_account_pair()
    a.transfer(b, 5)
    # close() locks the row; a deposit racing in between makes it raise and roll back
    with transaction.atomic():
        b.refresh_from_db()
        b.transfer(a, b.balance)
        b.close()
    assert Account.objects.get(pk=b.pk).is_closed


class FakeDiag:
    def __init__(self, detail, constraint_name=None):
        self.message_primary = "refused"
        self.message_detail = detail
        self.constraint_name = constraint_name


class FakeDriverError(Exception):
    def __init__(self, sqlstate, diag):
        super().__init__("driver error")
        self.sqlstate = sqlstate
        self.diag = diag


@pytest.mark.parametrize(
    "detail",
    [
        None,
        "pledge.account_not_found",
        "not json",
        "[1, 2]",
        '{"code": "pledge.no_such_code"}',
        '{"code": ["pledge.immutable"]}',
        '{"code": "pledge.account_not_found"}',
    ],
)
def test_translate_ignores_unknown_or_malformed_detail(detail):
    assert pledge.translate(FakeDriverError("P0001", FakeDiag(detail))) is None


def test_translate_reads_json_detail():
    account_id = AccountId.new()
    detail = (
        f'{{"code": "pledge.account_not_empty", "account_id": "{account_id}", "balance": "1E-8"}}'
    )
    error = pledge.translate(FakeDriverError("P0001", FakeDiag(detail)))
    assert type(error) is pledge.AccountNotEmpty
    assert error.account_id == account_id and error.balance == Decimal("0.00000001")
    assert error.message == "refused"
    assert error.detail == {
        "code": "pledge.account_not_empty",
        "account_id": str(account_id),
        "balance": "1E-8",
    }


def test_unique_violation_on_transfers_pkey_is_a_transfer_conflict():
    diag = FakeDiag("Key (id)=(x) already exists.", constraint_name="transfers_pkey")
    error = pledge.translate(FakeDriverError("23505", diag))
    assert type(error) is pledge.TransferConflict
    assert error.transfer_id is None
    assert error.detail == {"code": "pledge.transfer_conflict"}
    other = FakeDiag("Key (id)=(x) already exists.", constraint_name="other_pkey")
    assert pledge.translate(FakeDriverError("23505", other)) is None


def test_translate_ignores_an_older_ledger_error_in_the_context():
    try:
        try:
            raise pledge.AccountClosed(
                "old", detail={"code": "pledge.account_closed", "account_id": None}
            )
        except pledge.LedgerError:
            raise RuntimeError("a later, unrelated error")  # noqa: B904
    except RuntimeError as exc:
        assert pledge.translate(exc) is None
