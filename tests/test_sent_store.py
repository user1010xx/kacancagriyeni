import json
from datetime import date, timedelta
from pathlib import Path

from sent_store import SentStore


def test_legacy_list_migration(tmp_path: Path):
    path = tmp_path / "sent_calls.json"
    old_key = f"1|905551112233|{date.today().strftime('%d.%m.%Y')}|10:00:00|Gelen"
    path.write_text(json.dumps([old_key]), encoding="utf-8")

    store = SentStore(path)
    assert store.is_complete(old_key)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "completed" in data
    assert old_key in data["completed"]


def test_complete_any_and_mark_keys(tmp_path: Path):
    store = SentStore(tmp_path / "sent_calls.json")
    keys = ["k1", "k1-legacy"]
    store.mark_complete_keys(keys)
    assert store.is_complete_any(["k1-legacy", "missing"])
    assert store.is_complete("k1")


def test_group_notified_and_complete(tmp_path: Path):
    store = SentStore(tmp_path / "sent_calls.json")
    key = "k1"

    store.mark_group_notified(key)
    store.mark_private_notified(key)
    assert store.is_group_notified(key)
    assert store.is_private_notified(key)
    assert not store.is_complete(key)

    store.mark_complete(key)
    assert store.is_complete(key)
    assert not store.is_group_notified(key)
    assert not store.is_private_notified(key)


def test_private_notified_schema_migration(tmp_path: Path):
    path = tmp_path / "sent_calls.json"
    path.write_text(
        json.dumps({"completed": [], "group_notified": ["k1"]}),
        encoding="utf-8",
    )

    store = SentStore(path)
    assert store.is_group_notified("k1")
    assert not store.is_private_notified("k1")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "private_notified" in data


def test_purge_old(tmp_path: Path):
    store = SentStore(tmp_path / "sent_calls.json", max_age_days=30)
    old_date = (date.today() - timedelta(days=60)).strftime("%d.%m.%Y")
    old_key = f"1|905551112233|{old_date}|10:00:00|Gelen"
    new_key = f"2|905551112233|{date.today().strftime('%d.%m.%Y')}|11:00:00|Gelen"

    store.add_many([old_key, new_key])
    removed = store.purge_old()
    assert removed == 1
    assert store.is_complete(new_key)
    assert not store.is_complete(old_key)


def test_unmark_for_dates(tmp_path: Path):
    store = SentStore(tmp_path / "sent_calls.json")
    d1 = date(2026, 7, 20)
    d2 = date(2026, 7, 21)
    d3 = date(2026, 7, 19)
    k1 = f"905551112233|{d1.strftime('%d.%m.%Y')}|10:00:00|1000"
    k2 = f"905551112244|{d2.strftime('%d.%m.%Y')}|11:00:00|1000"
    k3 = f"905551112255|{d3.strftime('%d.%m.%Y')}|12:00:00|1000"

    store.mark_complete(k1)
    store.mark_group_notified(k2)
    store.mark_private_notified(k2)
    store.mark_complete(k3)

    removed = store.unmark_for_dates({d1, d2})
    assert removed == 3
    assert not store.is_complete(k1)
    assert not store.is_group_notified(k2)
    assert not store.is_private_notified(k2)
    assert store.is_complete(k3)