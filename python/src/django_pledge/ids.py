from __future__ import annotations

import builtins
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar, Self
from uuid import UUID

__all__ = ["AccountId", "EntryId", "PledgeId", "TransferId"]

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODED_LENGTH = 26
# Crockford decoding like the extension: case-insensitive, O reads as 0, I and L as 1.
_DECODE = {char: digit for digit, char in enumerate(ALPHABET)}
_DECODE |= {char.lower(): digit for char, digit in _DECODE.items()}
_DECODE |= {"O": 0, "o": 0, "I": 1, "i": 1, "L": 1, "l": 1}


def _encode(number: int) -> str:
    chars = []
    for _ in range(_ENCODED_LENGTH):
        chars.append(ALPHABET[number & 31])
        number >>= 5
    return "".join(reversed(chars))


_last_timestamp = 0
_timestamp_lock = threading.Lock()


def _uuid7() -> UUID:
    """UUIDv7 with sub-millisecond time, increasing within a process like the extension."""
    global _last_timestamp
    nanos = time.time_ns()
    timestamp = (nanos // 1_000_000) << 12 | (nanos % 1_000_000) * 4096 // 1_000_000
    with _timestamp_lock:
        timestamp = max(timestamp, _last_timestamp + 1)
        _last_timestamp = timestamp
    raw = bytearray((timestamp >> 12).to_bytes(6, "big") + os.urandom(10))
    raw[6] = 0x70 | (timestamp >> 8) & 0x0F
    raw[7] = timestamp & 0xFF
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return uuid.UUID(bytes=bytes(raw))


class PledgeId:
    """Base of the typed ids; ids of different kinds never compare equal."""

    __slots__ = ("_uuid",)
    prefix: ClassVar[str]
    pg_type: ClassVar[str]
    _uuid: UUID

    def __init__(self, value: PledgeId | UUID | str) -> None:
        if isinstance(value, PledgeId):
            if type(value) is not type(self):
                raise TypeError(
                    f"cannot make a {type(self).__name__} from a {type(value).__name__}"
                )
            parsed = value._uuid
        elif isinstance(value, uuid.UUID):
            parsed = value
        elif isinstance(value, str):
            parsed = self._parse(value)
        else:
            raise TypeError(f"{type(self).__name__} expects a str or uuid.UUID, got {value!r}")
        object.__setattr__(self, "_uuid", parsed)

    @classmethod
    def _parse(cls, text: str) -> UUID:
        body = text[len(cls.prefix) + 1 :]
        if (
            not text.startswith(cls.prefix + "_")
            or len(body) != _ENCODED_LENGTH
            or _DECODE.get(body[0], 32) > 7
        ):
            raise ValueError(f"invalid {cls.__name__}: {text!r}")
        number = 0
        for char in body:
            digit = _DECODE.get(char)
            if digit is None:
                raise ValueError(f"invalid {cls.__name__}: {text!r}")
            number = (number << 5) | digit
        return uuid.UUID(int=number)

    @classmethod
    def new(cls) -> Self:
        """A fresh UUIDv7 id."""
        return cls(_uuid7())

    @classmethod
    def from_bytes(cls, data: builtins.bytes) -> Self:
        return cls(uuid.UUID(bytes=bytes(data)))

    @property
    def uuid(self) -> UUID:
        return self._uuid

    @property
    def bytes(self) -> builtins.bytes:
        return self._uuid.bytes

    @property
    def timestamp(self) -> datetime:
        """UTC creation time encoded in a UUIDv7 id, to the millisecond."""
        return datetime.fromtimestamp((self._uuid.int >> 80) / 1000, UTC)

    def __str__(self) -> str:
        return f"{self.prefix}_{_encode(self._uuid.int)}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r})"

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self._uuid,))

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: Any) -> Self:
        return self

    def __hash__(self) -> int:
        return hash((self.prefix, self._uuid))

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        assert isinstance(other, PledgeId)
        return self._uuid == other._uuid

    def _comparable_uuid(self, other: object) -> UUID:
        if type(other) is not type(self):
            raise TypeError(f"cannot compare {type(self).__name__} with {type(other).__name__}")
        assert isinstance(other, PledgeId)
        return other._uuid

    def __lt__(self, other: object) -> bool:
        return self._uuid < self._comparable_uuid(other)

    def __le__(self, other: object) -> bool:
        return self._uuid <= self._comparable_uuid(other)

    def __gt__(self, other: object) -> bool:
        return self._uuid > self._comparable_uuid(other)

    def __ge__(self, other: object) -> bool:
        return self._uuid >= self._comparable_uuid(other)


class AccountId(PledgeId):
    __slots__ = ()
    prefix = "pla"
    pg_type = "pledge.account_id"


class TransferId(PledgeId):
    __slots__ = ()
    prefix = "plt"
    pg_type = "pledge.transfer_id"


class EntryId(PledgeId):
    __slots__ = ()
    prefix = "ple"
    pg_type = "pledge.entry_id"
