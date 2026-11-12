from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from django_pledge.admin import ReadOnlyAdmin
from django_pledge.models import Account, Entry, Transfer

from .models import HouseAccount, Trade, Trader, Wallet, WalletAccount, Withdrawal


@admin.register(Account)
class LedgerAccountAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("id", "name", "currency", "balance", "version", "closed_at")
    list_filter = ("currency",)
    search_fields = ("name",)


@admin.register(WalletAccount)
class WalletAccountAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("name", "owner", "currency", "balance", "closed_at")
    list_filter = ("currency",)
    search_fields = ("name",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        return (
            super()
            .get_queryset(request)
            .filter(wallet__isnull=False)
            .select_related("wallet__trader")
        )

    @admin.display(description="trader")
    def owner(self, account: WalletAccount) -> str:
        return str(account.wallet.trader)


@admin.register(Transfer)
class TransferAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("id", "from_account", "to_account", "amount", "created_at", "metadata")
    list_select_related = ("from_account", "to_account")


@admin.register(Entry)
class EntryAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("account", "account_version", "amount", "account_current_balance", "transfer")
    list_select_related = ("account",)


# The exchange's own rows are created by services together with their ledger accounts or
# transfers, so the admin only shows them.
@admin.register(Trader)
class TraderAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("id", "name", "created_at")
    search_fields = ("name",)


@admin.register(Wallet)
class WalletAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("trader", "currency", "account", "opened_at")
    list_filter = ("currency",)
    list_select_related = ("trader", "account")


@admin.register(HouseAccount)
class HouseAccountAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("kind", "currency", "account")
    list_select_related = ("account",)


@admin.register(Trade)
class TradeAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = (
        "id",
        "trader",
        "sell_amount",
        "sell_currency",
        "buy_amount",
        "buy_currency",
        "fee",
        "status",
        "failure",
    )
    list_filter = ("status", "sell_currency", "buy_currency")
    list_select_related = ("trader",)


@admin.register(Withdrawal)
class WithdrawalAdmin(ReadOnlyAdmin, admin.ModelAdmin):
    list_display = ("id", "trader", "amount", "currency", "status", "failure", "transfer_id")
    list_filter = ("status", "currency")
    list_select_related = ("trader",)
