from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "exchange"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="pages:home"), name="index"),
    path("traders/", views.traders, name="traders"),
    path("traders/<int:trader_id>/", views.portfolio, name="portfolio"),
    path("traders/<int:trader_id>/deposits/", views.deposit, name="deposit"),
    path("traders/<int:trader_id>/trades/", views.trade, name="trade"),
    path("traders/<int:trader_id>/withdrawals/", views.withdraw, name="withdraw"),
    path(
        "traders/<int:trader_id>/wallets/<str:currency>/statement/",
        views.statement,
        name="statement",
    ),
    path(
        "traders/<int:trader_id>/wallets/<str:currency>/close/",
        views.close_wallet,
        name="close-wallet",
    ),
    path("quote/", views.quote, name="quote"),
    path("house/", views.house, name="house"),
    path("house/payouts/", views.payout, name="payout"),
]
