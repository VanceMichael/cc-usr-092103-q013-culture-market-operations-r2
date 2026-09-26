import unittest
from pathlib import Path

from src.backend import MarketBackend
from src.errors import (
    AGE_RESTRICTED,
    CAPACITY_FULL,
    CONSENT_MISSING,
    DEVICE_UNAVAILABLE,
    MATERIAL_SHORTAGE,
)
from src.plan import load_plan
from tests.helpers import make_backend

AT = "2026-09-26T10:00:00"


class BookingRulesTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))

    def test_book_returns_consent_warnings_but_reserves(self):
        # 陈小明未完成过敏提示，仍可预约月饼手作，但看板须提示
        result = self.backend.book("ses-mooncake-1", "mem-chen-zi", AT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["warnings"], ["allergy"])

    def test_duplicate_booking_is_deduplicated(self):
        first = self.backend.book("ses-poetry-1", "mem-wang-sun", AT)
        second = self.backend.book("ses-poetry-1", "mem-wang-sun", AT)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["booking_id"], second["booking_id"])
        self.assertEqual(self.backend.session_view("ses-poetry-1")["booked"], 1)

    def test_age_restriction_enforced(self):
        # 刘朵朵 7 岁，宫灯扎制 8 岁起
        result = self.backend.book("ses-lantern-1", "mem-liu-nv", AT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], AGE_RESTRICTED)

    def test_capacity_never_exceeded(self):
        backend = make_backend()  # ses-1 容量 2，三名成员
        self.assertTrue(backend.book("ses-1", "m-1", AT)["ok"])
        self.assertTrue(backend.book("ses-1", "m-2", AT)["ok"])
        result = backend.book("ses-1", "m-3", AT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], CAPACITY_FULL)
        self.assertEqual(backend.session_view("ses-1")["booked"], 2)

    def test_venue_capacity_bounds_session_capacity(self):
        # 场次标称容量 5，但所在场地只能容纳 2 人
        backend = make_backend(sessions=[{
            "id": "ses-1", "activity_id": "act-plain", "venue_id": "v-small",
            "staff_id": "st-1", "start": "2026-09-26T10:00:00",
            "end": "2026-09-26T11:00:00", "capacity": 5,
        }])
        self.assertTrue(backend.book("ses-1", "m-1", AT)["ok"])
        self.assertTrue(backend.book("ses-1", "m-2", AT)["ok"])
        result = backend.book("ses-1", "m-3", AT)
        self.assertEqual(result["reason"], CAPACITY_FULL)


class GroupBookingTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))

    def test_group_books_all_members_together(self):
        result = self.backend.book_group("ses-poetry-1", "grp-friends", AT)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["booking_ids"]), 5)  # 陈家 3 人 + 刘家 2 人
        self.assertEqual(self.backend.session_view("ses-poetry-1")["booked"], 5)

    def test_group_rejected_as_whole_when_capacity_short(self):
        backend = make_backend(
            groups=[{"id": "g-1", "name": "三家结伴", "family_ids": ["f-1"]}],
        )  # f-1 有 3 名成员，ses-1 容量 2
        result = backend.book_group("ses-1", "g-1", AT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], CAPACITY_FULL)
        self.assertEqual(backend.session_view("ses-1")["booked"], 0)  # 不留半个组

    def test_group_rejected_as_whole_when_member_too_young(self):
        # 宫灯 8 岁起，刘朵朵 7 岁，整组不约
        result = self.backend.book_group("ses-lantern-1", "grp-friends", AT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], AGE_RESTRICTED)
        self.assertEqual(self.backend.session_view("ses-lantern-1")["booked"], 0)


class CheckInRulesTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))

    def test_missing_consent_blocks_checkin_until_recorded(self):
        self.backend.book("ses-mooncake-1", "mem-chen-zi", AT)
        denied = self.backend.check_in("ses-mooncake-1", "mem-chen-zi", AT)
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], CONSENT_MISSING)
        self.assertIn("allergy", denied["detail"])
        # 物料未发出
        self.assertEqual(self.backend.batch_remaining("batch-crust"), 40)
        # 补录过敏提示后放行
        self.backend.record_consent("mem-chen-zi", "allergy", AT)
        allowed = self.backend.check_in("ses-mooncake-1", "mem-chen-zi", AT)
        self.assertTrue(allowed["ok"])
        self.assertEqual(self.backend.batch_remaining("batch-crust"), 39)
        self.assertEqual(self.backend.batch_remaining("batch-filling"), 39)

    def test_material_shortage_blocks_checkin(self):
        backend = make_backend(
            batches=[{"id": "batch-a", "name": "原料批次A", "unit": "份",
                      "initial_quantity": 1}],
        )
        self.assertTrue(backend.check_in("ses-1", "m-1", AT)["ok"])
        denied = backend.check_in("ses-1", "m-2", AT)
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], MATERIAL_SHORTAGE)
        self.assertEqual(backend.batch_remaining("batch-a"), 0)

    def test_walkup_checkin_creates_booking_within_capacity(self):
        result = self.backend.check_in("ses-rubbing-1", "mem-wang-ye", AT)
        self.assertTrue(result["ok"])
        view = self.backend.session_view("ses-rubbing-1")
        self.assertEqual(view["booked"], 1)
        self.assertEqual(self.backend.batch_remaining("batch-ricepaper"), 58)

    def test_disabled_device_blocks_checkin(self):
        self.backend.set_device_status("dev-dog-1", "disabled", "计划性停用", AT)
        result = self.backend.check_in("ses-robotdog-1", "mem-wang-sun", AT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], DEVICE_UNAVAILABLE)


class StaffOverviewTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))

    def test_overview_shows_capacity_materials_devices_and_consents(self):
        self.backend.book("ses-mooncake-1", "mem-chen-zi", AT)
        self.backend.set_device_status("dev-vr-2", "disabled", "线缆检修", AT)
        overview = self.backend.staff_overview(at="2026-09-26T09:00:00")

        mooncake = next(s for s in overview["sessions"] if s["session_id"] == "ses-mooncake-1")
        self.assertEqual(mooncake["remaining_capacity"], 11)
        self.assertEqual(
            {m["batch_id"]: m["remaining"] for m in mooncake["materials"]},
            {"batch-crust": 40, "batch-filling": 40},
        )
        self.assertEqual(
            mooncake["members_missing_consents"],
            [{"member_id": "mem-chen-zi", "name": "陈小明", "missing": ["allergy"]}],
        )

        vr = next(s for s in overview["sessions"] if s["session_id"] == "ses-vr-1")
        self.assertEqual(
            {d["device_id"]: d["status"] for d in vr["devices"]},
            {"dev-vr-1": "ready", "dev-vr-2": "disabled"},
        )
        self.assertEqual(
            [d["device_id"] for d in overview["unavailable_devices"]],
            ["dev-vr-2"],
        )

    def test_overview_filters_finished_sessions(self):
        overview = self.backend.staff_overview(at="2026-09-26T15:45:00")
        ids = [s["session_id"] for s in overview["sessions"]]
        self.assertNotIn("ses-poetry-1", ids)   # 10:45 已结束
        self.assertIn("ses-vr-1", ids)          # 15:30-16:00 进行中
        self.assertIn("ses-poetry-2", ids)      # 16:00 未开始


if __name__ == "__main__":
    unittest.main()
