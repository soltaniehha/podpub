"""State persistence, quota ledger, and fail-safe behavior on corruption."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from notebooklm.state import (
    SCHEMA_VERSION,
    STATUS_DELIVERED,
    STATUS_GENERATING,
    QuotaLedger,
    StateCorruptError,
    StateError,
    StateStore,
)


class QuotaLedgerTest(unittest.TestCase):
    def test_remaining_counts_down_from_cap(self) -> None:
        ledger = QuotaLedger(date="2026-08-09", used=1)
        self.assertEqual(ledger.remaining(3, today="2026-08-09"), 2)

    def test_consume_increments_used(self) -> None:
        ledger = QuotaLedger(date="2026-08-09", used=0)
        ledger.consume(3, today="2026-08-09")
        ledger.consume(3, today="2026-08-09")
        self.assertEqual(ledger.used, 2)

    def test_consume_past_cap_raises(self) -> None:
        ledger = QuotaLedger(date="2026-08-09", used=3)
        with self.assertRaises(StateError):
            ledger.consume(3, today="2026-08-09")

    def test_new_day_rolls_the_counter_over(self) -> None:
        ledger = QuotaLedger(date="2026-08-09", used=3)
        self.assertEqual(ledger.remaining(3, today="2026-08-10"), 3)
        self.assertEqual(ledger.used, 0)
        self.assertEqual(ledger.date, "2026-08-10")

    def test_same_day_does_not_roll_over(self) -> None:
        ledger = QuotaLedger(date="2026-08-09", used=2)
        ledger.rollover("2026-08-09")
        self.assertEqual(ledger.used, 2)

    def test_ai_pro_cap_is_just_a_bigger_number(self) -> None:
        ledger = QuotaLedger(date="2026-08-09", used=3)
        self.assertEqual(ledger.remaining(15, today="2026-08-09"), 12)


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.addCleanup(self.tmp.cleanup)

    def test_missing_file_starts_empty(self) -> None:
        store = StateStore.load(self.path)
        self.assertEqual(store.episodes, {})
        self.assertEqual(store.quota.used, 0)

    def test_round_trip_preserves_records_and_quota(self) -> None:
        store = StateStore.load(self.path)
        record = store.upsert("k1", slug="Slug", title="Title", queue_dir="/q/ep")
        record.notebook_id = "nb_1"
        record.task_id = "task_1"
        record.status = STATUS_GENERATING
        record.sources_added = ["a.pdf"]
        store.quota.used = 2
        store.save()

        reloaded = StateStore.load(self.path)
        restored = reloaded.get("k1")
        assert restored is not None
        self.assertEqual(restored.key, "k1")
        self.assertEqual(restored.notebook_id, "nb_1")
        self.assertEqual(restored.task_id, "task_1")
        self.assertEqual(restored.status, STATUS_GENERATING)
        self.assertEqual(restored.sources_added, ["a.pdf"])
        self.assertEqual(reloaded.quota.used, 2)

    def test_save_writes_current_schema_version(self) -> None:
        store = StateStore.load(self.path)
        store.save()
        self.assertEqual(json.loads(self.path.read_text())["version"], SCHEMA_VERSION)

    def test_save_is_atomic_and_leaves_no_temp_file(self) -> None:
        store = StateStore.load(self.path)
        store.upsert("k1", slug="s", title="t", queue_dir="d")
        store.save()
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_upsert_refreshes_metadata_but_keeps_progress(self) -> None:
        store = StateStore.load(self.path)
        first = store.upsert("k1", slug="Old", title="Old Title", queue_dir="/q/old")
        first.notebook_id = "nb_1"
        again = store.upsert("k1", slug="New", title="New Title", queue_dir="/q/new")
        self.assertIs(first, again)
        self.assertEqual(again.title, "New Title")
        self.assertEqual(again.notebook_id, "nb_1")

    def test_delivered_flag(self) -> None:
        store = StateStore.load(self.path)
        record = store.upsert("k1", slug="s", title="t", queue_dir="d")
        self.assertFalse(record.is_delivered)
        record.status = STATUS_DELIVERED
        self.assertTrue(record.is_delivered)

    # ----- fail-safe -----

    def test_unparseable_state_refuses_to_run(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateCorruptError):
            StateStore.load(self.path)

    def test_non_object_state_refuses_to_run(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(StateCorruptError):
            StateStore.load(self.path)

    def test_malformed_episode_entry_refuses_to_run(self) -> None:
        self.path.write_text(json.dumps({"version": 1, "episodes": {"k": "oops"}}), encoding="utf-8")
        with self.assertRaises(StateCorruptError):
            StateStore.load(self.path)

    def test_newer_schema_refuses_to_run(self) -> None:
        self.path.write_text(json.dumps({"version": SCHEMA_VERSION + 1, "episodes": {}}),
                             encoding="utf-8")
        with self.assertRaises(StateCorruptError):
            StateStore.load(self.path)

    def test_corruption_message_names_the_file_and_the_risk(self) -> None:
        self.path.write_text("garbage", encoding="utf-8")
        with self.assertRaises(StateCorruptError) as ctx:
            StateStore.load(self.path)
        self.assertIn("state.json", str(ctx.exception))
        self.assertIn("Refusing to run", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
