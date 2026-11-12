#!/bin/sh
# Entry point of the `example` compose service: fresh setup on first start, then runserver.
set -eu
cd /app/example

python - <<'PY'
import os
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

dsn = os.environ["PLEDGE_DSN"]
name = conninfo_to_dict(dsn)["dbname"]
with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone():
        conn.execute(f'CREATE DATABASE "{name}"')
        open("/tmp/fresh", "w").close()
PY

python manage.py migrate --noinput
if [ -f /tmp/fresh ]; then
    for currency in EUR:1000000 USD:1000000 BTC:20; do
        python manage.py fund_liquidity "${currency%%:*}" "${currency##*:}"
    done
    python manage.py createsuperuser --noinput || true
fi
exec python manage.py runserver 0.0.0.0:8000
