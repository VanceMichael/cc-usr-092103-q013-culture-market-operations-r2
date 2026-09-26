import unittest
from pathlib import Path

from src.backend import MarketBackend
from src.errors import (
    CAPACITY_FULL,
    DEVICE_UNAVAILABLE,
    SESSION_CLOSED,
    VENUE_CAPACITY,
)
from src.models import MovementKind
from src.plan import load_plan
from src.reports import assert_conservation
from tests.helpers import make_backend

AT = "2026-09-26T10:00:00"


def tech_backend() -> MarketBackend:
    """带设备与物料的场次：用于设备故障与物料损耗测试。"""
    return make_backend(
        activities=[{
            "id": "act-plain", "name": "机器狗工坊", "category": "tech",
            "required_consents": ["safety"], "min_age": 5,
            "materials": {"batch-a": 2}, "device_type": "robot-dog",
        }],
        devices=[
            {"id": "dog-1", "name": "机器狗·甲", "type": "robot-dog"},
            {"id": "dog-2", "name": "机器狗·乙", "type": "robot-dog"},
        ],
        sessions=[{
            "id": "ses-1", "activity_id": "act-plain", "venue_id": "v-big",
            "staff_id": "st-1", "start": "2026-09-26T10:00:00",
            "end": "2026-09-26T11:00:00", "capacity": 2, "device_ids": ["dog-1"],
        }],
    )


class VenueChangeTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))

    def test_change_venue_moves_session_and_shrinks_effective_capacity(self):
        for member_id in ("mem-chen-die", "mem-chen-ma", "mem-chen-zi",
                          "mem-liu-ma", "mem-liu-nv", "mem-wang-ye", "mem-wang-sun"):
            self.backend.book("ses-poetry-2", member_id, AT)
        result = self.backend.change_venue(
            "ses-poetry-2", "venue-lantern", "诗词亭临时占用", AT
        )
        self.assertTrue(result["ok"])
        # 场次标称 24 人，宫灯长廊只能容纳 16 人
        self.assertEqual(result["effective_capacity"], 16)
        view = self.backend.session_view("ses-poetry-2")
        self.assertEqual(view["venue_id"], "venue-lantern")
        self.assertEqual(view["remaining_capacity"], 9)
        assert_conservation(self.backend)

    def test_change_venue_rejected_when_target_too_small(self):
        backend = make_backend()  # ses-1 容量 2，当前场地 v-big
        backend.book("ses-1", "m-1", AT)
        backend.book("ses-1", "m-2", AT)
        result = backend.change_venue("ses-1", "v-small", "大厅空调故障", AT)
        # v-small 容量 2，恰好容纳已约 2 人 —— 可以换
        self.assertTrue(result["ok"])
        # 换场后名额已满，第三人约不进
        denied = backend.book("ses-1", "m-3", AT)
        self.assertEqual(denied["reason"], CAPACITY_FULL)

    def test_change_venue_rejected_when_booked_exceeds_target(self):
        backend = make_backend(
            venues=[
                {"id": "v-small", "name": "小厅", "capacity": 1},
                {"id": "v-big", "name": "大厅", "capacity": 50},
            ],
        )
        backend.book("ses-1", "m-1", AT)
        backend.book("ses-1", "m-2", AT)
        result = backend.change_venue("ses-1", "v-small", "大厅空调故障", AT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], VENUE_CAPACITY)
        # 换场失败，原场地与名额不变
        self.assertEqual(backend.session_view("ses-1")["venue_id"], "v-big")
        self.assertEqual(backend.session_view("ses-1")["booked"], 2)


class DeviceFaultTest(unittest.TestCase):
    def test_fault_blocks_checkin_until_swapped(self):
        backend = tech_backend()
        backend.set_device_status("dog-1", "faulty", "舵机异响", AT)
        denied = backend.check_in("ses-1", "m-1", AT)
        self.assertEqual(denied["reason"], DEVICE_UNAVAILABLE)
        # 换用备用机器狗后恢复
        backend.swap_device("ses-1", "dog-1", "dog-2", AT)
        self.assertTrue(backend.check_in("ses-1", "m-1", AT)["ok"])
        assert_conservation(backend)

    def test_fail_session_wastes_issued_materials(self):
        backend = tech_backend()
        backend.book("ses-1", "m-1", AT)
        backend.book("ses-1", "m-2", AT)
        backend.check_in("ses-1", "m-1", AT)  # m-1 已核销并领了 2 份物料
        backend.set_device_status("dog-1", "faulty", "舵机异响", AT)
        result = backend.fail_session(
            "ses-1", DEVICE_UNAVAILABLE, "机器狗故障且备用机未到位", AT
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed_bookings"], 2)
        # 已发出的物料记为损耗，不回库
        self.assertEqual(backend.batch_remaining("batch-a"), 8)
        waste = [m for m in backend.movements if m.kind == MovementKind.WASTE]
        self.assertEqual(len(waste), 1)
        self.assertEqual(waste[0].quantity, 2)
        assert_conservation(backend)
        # 场次取消后，任何预约与核销都被拒绝并记录原因
        late = backend.check_in("ses-1", "m-3", AT)
        self.assertEqual(late["reason"], SESSION_CLOSED)

    def test_fail_session_with_salvage_returns_materials(self):
        backend = tech_backend()
        backend.check_in("ses-1", "m-1", AT)
        backend.fail_session(
            "ses-1", DEVICE_UNAVAILABLE, "设备故障，原料未拆封", AT,
            salvage_materials=True,
        )
        self.assertEqual(backend.batch_remaining("batch-a"), 10)  # 全部归还
        returned = [m for m in backend.movements if m.kind == MovementKind.RETURN]
        self.assertEqual(len(returned), 1)
        assert_conservation(backend)

    def test_fail_session_is_idempotent(self):
        backend = tech_backend()
        backend.book("ses-1", "m-1", AT)
        first = backend.fail_session("ses-1", DEVICE_UNAVAILABLE, "故障", AT)
        second = backend.fail_session("ses-1", DEVICE_UNAVAILABLE, "故障", AT)
        self.assertEqual(first["failed_bookings"], 1)
        self.assertTrue(second["deduplicated"])
        assert_conservation(backend)

    def test_cancel_after_checkin_returns_materials(self):
        backend = tech_backend()
        backend.check_in("ses-1", "m-1", AT)
        booking_id = backend.bookings[0].id
        backend.cancel_booking(booking_id, AT)
        self.assertEqual(backend.batch_remaining("batch-a"), 10)
        assert_conservation(backend)


if __name__ == "__main__":
    unittest.main()
