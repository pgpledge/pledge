from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, NoReturn

from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, models, router
from django.db.models.options import Options
from django.db.models.query import RawQuerySet
from django.db.models.signals import class_prepared

from . import sql
from .errors import VerificationFailed
from .fields import AccountIdField, EntryIdField, NumericField, TransferIdField
from .ids import AccountId, TransferId
from .json import LedgerJSONEncoder
from .sql import Check

__all__ = [
    "Account",
    "AccountManager",
    "AccountQuerySet",
    "Check",
    "Entry",
    "LedgerModel",
    "ReadOnlyError",
    "ReadOnlyQuerySet",
    "Transfer",
    "TransferQuerySet",
]


class ReadOnlyError(TypeError):
    """Ledger rows are append-only: they cannot be updated or deleted through the ORM."""


def _raise_read_only(operation: str) -> NoReturn:
    raise ReadOnlyError(f"pledge ledger rows are append-only: {operation} is not supported")


class ReadOnlyQuerySet(models.QuerySet):
    """Rejects writes; async variants such as ``aupdate`` delegate here."""

    model: type[Any]

    def create(self, **kwargs: Any) -> Any:
        _raise_read_only(f"{self.model.__name__}.objects.create()")

    def bulk_create(self, *args: Any, **kwargs: Any) -> Any:
        _raise_read_only(f"{self.model.__name__}.objects.bulk_create()")

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        _raise_read_only("bulk_update()")

    def update(self, **kwargs: Any) -> NoReturn:
        _raise_read_only("update()")

    def update_or_create(self, *args: Any, **kwargs: Any) -> NoReturn:
        _raise_read_only("update_or_create()")

    def get_or_create(self, *args: Any, **kwargs: Any) -> NoReturn:
        _raise_read_only("get_or_create()")

    def delete(self) -> NoReturn:
        _raise_read_only("delete()")

    delete.queryset_only = True  # ty: ignore[missing-attribute]


ReadOnlyManager = models.Manager.from_queryset(ReadOnlyQuerySet)


class LedgerModel(models.Model):
    """Base for the ledger models: rejects updates, deletes and ``save_base()``."""

    _meta: ClassVar[Options]

    objects = ReadOnlyManager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        _raise_read_only(f"{type(self).__name__}.save()")

    def _save_table(self, *args: Any, **kwargs: Any) -> NoReturn:
        # loaddata calls Model.save_base() directly, which ends up here.
        _raise_read_only(f"{type(self).__name__}.save_base()")

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        _raise_read_only(f"{type(self).__name__}.delete()")

    def _ensure_new(self, update_fields: Any, force_update: bool) -> None:
        if not self._state.adding or update_fields is not None or force_update:
            _raise_read_only(f"saving an existing {type(self).__name__}")

    def _db_for_write(self, using: str | None) -> str:
        return using or router.db_for_write(type(self), instance=self)

    @classmethod
    def _db_value(cls, name: str, value: Any, using: str) -> Any:
        """``value`` prepared by the model field, as Django would save it."""
        return cls._meta.get_field(name).get_db_prep_save(value, connection=connections[using])

    @classmethod
    def _fetch_rows(cls, using: str, query: str, params: list[Any]) -> list[Any]:
        """Run a pledge function that returns view rows, as instances of ``cls``."""
        with sql.atomic(using):
            return list(RawQuerySet(query, model=cls, params=params, using=using))

    def _load_row(self, row: LedgerModel, using: str) -> None:
        """Copy a stored row into this instance, which is no longer new."""
        for field in self._meta.concrete_fields:
            setattr(self, field.attname, getattr(row, field.attname))
        self._state.adding = False
        self._state.db = using


class AccountQuerySet(ReadOnlyQuerySet):
    model: type[Account]

    def create(  # ty: ignore[incompatible-method-override]
        self,
        *,
        name: str,
        currency: str,
        allow_negative_balance: bool = True,
        allow_positive_balance: bool = True,
        metadata: Any = None,
    ) -> Account:
        """Create an account with ``pledge.create_account``; the database assigns its id."""
        account = self.model(
            name=name,
            currency=currency,
            allow_negative_balance=allow_negative_balance,
            allow_positive_balance=allow_positive_balance,
            metadata=metadata,
        )
        self._for_write = True
        account.save(using=self.db)
        return account


class AccountManager(models.Manager.from_queryset(AccountQuerySet)):
    def _db_for_verify(self) -> str:
        return self._db or router.db_for_write(self.model)

    def verify(self) -> list[Check]:
        """Run ``pledge.verify()``, which checks every ledger invariant from the tables."""
        using = self._db_for_verify()
        with sql.atomic(using), connections[using].cursor() as cursor:
            cursor.execute(sql.VERIFY)
            return [Check(*row) for row in cursor.fetchall()]

    def assert_verified(self) -> None:
        """Raise VerificationFailed if any ``pledge.verify()`` check fails."""
        failures = [check for check in self.verify() if not check.ok]
        if failures:
            raise VerificationFailed(failures)


class Account(LedgerModel):
    id = AccountIdField(primary_key=True)
    name = models.TextField()
    currency = models.TextField()
    balance = NumericField()
    version = models.BigIntegerField()
    allow_negative_balance = models.BooleanField()
    allow_positive_balance = models.BooleanField()
    metadata = models.JSONField(null=True, encoder=LedgerJSONEncoder)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)

    objects = AccountManager()

    class Meta:
        managed = False
        db_table = '"pledge"."accounts_view"'
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.currency})"

    def save(
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Any = None,
    ) -> None:
        """Create this new account with ``pledge.create_account``; unset balance flags mean True."""
        self._ensure_new(update_fields, force_update)
        if self.id is not None:
            raise ValueError("account ids are assigned by pledge.create_account; leave id unset")
        using = self._db_for_write(using)
        params = [
            self.name,
            self.currency,
            self.allow_negative_balance is not False,
            self.allow_positive_balance is not False,
            self._db_value("metadata", self.metadata, using),
        ]
        [row] = self._fetch_rows(using, sql.CREATE_ACCOUNT, params)
        self._load_row(row, using)

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None

    def transfer(
        self,
        to: Account | AccountId | str,
        amount: Decimal | int,
        *,
        id: TransferId | str | None = None,
        event_at: datetime | None = None,
        metadata: Any = None,
    ) -> Transfer:
        """Move ``amount`` from this account to ``to``; see ``Transfer.objects.create()``."""
        return Transfer.objects.db_manager(self._state.db).create(
            from_account=self,
            to_account=to,
            amount=amount,
            id=id,
            event_at=event_at,
            metadata=metadata,
        )

    def close(self) -> None:
        """Close the account with ``pledge.close_account`` and reload this instance."""
        using = self._db_for_write(self._state.db)
        [row] = self._fetch_rows(using, sql.CLOSE_ACCOUNT, [str(sql.coerce_account_id(self))])
        self._load_row(row, using)

    def history(self) -> ReadOnlyQuerySet:
        """This account's entries, oldest first."""
        return Entry.objects.using(self._state.db).filter(account=self).order_by("account_version")

    def transfers(self) -> TransferQuerySet:
        """Transfers in or out of this account, in the order they hit it."""
        return (
            Transfer.objects.using(self._state.db)
            .filter(entries__account=self)
            .order_by("entries__account_version")
        )

    def balance_at(self, when: datetime) -> Decimal:
        """Balance after the last entry created at or before ``when``."""
        balance = (
            self.history()
            .filter(created_at__lte=when)
            .order_by("-account_version")
            .values_list("account_current_balance", flat=True)
            .first()
        )
        return Decimal(0) if balance is None else balance


def _account_ref_kwargs(name: str, instance: Any, raw_id: Any) -> dict[str, Any]:
    """Model kwargs for ``name=`` or ``name_id=``, accepting instances or ids."""
    if instance is not None and raw_id is not None:
        raise TypeError(f"pass {name} or {name}_id, not both")
    ref = instance if instance is not None else raw_id
    if ref is None:
        raise TypeError(f"missing {name}")
    if isinstance(ref, Account):
        return {name: ref}
    return {f"{name}_id": sql.coerce_account_id(ref)}


class TransferQuerySet(ReadOnlyQuerySet):
    model: type[Transfer]

    def create(  # ty: ignore[incompatible-method-override]
        self,
        *,
        amount: Decimal | int,
        from_account: Account | AccountId | str | None = None,
        to_account: Account | AccountId | str | None = None,
        from_account_id: AccountId | str | None = None,
        to_account_id: AccountId | str | None = None,
        id: TransferId | str | None = None,
        event_at: datetime | None = None,
        metadata: Any = None,
    ) -> Transfer:
        """Move ``amount`` with ``pledge.create_transfer``; pass ``id`` for idempotent retries."""
        transfer = self.model(
            **_account_ref_kwargs("from_account", from_account, from_account_id),
            **_account_ref_kwargs("to_account", to_account, to_account_id),
            amount=amount,
            id=id,
            event_at=event_at,
            metadata=metadata,
        )
        self._for_write = True
        transfer.save(using=self.db)
        return transfer

    def bulk_create(  # ty: ignore[incompatible-method-override]
        self,
        objs: Iterable[Transfer],
        *,
        event_at: datetime | None = None,
        metadata: Any = None,
    ) -> list[Transfer]:
        """Create all transfers in one atomic ``pledge.create_transfers`` call.

        ``event_at`` and ``metadata`` apply to transfers that set none. Missing ids are filled in
        first, so retrying the same instances is idempotent. Returns ``objs``, populated in order.
        """
        objs = list(objs)
        if not objs:
            return objs
        self._for_write = True
        using = self.db
        columns: tuple[list[Any], ...] = ([], [], [], [], [], [])
        for obj in objs:
            if not isinstance(obj, self.model):
                raise TypeError(f"bulk_create() expects Transfer instances, got {obj!r}")
            obj.id = sql.ensure_transfer_id(obj.id)  # ty: ignore[invalid-assignment]
            values = (
                sql.coerce_account_id(obj.from_account_id).uuid,
                sql.coerce_account_id(obj.to_account_id).uuid,
                obj._db_value("amount", obj.amount, using),
                obj.id.uuid,
                obj.event_at,
                obj._db_value("metadata", obj.metadata, using),
            )
            for column, value in zip(columns, values, strict=True):
                column.append(value)
        rows = self.model._fetch_rows(
            using,
            sql.CREATE_TRANSFERS,
            [*columns, event_at, self.model._db_value("metadata", metadata, using)],
        )
        for obj, row in zip(objs, rows, strict=True):
            obj._load_row(row, using)
        return objs

    async def abulk_create(  # ty: ignore[incompatible-method-override]
        self,
        objs: Iterable[Transfer],
        *,
        event_at: datetime | None = None,
        metadata: Any = None,
    ) -> list[Transfer]:
        return await sync_to_async(self.bulk_create)(objs, event_at=event_at, metadata=metadata)


TransferManager = models.Manager.from_queryset(TransferQuerySet)


class Transfer(LedgerModel):
    id = TransferIdField(primary_key=True)
    from_account = models.ForeignKey(
        Account, models.DO_NOTHING, db_constraint=False, related_name="outgoing"
    )
    to_account = models.ForeignKey(
        Account, models.DO_NOTHING, db_constraint=False, related_name="incoming"
    )
    amount = NumericField()
    created_at = models.DateTimeField()
    event_at = models.DateTimeField()
    metadata = models.JSONField(null=True, encoder=LedgerJSONEncoder)

    objects = TransferManager()

    class Meta:
        managed = False
        db_table = '"pledge"."transfers_view"'
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.id}: {self.amount} {self.from_account_id} -> {self.to_account_id}"

    def save(
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Any = None,
    ) -> None:
        """Create this new transfer with ``pledge.create_transfer``.

        A missing id is set on the instance before the call, so saving again after a failure
        retries idempotently.
        """
        self._ensure_new(update_fields, force_update)
        using = self._db_for_write(using)
        from_id = sql.coerce_account_id(self.from_account_id)
        to_id = sql.coerce_account_id(self.to_account_id)
        amount = self._db_value("amount", self.amount, using)
        self.id = sql.ensure_transfer_id(self.id)  # ty: ignore[invalid-assignment]
        params = [
            str(from_id),
            str(to_id),
            amount,
            self.event_at,
            self._db_value("metadata", self.metadata, using),
            str(self.id),
        ]
        [row] = self._fetch_rows(using, sql.CREATE_TRANSFER, params)
        self._load_row(row, using)


class Entry(LedgerModel):
    id = EntryIdField(primary_key=True)
    account = models.ForeignKey(
        Account, models.DO_NOTHING, db_constraint=False, related_name="entries"
    )
    transfer = models.ForeignKey(
        Transfer, models.DO_NOTHING, db_constraint=False, related_name="entries"
    )
    amount = NumericField()
    account_previous_balance = NumericField()
    account_current_balance = NumericField()
    account_version = models.BigIntegerField()
    created_at = models.DateTimeField()
    event_at = models.DateTimeField()
    metadata = models.JSONField(null=True, encoder=LedgerJSONEncoder)

    class Meta:
        managed = False
        db_table = '"pledge"."entries_view"'
        ordering = ["account_id", "account_version"]
        verbose_name_plural = "entries"

    def __str__(self) -> str:
        return f"{self.account_id} v{self.account_version}: {self.amount}"


def _reject_multi_table_inheritance(sender: type[models.Model], **kwargs: Any) -> None:
    # Multi-table inheritance would write parent rows through the ORM; proxies are fine.
    for base in (Account, Transfer, Entry):
        if sender is not base and issubclass(sender, base) and not sender._meta.proxy:
            raise ImproperlyConfigured(
                f"{sender.__name__} subclasses pledge's {base.__name__} without Meta.proxy = True. "
                f"Use a proxy model, or a model with OneToOneField('pledge.{base.__name__}', "
                "on_delete=models.PROTECT, db_constraint=False) for extra fields."
            )


class_prepared.connect(_reject_multi_table_inheritance)
