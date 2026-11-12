import os
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-example-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_pledge",
    "exchange",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"

# Same default DSN as python/tests; in production connect as a role with only the README grants.
_db = conninfo_to_dict(
    os.environ.get(
        "PLEDGE_DSN", "host=localhost port=5432 user=pledge password=pledge dbname=pledge"
    )
)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _db.get("dbname", "pledge"),
        "USER": _db.get("user", "pledge"),
        "PASSWORD": _db.get("password", ""),
        "HOST": _db.get("host", "localhost"),
        "PORT": _db.get("port", "5432"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"

# Currency code -> decimal places of its smallest unit.
EXCHANGE_CURRENCIES = {"EUR": 2, "USD": 2, "BTC": 8}
# Only bought and sold on the exchange: no bank or card deposits and payouts.
EXCHANGE_CRYPTO = {"BTC"}
# Reference prices in EUR; a real exchange would read these from a price feed.
EXCHANGE_PRICES_EUR = {"EUR": "1", "USD": "0.92", "BTC": "58000"}
EXCHANGE_FEE_RATE = "0.0025"
# What the card provider charges the broker per card deposit.
EXCHANGE_CARD_FEE_RATE = "0.015"
