from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("exchange/", include("exchange.urls")),
    path("", include("exchange.page_urls")),
]
