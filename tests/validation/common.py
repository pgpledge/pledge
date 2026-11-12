import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, NamedTuple

import psycopg

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_PRINTED_NOTES = 5

CREATE_TRANSFERS_SQL = """
    SELECT t.id::uuid, t.amount
    FROM pledge.create_transfers(ARRAY(
        SELECT pledge.request(u.f::pledge.account_id, u.t::pledge.account_id, u.a,
                              u.i::pledge.transfer_id)
        FROM unnest(%s::uuid[], %s::uuid[], %s::numeric[], %s::uuid[]) AS u (f, t, a, i)),
        event_at => %s::timestamptz) t
"""


class Leg(NamedTuple):
    from_account: uuid.UUID
    to_account: uuid.UUID
    amount: Decimal
    transfer_id: uuid.UUID


def encode_id(prefix: str, value: uuid.UUID) -> str:
    # text form of a pledge id as in src/id.rs: prefix + 26 Crockford base32 chars
    number, chars = value.int, []
    for _ in range(26):
        chars.append(CROCKFORD_ALPHABET[number & 31])
        number >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


def connect(dbname: str, **kwargs: Any) -> psycopg.Connection:
    return psycopg.connect(dbname=dbname, autocommit=True, **kwargs)


def create_fresh_database(name: str) -> None:
    with connect(os.environ.get("PGDATABASE", "postgres")) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    with connect(name) as conn:
        conn.execute("CREATE EXTENSION pledge")


def create_account(
    conn: psycopg.Connection,
    name: str,
    currency: str,
    *,
    allow_negative: bool = True,
    allow_positive: bool = True,
) -> uuid.UUID:
    row = conn.execute(
        "SELECT id::uuid FROM pledge.create_account(%s, %s, %s, %s)",
        [name, currency, allow_negative, allow_positive],
    ).fetchone()
    assert row is not None
    return row[0]


def create_transfers(
    conn: psycopg.Connection, legs: Sequence[Leg], event_at: str | None = None
) -> list[tuple[uuid.UUID, Decimal]]:
    columns = [[leg[index] for leg in legs] for index in range(len(Leg._fields))]
    return conn.execute(CREATE_TRANSFERS_SQL, [*columns, event_at]).fetchall()


def failed_verify_checks(conn: psycopg.Connection) -> list[tuple[str, bool, str]]:
    rows = conn.execute("SELECT check_name, ok, detail FROM pledge.verify()")
    return [row for row in rows if not row[1]]


@dataclass
class RoundResult:
    expected_balances: Mapping[uuid.UUID, Decimal]
    # scenario-specific checks: name -> number of failures, all must be 0
    checks: Mapping[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Scenario(ABC):
    name: str
    title: str
    database: str
    rounds: int = 1

    def __init__(self) -> None:
        self.stats: Counter[str] = Counter()

    @abstractmethod
    def setup(self, conn: psycopg.Connection, round_number: int) -> None: ...

    @abstractmethod
    def run(self, round_number: int) -> RoundResult: ...

    def summary(self, elapsed: float) -> dict[str, object]:
        return {}


def check_ledger(
    conn: psycopg.Connection, expected_balances: Mapping[uuid.UUID, Decimal]
) -> tuple[dict[str, int], list[str]]:
    failed_checks = failed_verify_checks(conn)
    unbalanced_currencies = conn.execute(
        "SELECT currency, sum(balance) FROM pledge.accounts"
        " GROUP BY currency HAVING sum(balance) <> 0"
    ).fetchall()
    balances = dict(conn.execute("SELECT id::uuid, balance FROM pledge.accounts"))
    wrong_balances = [
        (account, balances.get(account), expected_balances.get(account, Decimal(0)))
        for account in balances.keys() | expected_balances.keys()
        if balances.get(account) != expected_balances.get(account, Decimal(0))
    ]
    notes = [f"verify() failed: {check}" for check in failed_checks]
    notes += [
        f"{currency} balances sum to {total}"
        for currency, total in unbalanced_currencies
    ]
    notes += [
        f"account {account}: ledger {actual}, expected {expected}"
        for account, actual, expected in wrong_balances[:MAX_PRINTED_NOTES]
    ]
    checks = {
        "verify() failures": len(failed_checks),
        "currencies whose balances do not sum to 0": len(unbalanced_currencies),
        "balances != client-side expected": len(wrong_balances),
    }
    return checks, notes


def print_report(
    title: str, stats: Mapping[str, object], checks: Mapping[str, int], notes: list[str]
) -> bool:
    passed = not any(checks.values())
    width = max(map(len, [*stats, *checks]))
    print(f"\n== {title}: {'PASS' if passed else 'FAIL'}")
    for note in notes[:MAX_PRINTED_NOTES]:
        print(f"   ! {note}")
    for label, value in stats.items():
        print(f"   {label:<{width}}  {value}")
    print("   checks")
    for label, failures in checks.items():
        print(f"   {label:<{width}}  {failures}")
    return passed


def run_scenario(scenario: Scenario) -> bool:
    checks: Counter[str] = Counter()
    notes: list[str] = []
    elapsed = 0.0
    for round_number in range(scenario.rounds):
        create_fresh_database(scenario.database)
        with connect(scenario.database) as conn:
            scenario.setup(conn, round_number)
        started = time.perf_counter()
        result = scenario.run(round_number)
        elapsed += time.perf_counter() - started
        with connect(scenario.database) as conn:
            ledger_checks, ledger_notes = check_ledger(conn, result.expected_balances)
        checks.update(ledger_checks)
        checks.update(result.checks)
        notes += ledger_notes + result.notes
    stats = {**scenario.summary(elapsed), **dict(sorted(scenario.stats.items()))}
    return print_report(scenario.title, stats, checks, notes)


def run_scenarios(*scenarios: Scenario) -> int:
    results = {scenario.name: run_scenario(scenario) for scenario in scenarios}
    if len(results) > 1:
        print(
            "\n"
            + "  ".join(
                f"{name}: {'PASS' if ok else 'FAIL'}" for name, ok in results.items()
            )
        )
    return 0 if all(results.values()) else 1
