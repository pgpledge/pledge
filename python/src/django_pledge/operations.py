from __future__ import annotations

from typing import Any

from django.contrib.postgres.operations import CreateExtension


class PledgeExtension(CreateExtension):
    """``CREATE EXTENSION IF NOT EXISTS pledge``; reversing it keeps the extension and its data."""

    def __init__(self, hints: Any = None) -> None:
        super().__init__("pledge", hints=hints)

    def database_backwards(self, *args: Any, **kwargs: Any) -> None:
        # Dropping the extension would drop every ledger table.
        pass

    def describe(self) -> str:
        return "Creates extension pledge"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        kwargs = {"hints": self.hints} if self.hints else {}
        return self.__class__.__qualname__, [], kwargs
