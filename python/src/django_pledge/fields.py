from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django import forms
from django.core import exceptions
from django.core.exceptions import ValidationError
from django.db import models

from .ids import AccountId, EntryId, PledgeId, TransferId

__all__ = ["AccountIdField", "EntryIdField", "NumericField", "PledgeIdField", "TransferIdField"]


class PledgeIdFormField(forms.CharField):
    id_class: type[PledgeId]

    def __init__(self, *, id_class: type[PledgeId], **kwargs: Any) -> None:
        self.id_class = id_class
        super().__init__(**kwargs)

    def prepare_value(self, value: Any) -> Any:
        return str(value) if isinstance(value, PledgeId) else value

    def to_python(self, value: Any) -> PledgeId | None:
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        try:
            return self.id_class(value)
        except (TypeError, ValueError):
            raise ValidationError(
                f"Enter a valid {self.id_class.__name__} ({self.id_class.prefix}_...).",
                code="invalid",
            ) from None


class PledgeIdField(models.Field):
    """A ``pledge.*_id`` column holding PledgeId values, bound as text so no adapters are needed."""

    id_class: type[PledgeId]
    empty_strings_allowed = False
    default_error_messages = {"invalid": "“%(value)s” is not a valid %(type)s."}

    def db_type(self, connection: Any) -> str:
        return self.id_class.pg_type

    def rel_db_type(self, connection: Any) -> str:
        return self.id_class.pg_type

    def get_internal_type(self) -> str:
        return type(self).__name__

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> PledgeId | None:
        if value is None or isinstance(value, self.id_class):
            return value
        return self.id_class(value)

    def to_python(self, value: Any) -> PledgeId | None:
        if value is None or isinstance(value, self.id_class):
            return value
        if isinstance(value, (str, uuid.UUID)):
            try:
                return self.id_class(value)
            except ValueError:
                pass
        raise ValidationError(
            self.error_messages["invalid"],
            code="invalid",
            params={"value": value, "type": self.id_class.__name__},
        )

    def get_prep_value(self, value: Any) -> str | None:
        value = super().get_prep_value(value)
        if value is None:
            return None
        if not isinstance(value, (self.id_class, str, uuid.UUID)):
            raise TypeError(f"{self.name}: expected {self.id_class.__name__}, got {value!r}")
        return str(self.id_class(value))

    def value_to_string(self, obj: Any) -> str:
        value = self.value_from_object(obj)
        return "" if value is None else str(value)

    def formfield(
        self, form_class: Any = None, choices_form_class: Any = None, **kwargs: Any
    ) -> Any:
        return super().formfield(
            form_class=form_class or PledgeIdFormField,
            choices_form_class=choices_form_class,
            **{"id_class": self.id_class, **kwargs},
        )


class AccountIdField(PledgeIdField):
    description = "pledge account id"
    id_class = AccountId


class TransferIdField(PledgeIdField):
    description = "pledge transfer id"
    id_class = TransferId


class EntryIdField(PledgeIdField):
    description = "pledge entry id"
    id_class = EntryId


class NumericField(models.DecimalField):
    """Unbounded ``numeric``, as pledge stores amounts and balances."""

    description = "Unbounded decimal number"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("max_digits", None)
        kwargs.setdefault("decimal_places", None)
        super().__init__(*args, **kwargs)

    def check(self, **kwargs: Any) -> list[Any]:
        # Skip DecimalField's checks, which require max_digits and decimal_places.
        return models.Field.check(self, **kwargs)

    def deconstruct(self) -> Any:
        name, path, args, kwargs = super().deconstruct()
        for key in ("max_digits", "decimal_places"):
            if kwargs.get(key) is None:
                kwargs.pop(key, None)
        return name, path, args, kwargs

    def to_python(self, value: Any) -> Any:
        # bool and float are almost certainly bugs for money.
        if isinstance(value, (bool, float)):
            raise exceptions.ValidationError(
                f"amount must be a Decimal or int, got {type(value).__name__}: {value!r}",
                code="invalid",
            )
        # Value rules (positive, finite) are the ledger's, so NaN reaches it as InvalidRequest.
        if isinstance(value, (Decimal, int)):
            return Decimal(value)
        return super().to_python(value)

    def db_type(self, connection: Any) -> str:
        if self.max_digits is None:
            return "numeric"
        return super().db_type(connection)
