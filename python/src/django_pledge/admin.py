from typing import Any


class ReadOnlyAdmin:
    """ModelAdmin mixin that only allows viewing ledger rows."""

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def get_actions(self, request: Any, action_location: Any = None) -> dict[str, Any]:
        return {}
