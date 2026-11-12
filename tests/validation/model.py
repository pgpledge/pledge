import dataclasses
import os
import random
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import psycopg
from common import (
    Leg,
    RoundResult,
    Scenario,
    connect,
    create_account,
    create_transfers,
    encode_id,
    failed_verify_checks,
    run_scenarios,
)
from psycopg import errors

SEED_COUNT = int(os.environ.get("MODEL_SEEDS", "20"))
OPS_PER_SEED = int(os.environ.get("MODEL_OPS", "400"))
VERIFY_EVERY_OPS = 100
OPERATION_WEIGHTS = {
    "account": 8,
    "transfer": 42,
    "batch": 22,
    "retry_same": 10,
    "retry_diff": 6,
    "dup_id": 3,
    "close": 2,
    "ghost": 3,
}

# the outcome of a request: applied rows, or the exact error message
type Outcome = list[tuple[uuid.UUID, Decimal]] | str | None


class LedgerError(Exception):
    pass


@dataclass
class ModelAccount:
    name: str
    currency: str
    allow_negative: bool
    allow_positive: bool
    balance: Decimal = Decimal(0)
    version: int = 0
    closed: bool = False

    def tag(self, account_id: uuid.UUID) -> str:
        return f"(id={encode_id('pla', account_id)}, name={self.name})"


class ReferenceLedger:
    # the ledger rules written plainly, checked in the same order as src/ledger.rs

    def __init__(self) -> None:
        self.accounts: dict[uuid.UUID, ModelAccount] = {}
        self.transfers: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, Decimal]] = {}

    def create_transfers(self, legs: Sequence[Leg]) -> list[tuple[uuid.UUID, Decimal]]:
        transfer_ids = [leg.transfer_id for leg in legs]
        for index, transfer_id in enumerate(transfer_ids):
            if transfer_id in transfer_ids[:index]:
                raise LedgerError(
                    f"Transfer id ({encode_id('plt', transfer_id)})"
                    " appears more than once in the request"
                )
        # all-or-nothing: work on copies
        accounts = {
            key: dataclasses.replace(value) for key, value in self.accounts.items()
        }
        new_transfers: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, Decimal]] = {}
        applied = []
        for from_id, to_id, amount, transfer_id in legs:
            if existing := self.transfers.get(transfer_id):
                if existing != (from_id, to_id, amount):
                    raise LedgerError(
                        f"Transfer (id={encode_id('plt', transfer_id)})"
                        " already exists with different values"
                    )
                applied.append((transfer_id, existing[2]))
                continue
            if not (amount.is_finite() and amount > 0):
                raise LedgerError(f"Amount ({amount}) must be a positive finite number")
            if from_id == to_id:
                raise LedgerError(
                    f"Cannot transfer to the same account (id={encode_id('pla', from_id)})"
                )
            # request errors first, then balance limits per leg
            for account_id in (from_id, to_id):
                if account_id not in accounts:
                    raise LedgerError(
                        f"Account (id={encode_id('pla', account_id)}) does not exist"
                    )
            for account_id in (from_id, to_id):
                if accounts[account_id].closed:
                    raise LedgerError(
                        f"Account {accounts[account_id].tag(account_id)} is closed"
                    )
            source, target = accounts[from_id], accounts[to_id]
            if source.currency != target.currency:
                raise LedgerError(
                    "Cannot transfer between different currencies"
                    f" ({source.currency} and {target.currency})"
                )
            for account_id, delta in ((from_id, -amount), (to_id, amount)):
                account = accounts[account_id]
                account.balance += delta
                account.version += 1
                if not account.allow_negative and account.balance < 0:
                    raise LedgerError(
                        f"Account {account.tag(account_id)} does not allow negative balance"
                    )
                if not account.allow_positive and account.balance > 0:
                    raise LedgerError(
                        f"Account {account.tag(account_id)} does not allow positive balance"
                    )
            new_transfers[transfer_id] = (from_id, to_id, amount)
            applied.append((transfer_id, amount))
        self.accounts = accounts
        self.transfers = {**self.transfers, **new_transfers}
        return applied

    def close(self, account_id: uuid.UUID) -> None:
        account = self.accounts.get(account_id)
        if account is None:
            raise LedgerError(
                f"Account (id={encode_id('pla', account_id)}) does not exist"
            )
        if account.closed:
            raise LedgerError(
                f"Account (id={encode_id('pla', account_id)}) is already closed"
            )
        if account.balance != 0:
            raise LedgerError(
                f"Account {account.tag(account_id)} has a non-zero balance ({account.balance})"
            )
        account.closed = True


def describe_rejection(message: str | None) -> str:
    return re.sub(r"\([^)]*\)", "(..)", message or "")


class ModelScenario(Scenario):
    # Random operations run against pledge and against ReferenceLedger. After every
    # transfer request the outcome and every account's balance, version and closed
    # state must match the model; close requests must give the same error.
    name = "model"
    title = "model-based property test"
    database = "validate_model"
    rounds = SEED_COUNT

    def __init__(self) -> None:
        super().__init__()
        self.rng = random.Random()
        self.reference = ReferenceLedger()
        self.mismatches: list[str] = []

    def setup(self, conn: psycopg.Connection, round_number: int) -> None:
        # the ledger can't be truncated by design, so each seed gets a fresh database
        self.rng = random.Random(round_number)
        self.reference = ReferenceLedger()
        self.mismatches = []

    def run(self, round_number: int) -> RoundResult:
        with connect(self.database) as conn:
            for op_number in range(OPS_PER_SEED):
                self.run_operation(conn, round_number, op_number)
        self.stats["transfers in ledger"] += len(self.reference.transfers)
        return RoundResult(
            expected_balances={
                account_id: account.balance
                for account_id, account in self.reference.accounts.items()
            },
            checks={"mismatches vs model": len(self.mismatches)},
            notes=self.mismatches,
        )

    def summary(self, elapsed: float) -> dict[str, object]:
        operations = SEED_COUNT * OPS_PER_SEED
        return {
            "seeds x ops": f"{SEED_COUNT} x {OPS_PER_SEED} = {operations} operations"
            f" in {elapsed:.1f} s"
        }

    def record_mismatch(
        self, seed: int, op_number: int, kind: str, expected: object, actual: object
    ) -> None:
        self.mismatches.append(
            f"MISMATCH seed={seed} op={op_number} {kind}\n"
            f"      model: {expected!r}\n      pledge: {actual!r}"
        )

    def run_operation(
        self, conn: psycopg.Connection, seed: int, op_number: int
    ) -> None:
        account_ids = list(self.reference.accounts)
        kind = self.rng.choices(
            list(OPERATION_WEIGHTS), list(OPERATION_WEIGHTS.values())
        )[0]
        if kind == "account" or len(account_ids) < 2:
            self.create_account(conn, f"a{len(account_ids)}")
        elif kind == "close":
            expected, actual = self.close_account(conn, self.rng.choice(account_ids))
            if expected != actual:
                self.record_mismatch(seed, op_number, kind, expected, actual)
        else:
            self.stats[f"op: {kind}"] += 1
            legs = self.random_request(kind, account_ids)
            expected, actual = self.send_transfers(conn, legs)
            if expected != actual:
                self.record_mismatch(seed, op_number, kind, expected, actual)
            elif not self.state_matches(conn):
                self.record_mismatch(
                    seed, op_number, kind, "state", "balances/versions differ"
                )
            if op_number % VERIFY_EVERY_OPS == VERIFY_EVERY_OPS - 1 and (
                failed := failed_verify_checks(conn)
            ):
                self.record_mismatch(seed, op_number, kind, "verify()", failed)

    def create_account(self, conn: psycopg.Connection, name: str) -> None:
        currency = self.rng.choice(["USD"] * 4 + ["EUR"])
        allow_negative = self.rng.random() < 0.85
        allow_positive = self.rng.random() < 0.95
        account_id = create_account(
            conn,
            name,
            currency,
            allow_negative=allow_negative,
            allow_positive=allow_positive,
        )
        self.reference.accounts[account_id] = ModelAccount(
            name, currency, allow_negative, allow_positive
        )
        self.stats["op: create account"] += 1

    def close_account(
        self, conn: psycopg.Connection, account_id: uuid.UUID
    ) -> tuple[Outcome, Outcome]:
        self.stats["op: close account"] += 1
        expected: Outcome = None
        actual: Outcome = None
        try:
            self.reference.close(account_id)
        except LedgerError as error:
            expected = str(error)
        try:
            conn.execute(
                "SELECT pledge.close_account(%s::uuid::pledge.account_id)", [account_id]
            )
        except errors.RaiseException as error:
            actual = error.diag.message_primary
        return expected, actual

    def send_transfers(
        self, conn: psycopg.Connection, legs: list[Leg]
    ) -> tuple[Outcome, Outcome]:
        expected: Outcome
        actual: Outcome
        try:
            expected = self.reference.create_transfers(legs)
        except LedgerError as error:
            expected = str(error)
        try:
            actual = [
                (transfer_id, Decimal(amount))
                for transfer_id, amount in create_transfers(conn, legs)
            ]
            self.stats["outcome: applied/replayed"] += 1
        except errors.RaiseException as error:
            actual = error.diag.message_primary
            self.stats[f"outcome: rejected: {describe_rejection(actual)}"] += 1
        return expected, actual

    def random_amount(self) -> Decimal:
        if self.rng.random() < 0.03:
            return self.rng.choice(
                [Decimal(0), Decimal("-1.50"), Decimal("NaN"), Decimal("Infinity")]
            )
        return Decimal(self.rng.randint(1, 50000)) / self.rng.choice([1, 100, 1000])

    def random_leg(self, account_ids: list[uuid.UUID]) -> Leg:
        from_id = self.rng.choice(account_ids)
        # sometimes the same account on both sides, to hit that rule
        to_id = self.rng.choice(account_ids) if self.rng.random() < 0.05 else None
        to_id = to_id or self.rng.choice(
            [other for other in account_ids if other != from_id]
        )
        return Leg(from_id, to_id, self.random_amount(), uuid.uuid4())

    def random_request(self, kind: str, account_ids: list[uuid.UUID]) -> list[Leg]:
        known_transfers = list(self.reference.transfers)
        match kind:
            case "batch":
                return [
                    self.random_leg(account_ids) for _ in range(self.rng.randint(2, 5))
                ]
            case "retry_same" | "retry_diff" if known_transfers:
                transfer_id = self.rng.choice(known_transfers)
                from_id, to_id, amount = self.reference.transfers[transfer_id]
                if kind == "retry_diff":
                    amount += 1
                legs = [Leg(from_id, to_id, amount, transfer_id)]
                if self.rng.random() < 0.5:
                    legs.append(self.random_leg(account_ids))
                self.rng.shuffle(legs)
                return legs
            case "dup_id":
                leg = self.random_leg(account_ids)
                return [leg, leg]
            case "ghost":
                ghost_id, real_id = uuid.uuid4(), self.rng.choice(account_ids)
                if self.rng.random() < 0.5:
                    return [Leg(ghost_id, real_id, Decimal(5), uuid.uuid4())]
                return [Leg(real_id, ghost_id, Decimal(5), uuid.uuid4())]
            case _:
                return [self.random_leg(account_ids)]

    def state_matches(self, conn: psycopg.Connection) -> bool:
        rows = conn.execute(
            "SELECT id::uuid, balance, version, closed_at IS NOT NULL FROM pledge.accounts"
        )
        actual = {
            account_id: (balance, version, closed)
            for account_id, balance, version, closed in rows
        }
        expected = {
            account_id: (account.balance, account.version, account.closed)
            for account_id, account in self.reference.accounts.items()
        }
        return actual == expected


if __name__ == "__main__":
    raise SystemExit(run_scenarios(ModelScenario()))
