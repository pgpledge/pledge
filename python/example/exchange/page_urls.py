from django.urls import path

from . import pages

app_name = "pages"

urlpatterns = [
    path("", pages.home, name="home"),
    path("payout/", pages.payout, name="payout"),
    path("traders/new/", pages.create_trader, name="create-trader"),
    path("traders/<int:trader_id>/", pages.trader_page, name="trader"),
    path("traders/<int:trader_id>/accounts/", pages.add_account, name="add-account"),
    path("traders/<int:trader_id>/deposit/", pages.deposit, name="deposit"),
    path("traders/<int:trader_id>/send/", pages.send, name="send"),
    path("traders/<int:trader_id>/trade/", pages.trade, name="trade"),
    path("traders/<int:trader_id>/trade/preview/", pages.preview_trade, name="preview-trade"),
    path("traders/<int:trader_id>/withdraw/", pages.withdraw, name="withdraw"),
    path("traders/<int:trader_id>/close/", pages.close_wallet, name="close-wallet"),
]
