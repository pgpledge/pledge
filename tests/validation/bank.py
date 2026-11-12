import os
import random
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from decimal import Decimal

import psycopg
from common import (
    Leg,
    RoundResult,
    Scenario,
    connect,
    create_account,
    create_transfers,
    run_scenarios,
)
from psycopg import errors

ACCOUNT_COUNT = int(os.environ.get("BANK_ACCOUNTS", "12"))
WORKER_COUNT = int(os.environ.get("BANK_WORKERS", "8"))
DURATION_SECONDS = float(os.environ.get("BANK_DURATION", "30"))
KILL_EVERY_SECONDS = float(os.environ.get("BANK_KILL_EVERY", "0.15"))
WORKER_APPLICATION_NAME = "bank-worker"
LOST_ACK_PROBABILITY = 0.05


def describe_rejection(error: errors.RaiseException) -> str:
    return re.sub(r"\([^)]*\)", "(..)", error.diag.message_primary or "")


class BankScenario(Scenario):
    # Workers move money between a few accounts while a killer terminates their
    # backends and a reader checks conservation in REPEATABLE READ snapshots. A
    # request with an unknown outcome is retried with the same transfer ids, so each
    # one ends up applied exactly once or rejected.
    name = "bank"
    title = "bank test (concurrency + killed connections)"
    database = "validate_bank"

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.accounts: list[uuid.UUID] = []
        self.acked: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, Decimal]] = {}
        self.rejected: set[uuid.UUID] = set()
        self.violations: list[str] = []
        self.crashes: list[str] = []

    def count(self, label: str, amount: int = 1) -> None:
        with self.lock:
            self.stats[label] += amount

    def setup(self, conn: psycopg.Connection, round_number: int) -> None:
        # half the accounts may not go negative, so transfers out of them can be rejected
        self.accounts = [create_account(conn, "bank", "USD")] + [
            create_account(conn, f"acct{index}", "USD", allow_negative=index % 2 == 0)
            for index in range(ACCOUNT_COUNT - 1)
        ]
        conn.execute("ANALYZE")

    def run(self, round_number: int) -> RoundResult:
        targets = [(self.work, seed) for seed in range(WORKER_COUNT)]
        targets += [(self.kill,), (self.read,)]
        threads = [
            threading.Thread(target=self.guard, args=target) for target in targets
        ]
        for thread in threads:
            thread.start()
        time.sleep(DURATION_SECONDS)
        self.stop.set()
        for thread in threads:
            thread.join()

        with connect(self.database) as conn:
            stored = {
                transfer_id: (from_account, to_account, amount)
                for transfer_id, from_account, to_account, amount in conn.execute(
                    "SELECT id::uuid, from_account_id::uuid, to_account_id::uuid, amount"
                    " FROM pledge.transfers"
                )
            }
        expected_balances: Counter[uuid.UUID] = Counter()
        for from_account, to_account, amount in self.acked.values():
            expected_balances[from_account] -= amount
            expected_balances[to_account] += amount
        checks = {
            "conservation violations seen by the reader": len(self.violations),
            "acked transfers missing/different": sum(
                stored.get(transfer_id) != values
                for transfer_id, values in self.acked.items()
            ),
            "rejected transfers present": len(self.rejected & stored.keys()),
            "transfers nobody asked for": len(stored.keys() - self.acked.keys()),
            "deadlocks": self.stats["deadlocks"],
            "threads crashed": len(self.crashes),
        }
        return RoundResult(
            expected_balances, checks, self.crashes + self.violations[:3]
        )

    def summary(self, elapsed: float) -> dict[str, object]:
        return {
            "duration": f"{elapsed:.1f} s, {WORKER_COUNT} workers, {ACCOUNT_COUNT} accounts",
            "throughput": f"{self.stats['transfers ok'] / elapsed:.0f} transfers/s"
            " under fault injection",
        }

    def guard(self, target: Callable[..., None], *args: object) -> None:
        # a crashed thread fails the run instead of silently doing nothing
        try:
            target(*args)
        except Exception as error:
            with self.lock:
                self.crashes.append(f"{target.__name__} crashed: {error!r}")
            raise

    def random_request(self, rng: random.Random) -> list[Leg]:
        leg_count = 1 if rng.random() < 0.7 else rng.randint(2, 4)
        legs = []
        for _ in range(leg_count):
            from_account, to_account = rng.sample(self.accounts, 2)
            amount = Decimal(rng.randint(1, 20000)) / 100
            legs.append(Leg(from_account, to_account, amount, uuid.uuid4()))
        return legs

    def work(self, seed: int) -> None:
        rng = random.Random(seed)
        conn: psycopg.Connection | None = None
        while not self.stop.is_set():
            conn = self.send_until_definite(conn, self.random_request(rng), rng)

    def send_until_definite(
        self, conn: psycopg.Connection | None, legs: list[Leg], rng: random.Random
    ) -> psycopg.Connection | None:
        transfer_ids = [leg.transfer_id for leg in legs]
        attempt = 0
        while True:
            attempt += 1
            try:
                if conn is None or conn.closed:
                    conn = connect(
                        self.database, application_name=WORKER_APPLICATION_NAME
                    )
                committed_before = 0
                if attempt > 1:
                    # did the interrupted attempt commit, asked before retrying
                    committed_before = conn.execute(
                        "SELECT count(*) FROM pledge.transfers"
                        " WHERE id = ANY (%s::uuid[]::pledge.transfer_id[])",
                        [transfer_ids],
                    ).fetchone()[0]
                create_transfers(conn, legs)
                if attempt == 1 and rng.random() < LOST_ACK_PROBABILITY:
                    # committed, but the reply is dropped: the retry must replay it
                    self.count("lost acks simulated (committed, reply dropped)")
                    conn.close()
                    conn = None
                    continue
                self.record_ack(legs, attempt, committed_before > 0)
                return conn
            except errors.RaiseException as error:
                # a ledger rule, e.g. a no-negative account would go below 0
                with self.lock:
                    self.stats["requests rejected (ledger rule)"] += 1
                    self.stats["  " + describe_rejection(error)] += 1
                    self.rejected.update(transfer_ids)
                return conn
            except errors.DeadlockDetected:
                self.count("deadlocks")
            except psycopg.OperationalError:
                # outcome unknown: retry with the same ids
                self.count("connections killed mid-request")
                conn = None

    def record_ack(self, legs: list[Leg], attempt: int, committed_before: bool) -> None:
        with self.lock:
            self.stats["requests ok"] += 1
            self.stats["transfers ok"] += len(legs)
            if attempt > 1:
                self.stats["in-doubt requests resolved by retry"] += 1
                if committed_before:
                    self.stats[
                        "  ... had committed before the kill (retry replayed it)"
                    ] += 1
                else:
                    self.stats["  ... had not committed (retry applied it)"] += 1
            for leg in legs:
                self.acked[leg.transfer_id] = (
                    leg.from_account,
                    leg.to_account,
                    leg.amount,
                )

    def kill(self) -> None:
        with connect(self.database) as conn:
            while not self.stop.wait(KILL_EVERY_SECONDS):
                terminated = conn.execute(
                    """SELECT count(pg_terminate_backend(pid)) FROM (
                           SELECT pid FROM pg_stat_activity
                           WHERE application_name = %s AND state = 'active'
                           ORDER BY random() LIMIT 1) x""",
                    [WORKER_APPLICATION_NAME],
                ).fetchone()[0]
                self.count("backends terminated", terminated)

    def read(self) -> None:
        with connect(self.database) as conn:
            while not self.stop.is_set():
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    balance_sum, entry_sum = conn.execute(
                        "SELECT (SELECT sum(balance) FROM pledge.accounts),"
                        "       (SELECT coalesce(sum(amount), 0) FROM pledge.entries)"
                    ).fetchone()
                with self.lock:
                    self.stats["reader snapshots checked"] += 1
                    if balance_sum != 0 or entry_sum != 0:
                        self.violations.append(
                            f"sum(balance)={balance_sum} sum(entries)={entry_sum}"
                        )


if __name__ == "__main__":
    raise SystemExit(run_scenarios(BankScenario()))
