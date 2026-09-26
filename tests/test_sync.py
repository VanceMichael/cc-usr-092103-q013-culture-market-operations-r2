import unittest
from pathlib import Path

from src.backend import MarketBackend
from src.errors import BOOKING_STATE
from src.plan import load_plan
from src.reports import assert_conservation
from tests.helpers import events_of, make_backend, snapshot

AT = "2026-09-26T10:00:00"


class DuplicateScanTest(unittest.TestCase):
    def test_repeat_scan_does_not_double_count(self):
        backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        first = backend.check_in("ses-mooncake-1", "mem-chen-die", AT)
        second = backend.check_in("ses-mooncake-1", "mem-chen-die", AT)  # 重复扫码
        self.assertTrue(first["ok"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["booking_id"], second["booking_id"])
        # 名额与物料都只计一次
        self.assertEqual(backend.session_view("ses-mooncake-1")["booked"], 1)
        self.assertEqual(backend.batch_remaining("batch-crust"), 39)
        self.assertEqual(backend.batch_remaining("batch-filling"), 39)
        assert_conservation(backend)

    def test_repeat_complete_is_deduplicated(self):
        backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        backend.check_in("ses-poetry-1", "mem-wang-sun", AT)
        first = backend.complete(session_id="ses-poetry-1", member_id="mem-wang-sun", at=AT)
        second = backend.complete(session_id="ses-poetry-1", member_id="mem-wang-sun", at=AT)
        self.assertTrue(first["ok"])
        self.assertTrue(second["deduplicated"])
        # 阅读记录与印章都只生成一次
        self.assertEqual(len(backend.reading_records), 1)
        self.assertEqual(len(backend.stamps), 1)


class OfflineResyncTest(unittest.TestCase):
    def test_same_event_id_applies_only_once(self):
        backend = make_backend()
        event = {
            "event_id": "terminal-7-0001",
            "kind": "check_in",
            "at": AT,
            "payload": {"session_id": "ses-1", "member_id": "m-1"},
        }
        first = backend.apply(event)
        again = backend.apply(dict(event))  # 断网恢复后重传同一事件
        self.assertEqual(first, again)
        self.assertEqual(len(backend.event_log), 1)
        self.assertEqual(backend.batch_remaining("batch-a"), 9)

    def test_offline_batch_replay_is_idempotent(self):
        backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        offline_events = [
            {"event_id": "t1-001", "kind": "book", "at": AT,
             "payload": {"session_id": "ses-mooncake-1", "member_id": "mem-chen-die",
                         "group_id": None}},
            {"event_id": "t1-002", "kind": "check_in", "at": AT,
             "payload": {"session_id": "ses-mooncake-1", "member_id": "mem-chen-die"}},
            {"event_id": "t1-003", "kind": "check_in", "at": AT,
             "payload": {"session_id": "ses-mooncake-1", "member_id": "mem-chen-die"}},
        ]
        backend.apply_batch(offline_events)
        log_len = len(backend.event_log)
        before = snapshot(backend)
        # 网络恢复后整批重传，不得产生任何新效果
        backend.apply_batch(offline_events)
        self.assertEqual(len(backend.event_log), log_len)
        self.assertEqual(snapshot(backend), before)
        assert_conservation(backend)

    def test_replay_full_log_converges_to_same_state(self):
        backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        backend.book_group("ses-poetry-1", "grp-friends", AT)
        backend.check_in("ses-poetry-1", "mem-chen-zi", AT)
        backend.complete(session_id="ses-poetry-1", member_id="mem-chen-zi", at=AT)
        backend.record_consent("mem-chen-zi", "allergy", AT)
        backend.check_in("ses-mooncake-1", "mem-chen-zi", AT)
        backend.set_device_status("dev-vr-2", "disabled", "线缆检修", AT)
        backend.change_venue("ses-poetry-2", "venue-lantern", "诗词亭临时占用", AT)

        replayed = MarketBackend(load_plan(Path("fixtures/plan.json")))
        replayed.apply_batch(events_of(backend))
        self.assertEqual(snapshot(replayed), snapshot(backend))
        assert_conservation(replayed)

    def test_out_of_order_events_stay_consistent(self):
        backend = make_backend()
        backend.book("ses-1", "m-1", AT)
        # 离线终端乱序补传：先收到“完成”，后收到“核销”
        premature = backend.complete(session_id="ses-1", member_id="m-1", at=AT)
        self.assertFalse(premature["ok"])
        self.assertEqual(premature["reason"], BOOKING_STATE)
        self.assertTrue(backend.check_in("ses-1", "m-1", AT)["ok"])
        self.assertTrue(backend.complete(session_id="ses-1", member_id="m-1", at=AT)["ok"])
        assert_conservation(backend)


if __name__ == "__main__":
    unittest.main()
