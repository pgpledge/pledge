#!/usr/bin/env bash
# Benchmark pledge against pgledger on the same Postgres server with pgbench.
# Each (target, scenario, run) gets a fresh database so size growth is comparable.
#
# PROFILE picks defaults, small to large; any variable set explicitly still wins.
#   quick     ~1 min   4 clients, 5 s: "does it run, roughly how fast"
#   standard  ~4 min   20 clients, 30 s, low contention (the default)
#   scale     ~2 min   clients 1 4 16 32, 10 s each, transfer only: how throughput grows
#   full      ~20 min  low + high contention, 3 runs each: numbers worth recording
#   cpu       ~2 min   synchronous_commit=off, 1 and 8 clients: the server-side work without the
#                      commit fsync that otherwise dominates (never a production setting)
#   batch     ~3 min   1000 accounts, 8 clients, transfer vs 10- and 100-transfer calls
# Scenarios: transfer, batch (2 legs), idempotent, batchN (N transfers per call, e.g. batch100).
# SYNC=off adds synchronous_commit=off to any profile.
# ACCOUNTS and CLIENTS accept space-separated lists.
set -euo pipefail
cd "$(dirname "$0")"

PROFILE=${PROFILE:-standard}
case $PROFILE in
  quick)    d_acc=50;      d_cli=4;          d_dur=5;  d_runs=1; d_scen="transfer batch idempotent" ;;
  standard) d_acc=50;      d_cli=20;         d_dur=30; d_runs=1; d_scen="transfer batch idempotent" ;;
  scale)    d_acc=50;      d_cli="1 4 16 32"; d_dur=10; d_runs=1; d_scen="transfer" ;;
  full)     d_acc="50 10"; d_cli=20;         d_dur=30; d_runs=3; d_scen="transfer batch idempotent" ;;
  cpu)      d_acc=50;      d_cli="1 8";      d_dur=10; d_runs=1; d_scen="transfer batch idempotent"; SYNC=${SYNC:-off} ;;
  batch)    d_acc=1000;    d_cli=8;          d_dur=15; d_runs=1; d_scen="transfer batch10 batch100" ;;
  *) echo "unknown PROFILE=$PROFILE (quick|standard|scale|full|cpu|batch)" >&2; exit 2 ;;
esac
ACCOUNTS_LIST=${ACCOUNTS:-$d_acc}  # 50 = low contention, 10 = high contention
CLIENTS_LIST=${CLIENTS:-$d_cli}
THREADS=${THREADS:-4}              # capped at the client count
DURATION=${DURATION:-$d_dur}       # seconds per run
RUNS=${RUNS:-$d_runs}
SCENARIOS=${SCENARIOS:-$d_scen}
TARGETS=${TARGETS:-"pgledger pledge"}
PGLEDGER_DIR=${PGLEDGER_DIR:-../plans/reference/pgledger}
PGLEDGER_REF=${PGLEDGER_REF:-main}  # only used when PGLEDGER_DIR doesn't exist

export PGHOST=${PGHOST:-localhost} PGUSER=${PGUSER:-$USER}
export PGOPTIONS=${PGOPTIONS:-"-c client_min_messages=warning"}
SYNC=${SYNC:-on}
[[ $SYNC == off ]] && PGOPTIONS+=" -c synchronous_commit=off"
PSQL=(psql -X -q -v ON_ERROR_STOP=1)

if [[ ! -f "$PGLEDGER_DIR/pgledger.sql" ]]; then
  PGLEDGER_DIR=.cache/pgledger
  if [[ ! -d $PGLEDGER_DIR ]]; then
    git clone -q --depth 1 --branch "$PGLEDGER_REF" https://github.com/pgr0ss/pgledger "$PGLEDGER_DIR"
  fi
fi
PGLEDGER_SHA=$(git -C "$PGLEDGER_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)

setup_db() { # target db
  dropdb --if-exists --force "$2"
  createdb "$2"
  case $1 in
    pgledger)
      "${PSQL[@]}" -d "$2" \
        -f "$PGLEDGER_DIR/vendor/scoville-pgsql-ulid/ulid-to-uuid.sql" \
        -f "$PGLEDGER_DIR/vendor/scoville-pgsql-ulid/uuid-to-ulid.sql" \
        -f "$PGLEDGER_DIR/pgledger.sql" >/dev/null ;;
    pledge)
      "${PSQL[@]}" -d "$2" -c 'CREATE EXTENSION pledge' ;;
  esac
  "${PSQL[@]}" -d "$2" -v accounts="$ACCOUNTS" -f "sql/seed_$1.sql"
  # Analyze only what was seeded. ANALYZE on the still-empty transfer/entry tables records
  # reltuples = 0, and every backend then caches seq-scan plans for its FK checks until
  # autoanalyze runs (~1 min): an empty-table artifact a real ledger doesn't have.
  local acc=pgledger_accounts; [[ $1 == pledge ]] && acc=pledge.accounts
  "${PSQL[@]}" -d "$2" -c "VACUUM ANALYZE bench_accounts, $acc" -c 'CHECKPOINT'
}

p99() { sort -n | awk '{v[NR]=$1} END {if (NR==0) {print "nan"; exit} i=int(NR*0.99); if (i<1) i=1; printf "%.2f", v[i]/1000}'; }

stamp=$(date +%Y%m%d-%H%M%S)
csv="results/$stamp-$PROFILE.csv"
mkdir -p results
pgver=$("${PSQL[@]}" -d postgres -Atc 'SHOW server_version' | cut -d' ' -f1)
echo "profile,target,scenario,run,accounts,clients,duration_s,tps,lat_avg_ms,lat_p99_ms,transfers,transfers_per_s,bytes_per_transfer,check,sync,pg_version,pgledger_sha" > "$csv"

for ACCOUNTS in $ACCOUNTS_LIST; do
for CLIENTS in $CLIENTS_LIST; do
for scenario in $SCENARIOS; do
  for run in $(seq 1 "$RUNS"); do
    for target in $TARGETS; do
      threads=$(( THREADS < CLIENTS ? THREADS : CLIENTS ))
      db="bench_${target}"
      echo ">> $target / $scenario / accounts $ACCOUNTS / clients $CLIENTS / run $run" >&2
      setup_db "$target" "$db"
      before=$("${PSQL[@]}" -d "$db" -Atc 'SELECT pg_database_size(current_database())')

      script=$scenario batch=1
      if [[ $scenario =~ ^batch([0-9]+)$ ]]; then script=batchn batch=${BASH_REMATCH[1]}; fi
      logdir=$(mktemp -d)
      out=$( (cd "$logdir" && pgbench -n -d "$db" \
               -f "$OLDPWD/pgbench/${target}_${script}.sql" -D batch="$batch" \
               -D accounts="$ACCOUNTS" -c "$CLIENTS" -j "$threads" -T "$DURATION" -l) 2>&1 )
      tps=$(sed -n 's/^tps = \([0-9.]*\) (without.*/\1/p' <<<"$out")
      lat=$(sed -n 's/^latency average = \([0-9.]*\) ms.*/\1/p' <<<"$out")
      p=$(cat "$logdir"/pgbench_log.* 2>/dev/null | awk '{print $3}' | p99)
      rm -rf "$logdir"
      if [[ -z $tps ]]; then echo "$out" >&2; echo "pgbench failed for $target/$scenario" >&2; exit 1; fi

      "${PSQL[@]}" -d "$db" -c 'CHECKPOINT'
      after=$("${PSQL[@]}" -d "$db" -Atc 'SELECT pg_database_size(current_database())')
      IFS='|' read -r transfers check < <("${PSQL[@]}" -d "$db" -At -f "sql/stats_${target}.sql")
      bpt=$(( transfers > 0 ? (after - before) / transfers : 0 ))
      tfs=$(awk -v t="$transfers" -v d="$DURATION" 'BEGIN { printf "%.1f", t / d }')

      echo "$PROFILE,$target,$scenario,$run,$ACCOUNTS,$CLIENTS,$DURATION,$tps,$lat,$p,$transfers,$tfs,$bpt,$check,$SYNC,$pgver,$PGLEDGER_SHA" >> "$csv"
    done
  done
done
done
done

echo
echo "Results: bench/$csv  (Postgres $pgver, pgledger $PGLEDGER_SHA)"
echo
{ echo "target,scenario,accounts,clients,run,calls/s,transfers/s,avg ms,p99 ms,bytes/transfer,check"
  tail -n +2 "$csv" | awk -F, '{print $2","$3","$5","$6","$4","$8","$12","$9","$10","$13","$14}'
} | if command -v column >/dev/null; then column -t -s,; else tr , '\t'; fi
