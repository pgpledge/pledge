import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from asgiref.sync import sync_to_async
from django.contrib import admin
from django.core import serializers
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.forms import ModelForm
from testapp.models import Wallet

import django_pledge as pledge
from django_pledge import (
    AccountId,
    EntryId,
    NegativeBalanceNotAllowed,
    TransferConflict,
    TransferId,
)
from django_pledge.admin import ReadOnlyAdmin
from django_pledge.json import LedgerJSONEncoder
from django_pledge.models import Account, Entry, ReadOnlyError, Transfer

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "other"])


def create_account_pair(currency="USD", **options):
    return (
        Account.objects.create(name="a", currency=currency, **options),
        Account.objects.create(name="b", currency=currency, **options),
    )


def test_search_path_is_default():
    with connection.cursor() as cur:
        cur.execute("SELECT current_user, current_setting('search_path')")
        assert cur.fetchone() == ("pledge_app", '"$user", public')


def test_migrations_up_to_date():
    call_command("makemigrations", "pledge", "--check", "--dry-run", verbosity=0)


def test_create_account():
    a = Account.objects.create(
        name="cash", currency="EUR", allow_negative_balance=False, metadata={"k": 1}
    )
    assert isinstance(a.id, AccountId)
    assert (a.name, a.currency, a.balance, a.version) == ("cash", "EUR", Decimal(0), 0)
    assert a.metadata == {"k": 1}
    assert not a.allow_negative_balance and a.allow_positive_balance
    assert not a.is_closed and a.created_at is not None
    assert a._state.db == "default" and not a._state.adding
    assert Account.objects.get(pk=a.id).name == "cash"


def test_account_save_new_instance():
    a = Account(name="x", currency="USD", allow_positive_balance=False)
    assert a._state.adding
    a.save()
    assert isinstance(a.id, AccountId) and not a._state.adding
    assert a.allow_negative_balance and not a.allow_positive_balance  # unset flag -> True
    assert a.balance == 0 and a.version == 0
    with pytest.raises(ValueError):
        Account(id=AccountId.new(), name="x", currency="USD").save()
    with pytest.raises(TypeError):
        Account.objects.create(name="x", currency="USD", balance=5)


def test_lookups_and_ordering():
    a, b = create_account_pair()
    assert Account.objects.get(pk=a.id) == a
    assert Account.objects.get(pk=str(a.id)).name == "a"
    assert Account.objects.get(id=a.id.uuid).id == a.id
    assert Account.objects.filter(pk__in=[a.id, b.id]).count() == 2
    assert Account.objects.filter(pk__gt=a.id, pk__lte=b.id).get() == b
    assert Account.objects.exclude(pk=a.id).filter(pk=b.id).exists()
    ids = list(Account.objects.values_list("id", flat=True))
    assert ids == sorted(ids)
    assert all(type(i) is AccountId for i in ids)
    assert list(Account.objects.order_by("-id").values_list("id", flat=True)) == ids[::-1]
    assert Account.objects.in_bulk([a.id]) == {a.id: a}


def test_wrong_id_type_rejected():
    with pytest.raises(TypeError):
        Account.objects.filter(pk=TransferId.new()).count()
    with pytest.raises(ValidationError):
        Account._meta.pk.to_python("plt_00000000000000000000000000")


def test_transfer_fks_and_joins():
    a, b = create_account_pair()
    t = Transfer.objects.create(
        from_account=a, to_account=b, amount=Decimal("10.25"), metadata={"order": 7}
    )
    assert isinstance(t.id, TransferId) and not t._state.adding
    assert t.from_account_id == a.id and t.to_account == b
    assert t.metadata == {"order": 7} and t.created_at is not None
    assert list(a.outgoing.all()) == [t] and list(b.incoming.all()) == [t]
    assert Transfer.objects.filter(from_account__name="a", to_account=b).get() == t
    entry = Entry.objects.select_related("account", "transfer").get(transfer=t, account=a)
    assert isinstance(entry.id, EntryId)
    assert entry.amount == Decimal("-10.25") and entry.account.name == "a" and entry.transfer == t
    assert entry.account_previous_balance == 0
    assert entry.account_current_balance == Decimal("-10.25")
    assert set(t.entries.values_list("account_id", flat=True)) == {a.id, b.id}
    assert Account.objects.filter(entries__transfer=t).count() == 2
    a.refresh_from_db()
    assert a.balance == Decimal("-10.25") and a.version == 1


def test_transfer_create_account_refs():
    a, b = create_account_pair()
    t1 = Transfer.objects.create(from_account=a.id, to_account=str(b.id), amount=1)
    t2 = Transfer.objects.create(from_account_id=b.id, to_account_id=str(a.id), amount=1)
    assert (t1.from_account_id, t1.to_account_id) == (a.id, b.id)
    assert (t2.from_account_id, t2.to_account_id) == (b.id, a.id)
    assert type(t1.to_account_id) is AccountId and t1.to_account == b
    with pytest.raises(TypeError):
        Transfer.objects.create(from_account=a, from_account_id=a.id, to_account=b, amount=1)
    with pytest.raises(TypeError):
        Transfer.objects.create(to_account=b, amount=1)
    with pytest.raises(TypeError):
        Transfer.objects.create(from_account=a, to_account=b, amount=1, created_at=None)


def test_transfer_save_new_instance():
    a, b = create_account_pair()
    day = datetime(2024, 1, 1, tzinfo=UTC)
    t = Transfer(from_account=a, to_account=b, amount=5, event_at=day, metadata={"m": 1})
    assert t._state.adding and t.id is None
    t.save()
    assert isinstance(t.id, TransferId) and not t._state.adding and t._state.db == "default"
    assert t.amount == 5 and t.event_at == day and t.metadata == {"m": 1}
    assert t.created_at is not None
    assert Transfer.objects.get(pk=t.id) == t


def test_account_transfer_and_close():
    a, b = create_account_pair()
    transfer_id = TransferId.new()
    t = a.transfer(b, Decimal("3"), id=transfer_id, metadata={"x": 1})
    assert t.id == transfer_id and t.from_account_id == a.id and t.to_account_id == b.id
    assert t.metadata == {"x": 1}
    assert a.transfer(b.id, 1).to_account_id == b.id
    b.transfer(a, 4)  # only an empty account can be closed
    assert a.close() is None
    assert a.is_closed and a.closed_at is not None and a.updated_at >= a.created_at
    assert Account.objects.get(pk=a.id).closed_at == a.closed_at


def test_history_transfers_and_balance_at():
    a, b = create_account_pair()
    t1 = a.transfer(b, 5)
    t2 = b.transfer(a, 2)
    assert [entry.amount for entry in a.history()] == [-5, 2]
    assert list(a.transfers()) == [t1, t2]
    assert a.balance_at(t1.created_at - timedelta(seconds=1)) == 0
    assert a.balance_at(t2.created_at) == -3
    assert b.balance_at(t1.created_at) in (5, 3)  # same transaction time is possible


def test_user_model_fk():
    a, b = create_account_pair()
    wallet = Wallet.objects.create(label="alice", account=a, other_id=b.id)
    wallet.refresh_from_db()
    assert wallet.account_id == a.id and wallet.account == a and wallet.other_id == b.id
    assert Wallet.objects.filter(account__currency="USD", account=a).get() == wallet
    assert list(a.wallets.all()) == [wallet]
    assert Account.objects.filter(wallets__label="alice").get() == a
    assert Wallet.objects.filter(other_id=str(b.id)).exists()


def test_serialization_and_forms():
    a, b = create_account_pair()
    t = a.transfer(b, 1)
    data = json.loads(serializers.serialize("json", [t]))[0]
    assert data["pk"] == str(t.id)
    assert data["fields"]["from_account"] == str(a.id)
    deserialized = next(serializers.deserialize("json", serializers.serialize("json", [t])))
    assert deserialized.object.id == t.id and deserialized.object.from_account_id == a.id
    with pytest.raises(ReadOnlyError):
        deserialized.save()  # loaddata never writes the views

    class WalletForm(ModelForm):
        class Meta:
            model = Wallet
            fields = ["label", "other_id"]

    form = WalletForm({"label": "x", "other_id": str(a.id).lower()})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["other_id"] == a.id
    assert not WalletForm({"label": "x", "other_id": "plt_00000000000000000000000000"}).is_valid()
    assert str(a.id) in str(WalletForm(instance=Wallet(label="y", account=a, other_id=a.id)))


def test_writes_to_stored_rows_raise_read_only_error():
    a, b = create_account_pair()
    t = a.transfer(b, 1)
    entry = t.entries.first()
    for call in (
        lambda: a.save(),
        lambda: a.save(update_fields=["name"]),
        lambda: Account(name="n", currency="USD").save(force_update=True),
        lambda: a.delete(),
        lambda: t.save(),
        lambda: t.delete(),
        lambda: entry.save(),
        lambda: entry.delete(),
        lambda: Account.objects.filter(pk=a.id).update(name="x"),
        lambda: Account.objects.filter(pk=a.id).delete(),
        lambda: Account.objects.bulk_create([Account(name="x")]),
        lambda: Account.objects.bulk_update([a], ["name"]),
        lambda: Account.objects.get_or_create(name="nope"),
        lambda: Account.objects.update_or_create(name="nope"),
        lambda: Transfer.objects.get_or_create(id=t.id),
        lambda: Transfer.objects.bulk_update([t], ["metadata"]),
        lambda: a.outgoing.update(amount=0),
        lambda: t.entries.create(amount=1),
        lambda: Entry.objects.bulk_create([]),
        lambda: Transfer.objects.all().delete(),
    ):
        with pytest.raises(ReadOnlyError):
            call()
    a.refresh_from_db()
    assert a.name == "a" and a.balance == -1


async def test_async_writes():
    a, b = await sync_to_async(create_account_pair)()
    t = await Transfer.objects.acreate(from_account=a, to_account=b, amount=2)
    [t2] = await Transfer.objects.abulk_create([Transfer(from_account=b, to_account=a, amount=1)])
    t3 = Transfer(from_account=a, to_account=b, amount=1)
    await t3.asave()
    assert {type(x.id) for x in (t, t2, t3)} == {TransferId}
    with pytest.raises(ReadOnlyError):
        await t.asave()
    with pytest.raises(ReadOnlyError):
        await Transfer.objects.filter(pk=t.id).aupdate(amount=1)
    with pytest.raises(ReadOnlyError):
        await t.adelete()


def test_read_only_admin_denies_all_writes():
    class AccountAdmin(ReadOnlyAdmin, admin.ModelAdmin):
        pass

    model_admin = AccountAdmin(Account, admin.site)
    assert not model_admin.has_add_permission(None)
    assert not model_admin.has_change_permission(None)
    assert not model_admin.has_delete_permission(None)


def test_bulk_create_populates_instances_in_order():
    a, b = create_account_pair()
    c = Account.objects.create(name="c", currency="USD")
    transfer_id = TransferId.new()
    objs = [
        Transfer(from_account=a, to_account=b, amount=10),
        Transfer(from_account_id=b.id, to_account=c, amount=Decimal("2.5"), id=transfer_id),
        Transfer(from_account_id=str(c.id), to_account_id=a.id, amount=1),
    ]
    transfers = Transfer.objects.bulk_create(objs, metadata={"batch": 1})
    assert transfers == objs and all(x is y for x, y in zip(transfers, objs, strict=True))
    assert [(t.from_account_id, t.to_account_id, t.amount) for t in transfers] == [
        (a.id, b.id, 10),
        (b.id, c.id, Decimal("2.5")),
        (c.id, a.id, 1),
    ]
    assert transfers[1].id == transfer_id and all(isinstance(t.id, TransferId) for t in transfers)
    assert all(t.metadata == {"batch": 1} and not t._state.adding for t in transfers)
    assert all(t.created_at is not None for t in transfers)
    assert Transfer.objects.bulk_create([]) == []
    with pytest.raises(TypeError):
        Transfer.objects.bulk_create([a])


def test_bulk_create_per_instance_metadata_and_event_at():
    a, b = create_account_pair()
    day = datetime(2024, 1, 1, tzinfo=UTC)
    batch_day = datetime(2023, 6, 1, tzinfo=UTC)
    transfers = Transfer.objects.bulk_create(
        [
            Transfer(from_account=a, to_account=b, amount=1, metadata={"order": 1}, event_at=day),
            Transfer(from_account=a, to_account=b, amount=2),
        ],
        metadata={"order": "batch"},
        event_at=batch_day,
    )
    assert [t.metadata for t in transfers] == [{"order": 1}, {"order": "batch"}]
    assert [t.event_at for t in transfers] == [day, batch_day]


def test_same_transfer_id_retry_is_idempotent():
    a, b = create_account_pair()
    transfer_id = TransferId.new()
    t1 = Transfer.objects.create(from_account=a, to_account=b, amount=7, id=transfer_id)
    t2 = Transfer.objects.create(from_account=a, to_account=b, amount=7, id=str(transfer_id))
    assert t1.id == t2.id == transfer_id and t1.created_at == t2.created_at
    [t3] = Transfer.objects.bulk_create(
        [Transfer(from_account=a, to_account=b, amount=7, id=transfer_id)]
    )
    assert t3.id == transfer_id and t3.created_at == t1.created_at
    a.refresh_from_db()
    assert a.balance == -7 and a.version == 1


def test_retry_same_instance_after_failure():
    a = Account.objects.create(name="a", currency="USD", allow_negative_balance=False)
    b = Account.objects.create(name="b", currency="USD")
    t = Transfer(from_account=a, to_account=b, amount=1)
    with pytest.raises(NegativeBalanceNotAllowed):
        t.save()
    assert isinstance(t.id, TransferId) and t._state.adding
    first_id = t.id
    b.transfer(a, 5)
    t.save()
    assert t.id == first_id and not t._state.adding

    objs = [Transfer(from_account=a, to_account=b, amount=100)]
    with pytest.raises(NegativeBalanceNotAllowed):
        Transfer.objects.bulk_create(objs)
    bulk_id = objs[0].id
    assert isinstance(bulk_id, TransferId) and objs[0]._state.adding
    objs[0].amount = 1
    Transfer.objects.bulk_create(objs)
    assert objs[0].id == bulk_id and not objs[0]._state.adding


def test_amount_rejects_float_and_bool():
    a, b = create_account_pair()
    for bad in (1.5, True):
        with pytest.raises(ValidationError):
            Transfer.objects.create(from_account=a, to_account=b, amount=bad)
        with pytest.raises(ValidationError):
            Transfer.objects.bulk_create([Transfer(from_account=a, to_account=b, amount=bad)])


def test_amount_accepts_strings_and_leaves_value_rules_to_the_ledger():
    a, b = create_account_pair()
    assert Transfer.objects.create(from_account=a, to_account=b, amount="1.50").amount == Decimal(
        "1.50"
    )
    for bad in (None, Decimal("NaN"), 0):
        with pytest.raises(pledge.InvalidRequest):
            Transfer.objects.create(from_account=a, to_account=b, amount=bad)


def test_metadata_accepts_decimals_and_dates():
    a, b = create_account_pair()
    when = datetime(2024, 1, 1, tzinfo=UTC)
    t = Transfer.objects.create(
        from_account=a, to_account=b, amount=1, metadata={"fee": Decimal("0.25"), "at": when}
    )
    assert Transfer.objects.get(pk=t.pk).metadata == {"fee": "0.25", "at": "2024-01-01T00:00:00Z"}


def test_writes_follow_using_and_db_manager():
    with transaction.atomic(using="other"):
        a = Account.objects.db_manager("other").create(name="a", currency="USD")
        b = Account(name="b", currency="USD")
        b.save(using="other")
        assert a._state.db == b._state.db == "other"
        t = a.transfer(b, 1)  # follows the account's database
        [t2] = Transfer.objects.using("other").bulk_create(
            [Transfer(from_account=b, to_account=a, amount=1)]
        )
        assert t._state.db == t2._state.db == "other"
        a.close()
        assert a._state.db == "other"
        # uncommitted on "other": invisible to the default connection
        assert not Account.objects.filter(pk=a.id).exists()
        assert not Transfer.objects.filter(pk__in=[t.id, t2.id]).exists()
        assert Transfer.objects.using("other").filter(pk__in=[t.id, t2.id]).count() == 2
        assert all(c.ok for c in Account.objects.db_manager("other").verify())
        transaction.set_rollback(True, using="other")
    assert not Account.objects.using("other").filter(pk=a.id).exists()


def test_atomic_inside_outer_transaction():
    a, b = create_account_pair(allow_negative_balance=False)
    with transaction.atomic():
        with pytest.raises(NegativeBalanceNotAllowed):
            a.transfer(b, 1)
        # the savepoint was rolled back; the outer transaction is still usable
        assert Account.objects.filter(pk=a.id).exists()


def test_verify_passes_on_consistent_ledger():
    create_account_pair()
    checks = Account.objects.verify()
    assert checks and all(c.ok for c in checks)
    assert checks[0]._fields == ("check_name", "ok", "detail")
    Account.objects.assert_verified()


def test_proxy_models_are_allowed():
    class Pot(Account):
        class Meta:
            proxy = True
            app_label = "testapp"

        def label(self) -> str:
            return f"pot {self.name}"

    a, _ = create_account_pair()
    assert Pot.objects.get(pk=a.pk).label() == f"pot {a.name}"


def test_multi_table_inheritance_is_rejected():
    with pytest.raises(ImproperlyConfigured, match=r"without Meta\.proxy = True"):

        class CustomerAccount(Account):
            class Meta:
                app_label = "testapp"


def test_concurrent_same_new_id_is_a_transfer_conflict():
    """Different accounts share no row locks, so the second insert waits on the primary key."""
    a, b = create_account_pair()
    c, d = create_account_pair()
    transfer_id = TransferId.new()
    seen: dict[str, BaseException] = {}

    def race() -> None:
        try:
            Transfer.objects.create(from_account=c, to_account=d, amount=1, id=transfer_id)
        except BaseException as exc:
            seen["error"] = exc
        finally:
            connections.close_all()

    with transaction.atomic(using="other"):
        Transfer.objects.db_manager("other").create(
            from_account=a, to_account=b, amount=1, id=transfer_id
        )
        racer = threading.Thread(target=race)
        racer.start()
        time.sleep(0.5)  # racer is now blocked on the primary key
    racer.join(10)
    assert isinstance(seen.get("error"), TransferConflict), seen
    assert Transfer.objects.get(pk=transfer_id).from_account_id == a.id
    assert Account.objects.get(pk=c.pk).balance == 0


def test_ledger_json_encoder_writes_decimals_in_plain_notation():
    payload = {
        "zero": Decimal("0E-8"),
        "tiny": Decimal("1E-8"),
        "at": datetime(2026, 1, 2, tzinfo=UTC),
    }
    assert json.loads(json.dumps(payload, cls=LedgerJSONEncoder)) == {
        "zero": "0.00000000",
        "tiny": "0.00000001",
        "at": "2026-01-02T00:00:00Z",
    }


def test_metadata_stores_decimals_in_plain_notation():
    a, b = create_account_pair()
    transfer = a.transfer(b, 1, metadata={"fee": Decimal("1E-8")})
    assert Transfer.objects.get(pk=transfer.pk).metadata == {"fee": "0.00000001"}
