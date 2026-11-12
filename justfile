default: test

# --- local (needs `cargo pgrx init`) -------------------------------------------------------

test:
    cargo pgrx test pg18

test-all:
    cargo pgrx test pg17
    cargo pgrx test pg18

lint:
    cargo fmt --check
    cargo clippy --all-targets -- -D warnings

install pg_config="pg_config":
    cargo pgrx install --release --pg-config {{pg_config}}

# local Postgres with pledge installed; see bench/README.md
bench *args:
    env {{args}} ./bench/run.sh

package:
    cargo pgrx package

# --- docker (no local Postgres or pgrx needed) ---------------------------------------------

# Postgres 18 + pledge on localhost:5432 (user/password/db: pledge)
up:
    docker compose up -d --build --wait db

down:
    docker compose down -v

# smoke test: create extension, transfers, overdraft, idempotency, verify()
check: up
    docker compose run --rm check

# pgbench pledge vs pgledger in Docker, e.g. `just docker-bench PROFILE=full` or `ACCOUNTS=10 RUNS=3`
docker-bench *args: up
    env {{args}} docker compose run --rm bench

# small to large: quick (~1 min), scale (~2 min), full (~20 min); stop whenever you've seen enough
bench-ladder: up
    PROFILE=quick docker compose run --rm bench
    PROFILE=scale docker compose run --rm bench
    PROFILE=full  docker compose run --rm bench

# cargo pgrx test + clippy + fmt in the builder image
docker-test:
    docker compose run --rm --build test

# validation suite: bank test (faults), model-based property test, Berka replay
# e.g. `just validate`, `just validate bank.py`, `BANK_DURATION=120 just validate bank.py`
validate script="run_all.py": up
    docker compose run --rm --build -e BANK_DURATION -e BANK_WORKERS -e BANK_ACCOUNTS \
      -e BANK_KILL_EVERY -e MODEL_SEEDS -e MODEL_OPS -e BERKA_LIMIT validate python -u {{script}}

psql:
    docker compose exec db psql -U pledge pledge

# open the docker db in Postico
postgres:
    docker compose up -d --wait db
    open -a "Postico 2" "postgres://pledge:pledge@localhost:${PLEDGE_PORT:-5432}/pledge" \
      || open "postgres://pledge:pledge@localhost:${PLEDGE_PORT:-5432}/pledge"

# --- docs -------------------------------------------

# live preview of the docs site
docs:
    uv run --project docs dokk serve --root docs

# static site in docs/.dokk/build
docs-build:
    uv run --project docs dokk build --root docs

# broken links, anchors and assets
docs-check:
    uv run --project docs dokk check --root docs

# exchange example on http://localhost:8000 (admin/admin), with its own database `exchange`
example: up
    docker compose --profile example up --build example

# exchange example: random traders and trades against the running example, then verify
example-simulate traders="20" trades="500":
    docker compose --profile example run --rm example python example/manage.py simulate --traders {{traders}} --trades {{trades}}

# exchange example: run the test suite (library + example)
example-test: up
    cd python && uv run pytest -q

# --- formatting -----------------------------------------------------------------------------

# pg_format the bench seed/stats SQL (brew install pgformatter). Not sql/, tests/smoke.sql or
# bench/pgbench/: pg_format breaks MODULE_PATHNAME, pg_catalog operators and pgbench :variables,
# and misindents plpgsql after RAISE
fmt-sql:
    pg_format -i -X -s 4 -u 2 -U 1 -f 0 -k --no-space-function bench/sql/*.sql
