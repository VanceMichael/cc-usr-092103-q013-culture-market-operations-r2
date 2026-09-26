import unittest
from pathlib import Path

from src.backend import MarketBackend
from src.plan import load_plan
from src.reports import (
    assert_conservation,
    family_journey,
    resource_report,
    service_report,
)


def simulate_day(backend: MarketBackend) -> None:
    """按时间顺序模拟中秋市集一天的真实运行（含异常与补救）。"""
    # 09:35 王小虎领取科普资源包
    backend.check_in("ses-kit-1", "mem-wang-sun", "2026-09-26T09:35:00")
    backend.complete(session_id="ses-kit-1", member_id="mem-wang-sun",
                     at="2026-09-26T09:36:00")
    # 09:40 邻里结伴组（陈家+刘家 5 人）预约诗词上午场
    backend.book_group("ses-poetry-1", "grp-friends", "2026-09-26T09:40:00")
    # 10:05 陈小明、刘朵朵参加诗词并各自完成
    backend.check_in("ses-poetry-1", "mem-chen-zi", "2026-09-26T10:05:00")
    backend.complete(session_id="ses-poetry-1", member_id="mem-chen-zi",
                     at="2026-09-26T10:40:00")
    backend.check_in("ses-poetry-1", "mem-liu-nv", "2026-09-26T10:06:00")
    backend.complete(session_id="ses-poetry-1", member_id="mem-liu-nv",
                     at="2026-09-26T10:41:00")
    # 10:31 陈小明月饼手作：过敏提示缺失被拒 → 补录 → 核销 → 完成
    backend.book("ses-mooncake-1", "mem-chen-zi", "2026-09-26T10:20:00")
    backend.check_in("ses-mooncake-1", "mem-chen-zi", "2026-09-26T10:31:00")
    backend.record_consent("mem-chen-zi", "allergy", "2026-09-26T10:32:00")
    backend.check_in("ses-mooncake-1", "mem-chen-zi", "2026-09-26T10:33:00")
    backend.complete(session_id="ses-mooncake-1", member_id="mem-chen-zi",
                     at="2026-09-26T11:10:00")
    # 11:05 陈小明宫灯扎制并完成（第二项手作）
    backend.check_in("ses-lantern-1", "mem-chen-zi", "2026-09-26T11:05:00")
    backend.complete(session_id="ses-lantern-1", member_id="mem-chen-zi",
                     at="2026-09-26T11:55:00")
    # 14:05 王小虎传拓体验并完成
    backend.check_in("ses-rubbing-1", "mem-wang-sun", "2026-09-26T14:05:00")
    backend.complete(session_id="ses-rubbing-1", member_id="mem-wang-sun",
                     at="2026-09-26T14:35:00")
    # 15:01 刘朵朵机器狗：安全确认缺失被拒 → 补录 → 核销 → 完成
    backend.check_in("ses-robotdog-1", "mem-liu-nv", "2026-09-26T15:01:00")
    backend.record_consent("mem-liu-nv", "safety", "2026-09-26T15:02:00")
    backend.check_in("ses-robotdog-1", "mem-liu-nv", "2026-09-26T15:03:00")
    backend.complete(session_id="ses-robotdog-1", member_id="mem-liu-nv",
                     at="2026-09-26T15:25:00")
    # 15:10 VR头显2号停用；15:31 刘朵朵 VR 被拒 → 换备用机 → 完成
    backend.set_device_status("dev-vr-2", "disabled", "线缆检修", "2026-09-26T15:10:00")
    backend.check_in("ses-vr-1", "mem-liu-nv", "2026-09-26T15:31:00")
    backend.swap_device("ses-vr-1", "dev-vr-2", "dev-vr-3", "2026-09-26T15:32:00")
    backend.check_in("ses-vr-1", "mem-liu-nv", "2026-09-26T15:33:00")
    backend.complete(session_id="ses-vr-1", member_id="mem-liu-nv",
                     at="2026-09-26T15:44:00")
    # 15:35 王小虎预约 VR；15:45 VR头显1号故障，备用机耗尽 → 取消场次
    backend.book("ses-vr-1", "mem-wang-sun", "2026-09-26T15:35:00")
    backend.set_device_status("dev-vr-1", "faulty", "定位失效", "2026-09-26T15:45:00")
    backend.fail_session("ses-vr-1", "device_unavailable", "VR头显相继故障",
                         "2026-09-26T15:46:00")
    # 16:00 诗词下午场临时换场到宫灯长廊
    backend.change_venue("ses-poetry-2", "venue-lantern", "诗词亭临时占用",
                         "2026-09-26T16:00:00")


class DaySimulationTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        simulate_day(self.backend)

    def test_conservation_holds_after_full_day(self):
        assert_conservation(self.backend)

    def test_stamps_follow_rules(self):
        stamps = {(s.member_id, s.name) for s in self.backend.stamps}
        # 陈小明：诗词（阅读）+ 月饼、宫灯（手作×2）
        self.assertIn(("mem-chen-zi", "书香章"), stamps)
        self.assertIn(("mem-chen-zi", "巧手章"), stamps)
        # 刘朵朵：诗词（阅读）+ 机器狗、VR（科技×2）
        self.assertIn(("mem-liu-nv", "书香章"), stamps)
        self.assertIn(("mem-liu-nv", "探索章"), stamps)
        # 王小虎：科普包（阅读）+ 传拓（非遗）
        self.assertIn(("mem-wang-sun", "书香章"), stamps)
        self.assertIn(("mem-wang-sun", "传习章"), stamps)
        self.assertNotIn(("mem-wang-sun", "探索章"), stamps)  # VR 未服务成功

    def test_individual_records_stay_independent_within_group(self):
        # 结伴同行，但阅读记录按成员各自独立
        reading = {(r.member_id, r.session_id) for r in self.backend.reading_records}
        self.assertEqual(
            reading,
            {("mem-chen-zi", "ses-poetry-1"), ("mem-liu-nv", "ses-poetry-1")},
        )
        # 同组的陈爸爸/陈妈妈/刘妈妈只预约未核销，没有阅读记录
        science = [(r.member_id, r.kit_batch_id) for r in self.backend.science_records]
        self.assertEqual(science, [("mem-wang-sun", "batch-kit")])


class ResourceReportTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        simulate_day(self.backend)
        self.report = resource_report(self.backend)

    def test_every_batch_conserved_and_explained(self):
        self.assertTrue(self.report["conservation_ok"])
        by_id = {b["batch_id"]: b for b in self.report["batches"]}
        crust = by_id["batch-crust"]
        self.assertEqual((crust["initial"], crust["issued"], crust["remaining"]), (40, 1, 39))
        paper = by_id["batch-ricepaper"]
        self.assertEqual((paper["issued"], paper["remaining"]), (2, 58))
        kit = by_id["batch-kit"]
        self.assertEqual((kit["issued"], kit["remaining"]), (1, 49))
        # 每条流水都能追溯到预约
        for batch in self.report["batches"]:
            for mv in batch["movements"]:
                self.assertTrue(mv["ref"].startswith("bk-"))

    def test_no_material_lost_to_device_failure(self):
        # VR 无物料，故障未造成任何批次的损耗
        self.assertEqual(sum(b["wasted"] for b in self.report["batches"]), 0)


class ServiceReportTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        simulate_day(self.backend)
        self.report = service_report(self.backend)

    def test_unserved_entries_carry_reasons(self):
        self.assertEqual(len(self.report["unserved"]), 1)
        entry = self.report["unserved"][0]
        self.assertEqual(entry["member_id"], "mem-wang-sun")
        self.assertEqual(entry["session_id"], "ses-vr-1")
        self.assertEqual(entry["reason"], "device_unavailable")
        self.assertIn("VR头显相继故障", entry["note"])

    def test_session_outcomes_add_up(self):
        vr = next(s for s in self.report["sessions"] if s["session_id"] == "ses-vr-1")
        self.assertTrue(vr["cancelled"])
        self.assertEqual(vr["served"], 1)   # 刘朵朵已完成
        self.assertEqual(vr["failed"], 1)   # 王小虎未服务成功
        poetry = next(s for s in self.report["sessions"] if s["session_id"] == "ses-poetry-1")
        self.assertEqual(poetry["served"], 2)


class FamilyJourneyTest(unittest.TestCase):
    def setUp(self):
        self.backend = MarketBackend(load_plan(Path("fixtures/plan.json")))
        simulate_day(self.backend)

    def test_journey_reconstructs_real_process_including_rejections(self):
        journey = family_journey(self.backend, "fam-chen")
        kinds = [e["kind"] for e in journey["timeline"]]
        self.assertIn("book_group", kinds)       # 结伴预约
        self.assertIn("record_consent", kinds)   # 补录过敏提示
        self.assertIn("stamp", kinds)            # 获得印章
        # 被拒的扫码也如实留在时间线里
        rejected = [e for e in journey["timeline"] if not e["ok"]]
        self.assertTrue(any(e["detail"]["reason"] == "consent_missing" for e in rejected))

    def test_journey_shows_failed_service_with_reason(self):
        journey = family_journey(self.backend, "fam-wang")
        fail_events = [e for e in journey["timeline"] if e["kind"] == "fail_session"]
        self.assertEqual(len(fail_events), 1)
        self.assertEqual(fail_events[0]["detail"]["reason"], "device_unavailable")

    def test_journey_is_limited_to_own_family(self):
        journey = family_journey(self.backend, "fam-liu")
        member_ids = {"mem-liu-ma", "mem-liu-nv"}
        for entry in journey["timeline"]:
            detail = entry["detail"]
            if "member_id" in detail:
                self.assertIn(detail["member_id"], member_ids)
        # 刘家的时间线不含王家领科普包的事件
        self.assertFalse(
            any(r.member_id == "mem-wang-sun" for r in journey["reading_records"])
        )
        self.assertEqual(journey["science_records"], [])

    def test_timeline_is_chronological(self):
        journey = family_journey(self.backend, "fam-chen")
        times = [e["at"] for e in journey["timeline"]]
        self.assertEqual(times, sorted(times))


if __name__ == "__main__":
    unittest.main()
