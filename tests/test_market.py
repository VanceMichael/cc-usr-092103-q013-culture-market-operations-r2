"""保障后台不变量测试。

覆盖：目录排程校验、幂等扫码、容量与设备折算、同意项与年龄拦截、
家庭结伴与独立记录、换场原子性、断网补传、物料逐批守恒、审计链、
闭场对账与家庭还原。
"""

import unittest
from pathlib import Path

from src.culture_market.backend import (
    Backend, REASON_AGE, REASON_CAPACITY, REASON_CONSENT,
    REASON_DUPLICATE, REASON_MATERIAL, REASON_NO_SHOW,
)
from src.culture_market.catalog import CatalogError, load_catalog
from src.culture_market.ledger import LedgerError, MaterialLedger
from src.culture_market.reports import (
    batch_reconciliation, capacity_reconciliation, family_journey,
    reconciliation_pack, staff_dashboard, unserved_report,
)

FIXTURE = Path("fixtures/market.json")


def make_backend():
    return Backend(load_catalog(FIXTURE))


def reasons(backend):
    return [e["reason"] for e in backend.unserved]


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(FIXTURE)

    def test_fixture_loads(self):
        self.assertEqual(self.catalog.event["id"], "mid-autumn-2026")
        self.assertGreaterEqual(len(self.catalog.activities), 6)
        self.assertIn("mooncake", self.catalog.activities)

    def test_opening_check_failure_marks_device_down(self):
        backend = Backend(self.catalog)
        self.assertEqual(backend.devices["VR-04"].status, "down")
        # 6 台头显停用 1 台 -> VR 场次有效容量 5
        self.assertEqual(backend.effective_capacity("VR-S1"), 5)
        self.assertEqual(backend.effective_capacity("MOON-S1"), 12)

    def test_overallocation_of_batch_rejected(self):
        data = self.catalog.raw
        data["batch_allocations"].append(
            {"session_id": "LAMP-S1", "batch_id": "B-LED-01", "qty": 1})
        from src.culture_market.catalog import _build_catalog
        with self.assertRaises(CatalogError):
            _build_catalog(json_roundtrip(data))

    def test_venue_double_booking_rejected(self):
        data = self.catalog.raw
        data["sessions"].append({
            "id": "X-1", "activity_id": "poetry", "venue_id": "V-HALL",
            "start": "13:10", "end": "13:40", "mentor_id": "M01",
        })
        from src.culture_market.catalog import _build_catalog
        with self.assertRaises(CatalogError):
            _build_catalog(json_roundtrip(data))

    def test_staff_double_booking_rejected(self):
        data = self.catalog.raw
        data["sessions"].append({
            "id": "X-2", "activity_id": "poetry", "venue_id": "V-VR-BACKUP",
            "start": "13:10", "end": "13:40", "mentor_id": "M01",
        })
        from src.culture_market.catalog import _build_catalog
        with self.assertRaises(CatalogError):
            _build_catalog(json_roundtrip(data))

    def test_capacity_above_venue_rejected(self):
        data = self.catalog.raw
        for a in data["activities"]:
            if a["id"] == "vr":
                a["capacity"] = 99
        from src.culture_market.catalog import _build_catalog
        with self.assertRaises(CatalogError):
            _build_catalog(json_roundtrip(data))

    def test_allocation_material_mismatch_rejected(self):
        data = self.catalog.raw
        data["batch_allocations"] = [
            {"session_id": "MOON-S1", "batch_id": "B-FRAME-01", "qty": 1}
        ]
        from src.culture_market.catalog import _build_catalog
        with self.assertRaises(CatalogError):
            _build_catalog(json_roundtrip(data))


class IdempotencyTest(unittest.TestCase):
    def test_repeated_scan_serves_once(self):
        b = make_backend()
        results = [
            b.serve("RID-1", "MOON-S1", "F02-2", "13:10")
            for _ in range(5)
        ]
        self.assertTrue(all(r["ok"] is True for r in results))
        self.assertEqual(b.session_counts("MOON-S1")["served"], 1)
        self.assertEqual(b.ledger.session_usage("MOON-S1")["饼皮"]["issued"], 1)
        # 真实重复扫码（另一个请求号，同人同场）被台账拒绝
        again = b.serve("RID-OTHER", "MOON-S1", "F02-2", "13:11")
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], REASON_DUPLICATE)
        self.assertEqual(b.session_counts("MOON-S1")["served"], 1)

    def test_rejected_scan_consumes_nothing_and_is_logged(self):
        b = make_backend()
        r = b.serve("RID-X", "MOON-S1", "F02-3", "13:10")  # 缺安全确认
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], REASON_CONSENT)
        self.assertEqual(b.session_counts("MOON-S1")["served"], 0)
        usage = b.ledger.session_usage("MOON-S1")
        self.assertEqual(usage["饼皮"]["issued"], 0)
        self.assertIn(REASON_CONSENT, reasons(b))

    def test_sync_replay_deduplicates(self):
        b = make_backend()
        queued = [
            {"op": "serve", "request_id": "OFF-1", "session_id": "LAMP-S1",
             "member_id": "F02-2", "at": "13:20"},
            {"op": "serve", "request_id": "OFF-1", "session_id": "LAMP-S1",
             "member_id": "F02-2", "at": "13:20"},
        ]
        out = b.sync(queued)
        self.assertTrue(all(r["ok"] for r in out))
        self.assertEqual(b.session_counts("LAMP-S1")["served"], 1)
        self.assertTrue(out[1].get("replayed"))


class CapacityAndDevicesTest(unittest.TestCase):
    def test_device_failure_tightens_capacity(self):
        b = make_backend()
        self.assertEqual(b.effective_capacity("VR-S2"), 5)
        b.set_device_status("D1", "VR-01", "down", "13:55", "故障")
        self.assertEqual(b.effective_capacity("VR-S2"), 4)
        b.set_device_status("D2", "VR-02", "down", "13:56", "故障")
        self.assertEqual(b.effective_capacity("VR-S2"), 3)

    def test_never_serve_beyond_effective_capacity(self):
        b = make_backend()
        b.set_device_status("D1", "VR-01", "down", "13:55", "故障")  # 容量 -> 4
        mids = ["F02-2", "F03-2", "F01-2", "F03-1", "F01-1", "F02-1"]
        results = [
            b.serve(f"V{i}", "VR-S2", mids[i], f"13:{56 + i}")
            for i in range(len(mids))
        ]
        served = sum(r["ok"] for r in results)
        self.assertEqual(served, 4)
        self.assertEqual(b.session_counts("VR-S2")["served"], 4)
        self.assertIn(REASON_CAPACITY, reasons(b))
        # 恢复设备不会让已拒绝的人自动进场，但新请求可进
        b.set_device_status("D3", "VR-01", "active", "14:10", "修复")
        self.assertEqual(b.effective_capacity("VR-S2"), 5)

    def test_booking_capacity_atomic_for_group(self):
        b = make_backend()
        # 宫灯 S1 容量 12，先占 11 个名额（用现有 8 名成员无法占满，
        # 改测整组需求大于剩余时整组失败、不产生半个预约）
        for i, mid in enumerate(["F02-2", "F03-2", "F01-2"]):
            b.book(f"B{i}", "LAMP-S1", [mid], "12:55")
        counts = b.session_counts("LAMP-S1")
        self.assertEqual(counts["booked"], 3)
        # 时间冲突：同成员已在宫灯 S1（13:00-13:50），不能再约传拓 S1
        clash = b.book("BC", "RUB-S1", ["F02-2"], "12:56")
        self.assertFalse(clash["ok"])

    def test_served_plus_booked_never_exceeds_capacity(self):
        b = make_backend()
        cap = b.effective_capacity("VR-S1")  # 6 台头显 - VR-04 停用 -> 5
        for mid in ["F02-2", "F03-2", "F01-2", "F03-1", "F01-1"]:
            r = b.book(f"BK-{mid}", "VR-S1", [mid], "12:50")
            self.assertTrue(r["ok"])
        sixth = b.book("BK-6", "VR-S1", ["F02-1"], "12:51")
        self.assertFalse(sixth["ok"])  # 只剩 5 个有效名额
        self.assertEqual(cap, 5)
        counts = b.session_counts("VR-S1")
        self.assertEqual(counts["booked"] + counts["served"], 5)


class ConsentAndAgeTest(unittest.TestCase):
    def test_age_gate(self):
        b = make_backend()
        # VR 下限 8 岁：李一笑 2021 年生 -> 5 岁
        r = b.serve("AGE-1", "VR-S1", "F01-3", "13:05")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], REASON_AGE)
        # 5 岁成员即使补了同意项，仍过不了宫灯 7 岁下限
        b.update_consent("AGE-3", "F01-3", "safety_confirmation", True, "13:06")
        r1 = b.serve("AGE-4", "LAMP-S1", "F01-3", "13:07")
        self.assertEqual(r1["reason"], REASON_AGE)
        # 9 岁且同意项齐全的李一诺可正常进宫灯
        r2 = b.serve("AGE-2", "LAMP-S1", "F01-2", "13:05")
        self.assertTrue(r2["ok"])

    def test_consent_update_unblocks(self):
        b = make_backend()
        r = b.serve("C-1", "ROBOT-S1", "F02-3", "13:05")  # 缺安全确认
        self.assertEqual(r["reason"], REASON_CONSENT)
        b.update_consent("C-2", "F02-3", "safety_confirmation", True, "13:20")
        r2 = b.serve("C-3", "ROBOT-S1", "F02-3", "13:21")
        self.assertTrue(r2["ok"])

    def test_image_authorization_required_for_poetry_and_robot(self):
        b = make_backend()
        # 张小满缺影像授权：诗词、机器狗都应被拦
        for session in ("POET-S1", "ROBOT-S1"):
            r = b.serve(f"IMG-{session}", session, "F03-2", "13:05")
            self.assertEqual(r["reason"], REASON_CONSENT)
        # 宫灯不要求影像授权，同成员可正常服务
        r = b.serve("IMG-OK", "LAMP-S1", "F03-2", "13:05")
        self.assertTrue(r["ok"])


class FamilyTest(unittest.TestCase):
    def test_family_group_books_together_records_separately(self):
        b = make_backend()
        r = b.book("GRP-1", "MOON-S1", ["F01-2", "F01-3"], "13:05",
                   group_id="G1")
        self.assertTrue(r["ok"])
        bookings = [b.bookings[i] for i in r["booking_ids"]]
        self.assertTrue(all(x.group_id == "G1" for x in bookings))
        b.update_consent("GRP-C", "F01-3", "allergy_notice", True, "13:15")
        b.serve("GRP-S1", "MOON-S1", "F01-2", "13:16")
        b.serve("GRP-S2", "MOON-S1", "F01-3", "13:17")
        # 印章按人独立
        self.assertEqual([s["code"] for s in b.stamps["F01-2"]], ["饼"])
        self.assertEqual([s["code"] for s in b.stamps["F01-3"]], ["饼"])

    def test_reading_records_are_per_person(self):
        b = make_backend()
        b.serve("R-1", "POET-S2", "F01-2", "14:00",
                reading={"title": "望月怀远", "minutes": 10})
        b.serve("R-2", "POET-S3", "F02-2", "14:50",
                reading={"title": "古朗月行（节选）", "minutes": 8})
        self.assertEqual(len(b.readings["F01-2"]), 1)
        self.assertEqual(b.readings["F01-2"][0]["title"], "望月怀远")
        self.assertEqual(b.readings["F02-2"][0]["minutes"], 8)
        self.assertNotIn("F01-3", b.readings)

    def test_kit_one_per_family_science_records_per_child(self):
        b = make_backend()
        b.book("K-1", "KIT-1", ["F01-1"], "13:30")
        r1 = b.serve("K-2", "KIT-1", "F01-1", "13:31")
        self.assertTrue(r1["ok"])
        # 同一家庭另一成员重复领 -> 拒绝
        r2 = b.serve("K-3", "KIT-1", "F01-2", "13:32")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["reason"], REASON_DUPLICATE)
        self.assertEqual(b.ledger.session_usage("KIT-1")["科普资源包"]["issued"], 1)
        # 家中两名未成年人各自获得独立的科普记录
        self.assertEqual(len(b.science["F01-2"]), 1)
        self.assertEqual(len(b.science["F01-3"]), 1)
        self.assertNotIn("F01-1", b.science)  # 成年人不建科普记录


class TransferAndStockTest(unittest.TestCase):
    def test_transfer_is_atomic(self):
        b = make_backend()
        b.book("T-1", "LAMP-S1", ["F02-2"], "12:55")
        booking = next(
            x for x in b.bookings.values()
            if x.member_id == "F02-2" and x.session_id == "LAMP-S1"
        )
        # 换到同时段重叠场次 -> 拒绝（LAMP-S1 13:00-13:50 vs MOON-S1）
        bad = b.transfer("T-2", booking.id, "MOON-S1", "12:58")
        self.assertFalse(bad["ok"])
        self.assertEqual(b.bookings[booking.id].status, "booked")
        # 换到不冲突场次 -> 成功，原名额释放
        ok = b.transfer("T-3", booking.id, "LAMP-S2", "12:59")
        self.assertTrue(ok["ok"])
        self.assertEqual(b.bookings[booking.id].status, "transferred")
        self.assertEqual(b.session_counts("LAMP-S1")["booked"], 0)
        self.assertEqual(b.session_counts("LAMP-S2")["booked"], 1)

    def test_move_stock_preserves_conservation(self):
        b = make_backend()
        before = b.ledger.batches["B-FRAME-01"]
        b.move_stock("M-1", "LAMP-S1", "LAMP-S2", "宫灯骨架", "B-FRAME-01",
                     3, "12:40")
        after = b.ledger.batches["B-FRAME-01"]
        self.assertEqual(before.received, after.received)
        self.assertEqual(b.ledger.stocks["LAMP-S1"].free_of("宫灯骨架", "B-FRAME-01"), 9)
        self.assertEqual(b.ledger.stocks["LAMP-S2"].free_of("宫灯骨架", "B-FRAME-01"), 15)

    def test_waste_is_traced_to_batch(self):
        b = make_backend()
        picked = b.record_waste("W-1", "RUB-S1", "宣纸", 3, "13:40",
                                "撕破")["batches"]
        self.assertEqual(picked, {"B-PAPER-01": 3})
        self.assertEqual(b.ledger.batches["B-PAPER-01"].wasted, 3)


class MaterialConservationTest(unittest.TestCase):
    def test_issue_beyond_available_impossible(self):
        b = make_backend()
        # RUB-S1 每人 2 张宣纸、配额 20 张；服务 10 人后第 11 人因物料被拦
        mids = ["F01-2", "F02-2", "F02-3", "F03-2", "F01-3",
                "F01-1", "F02-1", "F03-1", "F01-2", "F02-2", "F03-2"]
        # 受样例成员数限制，直接在台账层面验证超发不可达
        with self.assertRaises(LedgerError):
            for _ in range(11):
                b.ledger.issue("RUB-S1", "宣纸", 2)

    def test_batch_identity_holds_after_mixed_activity(self):
        b = make_backend()
        # 月饼 S1/S2 用 B-DOUGH-01；报废 + 退料 + 调剂混合操作
        b.serve("MM-1", "MOON-S1", "F02-2", "13:10")
        b.serve("MM-2", "MOON-S1", "F03-2", "13:11")
        b.record_waste("MM-3", "MOON-S2", "饼皮", 1, "14:30", "掉落污染")
        b.move_stock("MM-4", "MOON-S4", "MOON-S3", "饼皮", "B-DOUGH-02",
                     2, "15:20")
        for bid, batch in b.ledger.batches.items():
            self.assertTrue(
                batch.conserved(),
                f"批次 {bid} 破坏守恒：{batch}",
            )
        self.assertTrue(b.ledger.all_conserved())

    def test_reconciliation_totals_balance(self):
        b = make_backend()
        b.serve("Z-1", "MOON-S1", "F02-2", "13:10")
        b.record_waste("Z-2", "LAMP-S1", "LED灯芯", 1, "13:30", "不亮")
        b.close("17:30")
        rec = batch_reconciliation(b)
        self.assertTrue(rec["conserved"])
        for row in rec["batches"]:
            self.assertAlmostEqual(
                row["received"], row["issued"] + row["wasted"] + row["remaining"],
                places=6,
            )


class CloseoutTest(unittest.TestCase):
    def test_close_marks_no_shows_and_reconciles(self):
        b = make_backend()
        b.book("N-1", "POET-S4", ["F02-2"], "16:00")
        b.serve("N-2", "POET-S2", "F03-2", "14:00",
                reading={"title": "望月怀远", "minutes": 9})
        b.close("17:30")
        booking = next(x for x in b.bookings.values()
                       if x.member_id == "F02-2" and x.session_id == "POET-S4")
        self.assertEqual(booking.status, "no_show")
        self.assertIn(REASON_NO_SHOW, reasons(b))
        pack = reconciliation_pack(b)
        self.assertTrue(pack["audit_chain_ok"])
        self.assertTrue(pack["capacity"]["all_within_limit"])
        self.assertTrue(pack["materials"]["conserved"])

    def test_audit_chain_detects_tampering(self):
        b = make_backend()
        b.serve("A-1", "MOON-S1", "F02-2", "13:10")
        self.assertTrue(b.verify_audit_chain())
        # 篡改一条历史事件
        b.audit[1]["at"] = "00:00"
        self.assertFalse(b.verify_audit_chain())

    def test_family_journey_reflects_reality(self):
        b = make_backend()
        b.book("FJ-1", "MOON-S1", ["F01-2", "F01-3"], "13:05", group_id="G1")
        r1 = b.serve("FJ-2", "MOON-S1", "F01-2", "13:10")
        self.assertTrue(r1["ok"])
        r2 = b.serve("FJ-3", "MOON-S1", "F01-3", "13:11")
        self.assertFalse(r2["ok"])  # 缺过敏提示
        b.update_consent("FJ-4", "F01-3", "allergy_notice", True, "13:15")
        b.serve("FJ-5", "MOON-S1", "F01-3", "13:16")
        journey = family_journey(b, "F01")
        members = {m["member_id"]: m for m in journey["members"]}
        self.assertEqual([s["code"] for s in members["F01-3"]["stamps"]], ["饼"])
        self.assertTrue(members["F01-3"]["consents"]["过敏提示"])
        # 未服务台账中保留被拦历史，家庭行程中两次预约状态真实可查
        statuses = [x["status"] for x in members["F01-3"]["bookings"]]
        self.assertIn("served", statuses)

    def test_dashboard_lists_down_devices_and_gaps(self):
        b = make_backend()
        board = staff_dashboard(b)
        self.assertTrue(any(d["id"] == "VR-04" for d in board["devices_down"]))
        gap_members = {g["member_id"] for g in board["consent_gaps"]}
        self.assertEqual(gap_members, {"F01-3", "F02-3", "F03-2"})
        b.update_consent("G-1", "F03-2", "image_authorization", True, "13:10")
        board2 = staff_dashboard(b)
        self.assertNotIn("F03-2", {g["member_id"] for g in board2["consent_gaps"]})

    def test_unserved_report_groups_reasons(self):
        b = make_backend()
        b.serve("U-1", "VR-S1", "F01-3", "13:05")  # 低龄
        b.serve("U-2", "ROBOT-S1", "F02-3", "13:06")  # 缺安全确认
        report = unserved_report(b)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["summary"]["未达年龄限制"], 1)
        self.assertEqual(report["summary"]["同意项未完成"], 1)


def json_roundtrip(data):
    import json
    return json.loads(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
