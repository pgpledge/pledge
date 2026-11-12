import csv
import lzma
import os
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby
from pathlib import Path

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

# official PKDD'99 release (VSE Prague, 1999), trans.asc only, xz-compressed
DATA_PATH = Path(__file__).parent / "data" / "berka" / "trans.asc.xz"
ROW_LIMIT = int(os.environ.get("BERKA_LIMIT", "0"))  # 0 means all rows
CREDIT_TYPE = "PRIJEM"
INTEREST_SYMBOLS = {"UROK", "SANKC. UROK"}
INTEREST_SLACK = {Decimal("0.10"), Decimal("-0.10")}
MAX_REORDERED_DAY_ROWS = 8
MAX_PRINTED_BROKEN_DAYS = 3


@dataclass(slots=True)
class Transaction:
    trans_id: int
    account_id: int
    date: str
    type: str
    amount: str
    balance: Decimal
    k_symbol: str
    signed_amount: Decimal
    booked_amount: Decimal
    position: int = 0
    has_oracle: bool = True

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Transaction":
        signed_amount = Decimal(row["amount"]) * (
            1 if row["type"] == CREDIT_TYPE else -1
        )
        raw_date = row["date"]  # 930101 -> 1993-01-01
        return cls(
            trans_id=int(row["trans_id"]),
            account_id=int(row["account_id"]),
            date=f"19{raw_date[:2]}-{raw_date[2:4]}-{raw_date[4:6]}",
            type=row["type"],
            amount=row["amount"],
            balance=Decimal(row["balance"]),
            k_symbol=row["k_symbol"],
            signed_amount=signed_amount,
            booked_amount=signed_amount,
        )


@dataclass
class BrokenDay:
    account_id: int
    date: str
    start_balance: Decimal
    rows: list[tuple[str, str, str]]


def find_balance_chain(
    start_balance: Decimal, day_rows: list[Transaction]
) -> list[tuple[Transaction, Decimal]] | None:
    # an order of day_rows where each balance is the previous one plus the row's amount,
    # with interest rows allowed to differ by 0.10; pairs each row with its booked amount
    if not day_rows:
        return []
    for index, row in enumerate(day_rows):
        delta = row.balance - start_balance - row.signed_amount
        if delta == 0 or (row.k_symbol in INTEREST_SYMBOLS and delta in INTEREST_SLACK):
            rest = find_balance_chain(
                row.balance, day_rows[:index] + day_rows[index + 1 :]
            )
            if rest is not None:
                return [(row, row.signed_amount + delta), *rest]
    return None


def order_by_balance_chain(
    transactions: list[Transaction], stats: Counter[str]
) -> tuple[list[Transaction], list[BrokenDay]]:
    # by date, and within each account-day in an order consistent with the balances
    by_account: dict[int, list[Transaction]] = defaultdict(list)
    for transaction in transactions:
        by_account[transaction.account_id].append(transaction)
    ordered: list[Transaction] = []
    broken_days: list[BrokenDay] = []
    for account_id, account_rows in by_account.items():
        account_rows.sort(key=lambda row: (row.date, row.trans_id))
        balance, chain_intact = Decimal(0), True
        for date, grouped in groupby(account_rows, key=lambda row: row.date):
            day_rows = list(grouped)
            chain = None
            if len(day_rows) <= MAX_REORDERED_DAY_ROWS:
                chain = find_balance_chain(balance, day_rows)
            chain_intact = chain_intact and chain is not None
            if chain is None:
                # keep trans_id order and report it
                found = day_rows
                broken_days.append(
                    BrokenDay(
                        account_id,
                        date,
                        balance,
                        [(row.type, row.amount, str(row.balance)) for row in day_rows],
                    )
                )
                stats["account-days without a consistent ordering"] += 1
                # the next day starts from start + amounts, the dataset balance is unknown
                balance += sum((row.signed_amount for row in day_rows), Decimal(0))
            else:
                found = [row for row, _ in chain]
                for row, booked_amount in chain:
                    row.booked_amount = booked_amount
                    if booked_amount != row.signed_amount:
                        stats["interest rows booked at balance delta (+/-0.10)"] += 1
                if [row.trans_id for row in found] != [
                    row.trans_id for row in day_rows
                ]:
                    stats["account-days reordered to match balances"] += 1
                balance = found[-1].balance
            for position, row in enumerate(found):
                row.position, row.has_oracle = position, chain_intact
            ordered.extend(found)
    ordered.sort(key=lambda row: (row.date, row.account_id, row.position))
    return ordered, broken_days


def load_transactions(path: Path) -> list[Transaction]:
    with lzma.open(path, "rt", newline="") as handle:
        return [
            Transaction.from_csv_row(row)
            for row in csv.DictReader(handle, delimiter=";")
        ]


@dataclass
class ExpectedEntry:
    account: uuid.UUID
    running_balance: Decimal
    dataset_balance: Decimal
    has_oracle: bool


class BerkaScenario(Scenario):
    # Replays the PKDD'99 Berka transactions as transfers to and from a world account,
    # one create_transfers call per day with event_at = that day. Checks every row's
    # ledger balance against a client-side running sum, and against the dataset's own
    # balance column wherever the account's balance chain is consistent.
    name = "berka"
    title = "Berka replay"
    database = "validate_berka"

    def __init__(self) -> None:
        super().__init__()
        self.transactions: list[Transaction] = []
        self.broken_days: list[BrokenDay] = []
        self.world = uuid.UUID(int=0)
        self.accounts: dict[int, uuid.UUID] = {}
        self.timings: dict[str, float] = {}
        self.replayed_rows = 0
        self.oracle_rows = 0
        self.database_size = ""

    def setup(self, conn: psycopg.Connection, round_number: int) -> None:
        transactions, self.broken_days = order_by_balance_chain(
            load_transactions(DATA_PATH), self.stats
        )
        self.transactions = transactions[:ROW_LIMIT] if ROW_LIMIT else transactions
        self.world = create_account(conn, "world", "CZK")
        account_ids = sorted(
            {transaction.account_id for transaction in self.transactions}
        )
        self.accounts = {
            account_id: create_account(conn, f"berka-{account_id}", "CZK")
            for account_id in account_ids
        }
        conn.execute("ANALYZE")

    def run(self, round_number: int) -> RoundResult:
        running_balances = {account: Decimal(0) for account in self.accounts.values()}
        expected_entries: dict[uuid.UUID, ExpectedEntry] = {}
        with connect(self.database) as conn:
            started = time.perf_counter()
            for date, day_rows in groupby(self.transactions, key=lambda row: row.date):
                legs = []
                for row in day_rows:
                    if row.booked_amount == 0:
                        self.stats["rows skipped (zero amount)"] += 1
                        continue
                    account = self.accounts[row.account_id]
                    running_balances[account] += row.booked_amount
                    transfer_id = uuid.UUID(int=row.trans_id)
                    expected_entries[transfer_id] = ExpectedEntry(
                        account, running_balances[account], row.balance, row.has_oracle
                    )
                    if row.booked_amount > 0:
                        legs.append(
                            Leg(self.world, account, row.booked_amount, transfer_id)
                        )
                    else:
                        legs.append(
                            Leg(account, self.world, -row.booked_amount, transfer_id)
                        )
                if legs:
                    self.stats["calls (one per day)"] += 1
                    self.stats["transfers"] += len(create_transfers(conn, legs, date))
            self.timings["load"] = time.perf_counter() - started

            started = time.perf_counter()
            entry_balances = dict(
                conn.execute(
                    "SELECT transfer_id::uuid, account_current_balance FROM pledge.entries"
                    " WHERE account_id <> %s::uuid::pledge.account_id",
                    [self.world],
                )
            )
            self.timings["verification"] = time.perf_counter() - started
            self.database_size = conn.execute(
                "SELECT pg_size_pretty(pg_database_size(current_database()))"
            ).fetchone()[0]

        oracle_entries = {
            transfer_id: entry
            for transfer_id, entry in expected_entries.items()
            if entry.has_oracle
        }
        self.replayed_rows, self.oracle_rows = (
            len(expected_entries),
            len(oracle_entries),
        )
        checks = {
            "running balance != client-side sum (ledger)": sum(
                entry_balances.get(transfer_id) != entry.running_balance
                for transfer_id, entry in expected_entries.items()
            ),
            "pledge balance != dataset balance (oracle rows)": sum(
                entry_balances.get(transfer_id) != entry.dataset_balance
                for transfer_id, entry in oracle_entries.items()
            ),
        }
        expected_balances = {
            **running_balances,
            self.world: -sum(running_balances.values(), Decimal(0)),
        }
        notes = [
            f"no consistent order: account {day.account_id} on {day.date},"
            f" start balance {day.start_balance}: {day.rows}"
            for day in self.broken_days[:MAX_PRINTED_BROKEN_DAYS]
        ]
        return RoundResult(expected_balances, checks, notes)

    def summary(self, elapsed: float) -> dict[str, object]:
        load_seconds = self.timings["load"]
        oracle_share = self.oracle_rows / max(self.replayed_rows, 1)
        return {
            "rows replayed": f"{self.replayed_rows:,} of {len(self.transactions):,}",
            "accounts": f"{len(self.accounts):,} + world",
            "load time": f"{load_seconds:.1f} s"
            f" ({self.stats['transfers'] / load_seconds:,.0f} transfers/s,"
            " 1 client, batch per day)",
            "verification time": f"{self.timings['verification']:.1f} s",
            "database size": self.database_size,
            "rows with a dataset oracle": f"{self.oracle_rows:,} ({oracle_share:.2%})",
        }


if __name__ == "__main__":
    raise SystemExit(run_scenarios(BerkaScenario()))
