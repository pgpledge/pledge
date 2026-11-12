import copy
import pickle
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from conftest import SUPER_DSN

from django_pledge import AccountId, EntryId, TransferId

# Golden ids: uuid, type and text as printed by the extension's pledge.*_id_out.
GOLDEN_IDS = [
    ("00000000-0000-0000-0000-000000000000", AccountId, "pla_00000000000000000000000000"),
    ("ffffffff-ffff-ffff-ffff-ffffffffffff", EntryId, "ple_7ZZZZZZZZZZZZZZZZZZZZZZZZZ"),
    ("0193a7d2-1c4e-7f00-8abc-0123456789ab", AccountId, "pla_01JEKX472EFW08NF014D2PF2DB"),
    ("0193a7d2-1c4e-7f00-8abc-0123456789ab", TransferId, "plt_01JEKX472EFW08NF014D2PF2DB"),
    ("019a5f3e-8b21-7c3d-9e4f-a1b2c3d4e5f6", EntryId, "ple_01K9FKX2S1FGYSWKX1PB1X9SFP"),
]

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "other"])


@pytest.mark.parametrize(("uuid_text", "id_class", "text"), GOLDEN_IDS)
def test_golden_ids_round_trip(uuid_text, id_class, text):
    assert str(id_class(uuid.UUID(uuid_text))) == text
    assert id_class(text).uuid == uuid.UUID(uuid_text)


def test_text_form_matches_extension_output():
    uuids = [uuid.uuid4() for _ in range(50)] + [AccountId.new().uuid for _ in range(50)]
    with psycopg.connect(SUPER_DSN) as conn:
        for id_class in (AccountId, TransferId, EntryId):
            rows = conn.execute(
                f"SELECT u, u::{id_class.pg_type}::text FROM unnest(%s::uuid[]) u", [uuids]
            ).fetchall()
            for value, text in rows:
                assert str(id_class(value)) == text
                assert id_class(text).uuid == value


def test_decoding_is_lenient_like_extension():
    assert AccountId("pla_01m72q37n5ec7anmh8v26v82j8").uuid == uuid.UUID(
        "01a1c571-9ea5-730e-aad2-28d88db40a48"
    )
    assert AccountId("pla_0IL0000000000000000000000O").uuid == uuid.UUID(
        "01080000-0000-0000-0000-000000000000"
    )


@pytest.mark.parametrize(
    "text",
    [
        "pla_8ZZZZZZZZZZZZZZZZZZZZZZZZZ",  # > 128 bits
        "pla_0000000000000000000000000",  # 25 chars
        "pla_000000000000000000000000000",  # 27 chars
        "pla_0000000000000000000000000U",  # U is not Crockford
        "plt_00000000000000000000000000",  # wrong prefix
        "pla00000000000000000000000000",
        "",
    ],
)
def test_invalid_text_raises_value_error(text):
    with pytest.raises(ValueError):
        AccountId(text)


def test_id_kinds_never_mix():
    shared = uuid.uuid4()
    account_id, transfer_id = AccountId(shared), TransferId(shared)
    assert account_id != transfer_id
    assert account_id == AccountId(shared)
    assert account_id != shared
    assert len({account_id, transfer_id, AccountId(shared)}) == 2
    with pytest.raises(TypeError):
        _ = account_id < transfer_id
    with pytest.raises(TypeError):
        AccountId(transfer_id)
    with pytest.raises(TypeError):
        AccountId(42)  # ty: ignore[invalid-argument-type]
    assert AccountId(account_id) == account_id


def test_ids_order_by_uuid_and_text():
    uuids = sorted(uuid.uuid4() for _ in range(20))
    ids = [AccountId(value) for value in reversed(uuids)]
    assert [account_id.uuid for account_id in sorted(ids)] == uuids
    # fixed width and an ascending alphabet keep text order equal to uuid order
    assert sorted(ids, key=str) == sorted(ids)


def test_new_is_uuidv7():
    before = datetime.now(UTC)
    new_id = TransferId.new()
    assert new_id.uuid.version == 7
    assert new_id.uuid.variant == uuid.RFC_4122
    assert before - timedelta(milliseconds=1) <= new_id.timestamp <= datetime.now(UTC)
    time.sleep(0.002)
    assert TransferId.new() > new_id
    assert len({TransferId.new() for _ in range(1000)}) == 1000


def test_ids_are_immutable_values():
    account_id = AccountId.new()
    assert repr(account_id) == f"AccountId('{account_id}')"
    assert pickle.loads(pickle.dumps(account_id)) == account_id
    assert copy.deepcopy(account_id) is account_id
    assert AccountId.from_bytes(account_id.bytes) == account_id
    with pytest.raises(AttributeError):
        account_id.foo = 1


def test_new_ids_sort_in_creation_order():
    ids = [TransferId.new() for _ in range(10_000)]
    assert sorted(ids) == ids


def test_first_character_accepts_the_same_aliases_as_the_extension():
    zero_first = "pla_0" + "1" * 25
    assert AccountId("pla_O" + "1" * 25) == AccountId(zero_first)
    assert AccountId("pla_o" + "1" * 25) == AccountId(zero_first)
    with pytest.raises(ValueError):
        AccountId("pla_8" + "1" * 25)
