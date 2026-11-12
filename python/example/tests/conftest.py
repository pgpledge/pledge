import itertools
import os
from decimal import Decimal

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connections
from psycopg.conninfo import make_conninfo

ADMIN_DSN = os.environ.get(
    "PLEDGE_TEST_DSN", "host=localhost port=5432 user=pledge password=pledge dbname=pledge"
)
# Separate from python/tests' database so both suites can run in one pytest session.
EXAMPLE_DB = os.environ.get("PLEDGE_EXAMPLE_TEST_DB", "pledge_example_test")
APP_ROLE = "pledge_app"
APP_PASSWORD = "pledge_app"

# The grants documented in README.md, nothing more.
GRANTS = f"""
GRANT USAGE ON SCHEMA pledge TO {APP_ROLE};
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA pledge TO {APP_ROLE};
GRANT SELECT ON pledge.accounts_view, pledge.transfers_view, pledge.entries_view TO {APP_ROLE};
GRANT SELECT, INSERT, UPDATE ON pledge.accounts, pledge.transfers, pledge.entries TO {APP_ROLE};
"""


def _setup_database() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{EXAMPLE_DB}" WITH (FORCE)')
        # python/tests drops and recreates this role, so reuse it rather than dropping it here.
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [APP_ROLE]).fetchone():
            conn.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
        conn.execute(f'CREATE DATABASE "{EXAMPLE_DB}" OWNER {APP_ROLE}')
    with psycopg.connect(ADMIN_DSN, dbname=EXAMPLE_DB, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION pledge")
        conn.execute(GRANTS)


def _drop_database() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{EXAMPLE_DB}" WITH (FORCE)')


def pytest_configure() -> None:
    os.environ["PLEDGE_DSN"] = make_conninfo(
        ADMIN_DSN, dbname=EXAMPLE_DB, user=APP_ROLE, password=APP_PASSWORD
    )
    from core import settings as example_settings

    settings.configure(**{k: v for k, v in vars(example_settings).items() if k.isupper()})


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker):
    _setup_database()
    with django_db_blocker.unblock():
        call_command("migrate", verbosity=0)
    yield
    # The database is owned by pledge_app; python/tests can only drop that role once it is gone.
    connections.close_all()
    _drop_database()


@pytest.fixture(autouse=True)
def house_liquidity(db):
    from exchange import services

    services.fund_liquidity("EUR", Decimal("1000000.00"))
    services.fund_liquidity("USD", Decimal("1000000.00"))
    services.fund_liquidity("BTC", Decimal("20"))


@pytest.fixture(autouse=True)
def ledger_stays_consistent(house_liquidity):
    yield
    from django_pledge.models import Account

    Account.objects.assert_verified()


unique_numbers = itertools.count(1)


@pytest.fixture
def make_trader():
    from exchange import services

    def make(name: str = "trader", **deposits: Decimal | int | str):
        trader = services.sign_up(f"{name}-{next(unique_numbers)}")
        for currency, amount in deposits.items():
            services.deposit(trader, currency, Decimal(amount))
        return trader

    return make
