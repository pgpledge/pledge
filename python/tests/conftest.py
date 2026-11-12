import os

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ADMIN_DSN = os.environ.get(
    "PLEDGE_TEST_DSN", "host=localhost port=5432 user=pledge password=pledge dbname=pledge"
)
TEST_DB = os.environ.get("PLEDGE_TEST_DB", "pledge_python_test")
# Not named "pledge": a role named like the schema would get it on its search_path.
APP_ROLE = "pledge_app"
APP_PASSWORD = "pledge_app"

# Keep in sync with the GRANT block in README.md.
GRANTS = f"""
GRANT USAGE ON SCHEMA pledge TO {APP_ROLE};
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA pledge TO {APP_ROLE};
GRANT SELECT ON pledge.accounts_view, pledge.transfers_view, pledge.entries_view TO {APP_ROLE};
GRANT SELECT, INSERT, UPDATE ON pledge.accounts, pledge.transfers, pledge.entries TO {APP_ROLE};
"""


def _setup_database() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
        conn.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
        # The app role owns the database but not the extension, as in production.
        conn.execute(f'CREATE DATABASE "{TEST_DB}" OWNER {APP_ROLE}')
    with psycopg.connect(ADMIN_DSN, dbname=TEST_DB, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION pledge")
        conn.execute(GRANTS)


_admin_params = conninfo_to_dict(ADMIN_DSN)
APP_DSN = make_conninfo(ADMIN_DSN, dbname=TEST_DB, user=APP_ROLE, password=APP_PASSWORD)
SUPER_DSN = make_conninfo(ADMIN_DSN, dbname=TEST_DB)

_DATABASE = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": TEST_DB,
    "USER": APP_ROLE,
    "PASSWORD": APP_PASSWORD,
    "HOST": _admin_params.get("host", "localhost"),
    "PORT": _admin_params.get("port", "5432"),
    # PLEDGE_SERVER_SIDE_BINDING=1 runs the suite with the setting the docs recommend for speed.
    "OPTIONS": {"server_side_binding": os.environ.get("PLEDGE_SERVER_SIDE_BINDING") == "1"},
}


def pytest_configure() -> None:
    settings.configure(
        # "other" is a second connection to the same database.
        DATABASES={"default": _DATABASE, "other": dict(_DATABASE)},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django_pledge",
            "testapp",
        ],
        USE_TZ=True,
        DEBUG=("VERBOSE" in os.environ),
    )


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker):
    _setup_database()
    with django_db_blocker.unblock():
        call_command("migrate", run_syncdb=True, verbosity=0)
