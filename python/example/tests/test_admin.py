from decimal import Decimal

import pytest
from django.contrib import admin
from django.urls import reverse
from exchange import services
from exchange.models import HouseAccount, Trade, Trader, Wallet, WalletAccount, Withdrawal

from django_pledge.models import Account, Entry, Transfer

pytestmark = pytest.mark.django_db


@pytest.fixture
def ledger_activity(make_trader):
    trader = make_trader("admin-trader", EUR="100.00")
    trade, _ = services.place_trade(trader, "t-1", "EUR", "BTC", Decimal("50.00"))
    withdrawal, _ = services.request_withdrawal(trader, "w-1", "EUR", Decimal("10.00"))
    return trader, trade, withdrawal


@pytest.mark.parametrize(
    "model",
    [Account, WalletAccount, Transfer, Entry, Trader, Wallet, HouseAccount, Trade, Withdrawal],
    ids=lambda m: m.__name__,
)
def test_changelists_render_read_only(admin_client, ledger_activity, model):
    opts = model._meta
    response = admin_client.get(reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist"))
    assert response.status_code == 200
    assert response.context["cl"].result_count > 0
    model_admin = admin.site.get_model_admin(model)
    request = response.wsgi_request
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.get_actions(request) == {}
    add_url = reverse(f"admin:{opts.app_label}_{opts.model_name}_add")
    assert admin_client.get(add_url).status_code == 403


def test_wallet_account_changelist_shows_only_trader_wallets(admin_client, ledger_activity):
    trader, _trade, _withdrawal = ledger_activity
    url = reverse("admin:exchange_walletaccount_changelist")
    response = admin_client.get(url, {"q": f"trader:{trader.pk}:"})
    shown = list(response.context["cl"].result_list)
    assert set(shown) == set(Account.objects.filter(wallet__trader=trader))
    assert all(account.wallet for account in shown)
    assert trader.name in response.content.decode()


def test_detail_pages_are_view_only(admin_client, ledger_activity):
    trader, trade, withdrawal = ledger_activity
    account_url = reverse(
        "admin:pledge_account_change", args=[services.wallet(trader, "EUR").account_id]
    )
    response = admin_client.get(account_url)
    assert response.status_code == 200
    assert b'name="_save"' not in response.content
    assert admin_client.post(account_url, {"name": "renamed"}).status_code == 403
    for url in (
        reverse("admin:pledge_transfer_change", args=[trade.buy_transfer_id]),
        reverse("admin:exchange_trade_change", args=[trade.pk]),
        reverse("admin:exchange_withdrawal_change", args=[withdrawal.pk]),
    ):
        response = admin_client.get(url)
        assert response.status_code == 200, url
        assert b'name="_save"' not in response.content
