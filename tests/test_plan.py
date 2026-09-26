import unittest
from pathlib import Path

from src.plan import load_plan


class PlanFixtureTest(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan(Path("fixtures/plan.json"))

    def test_fixture_loads_with_all_sections(self):
        self.assertEqual(self.plan.plan_id, "mid-autumn-market-2026")
        self.assertEqual(self.plan.version, 1)
        self.assertEqual(len(self.plan.activities), 7)
        self.assertEqual(len(self.plan.sessions), 8)
        self.assertEqual(len(self.plan.batches), 5)
        self.assertEqual(len(self.plan.devices), 5)
        self.assertEqual(len(self.plan.device_checks), 5)
        self.assertEqual(len(self.plan.stamp_rules), 4)
        self.assertEqual(len(self.plan.members), 7)
        self.assertEqual(len(self.plan.groups), 1)

    def test_roles_cover_mentor_and_inheritor(self):
        roles = {s.role for s in self.plan.staff}
        self.assertEqual(roles, {"mentor", "inheritor"})

    def test_every_device_checked_before_opening(self):
        checked = {c.device_id for c in self.plan.device_checks}
        self.assertEqual(checked, {d.id for d in self.plan.devices})


class PlanValidationTest(unittest.TestCase):
    def _base(self):
        return {
            "plan_id": "p",
            "version": 1,
            "date": "2026-09-26",
            "venues": [{"id": "v", "name": "厅", "capacity": 10}],
            "staff": [{"id": "s", "name": "导师", "role": "mentor"}],
            "activities": [{"id": "a", "name": "体验", "category": "craft"}],
            "sessions": [
                {"id": "se", "activity_id": "a", "venue_id": "v", "staff_id": "s",
                 "start": "2026-09-26T10:00:00", "end": "2026-09-26T11:00:00",
                 "capacity": 5}
            ],
        }

    def test_missing_required_field_rejected(self):
        plan = self._base()
        del plan["version"]
        with self.assertRaises(ValueError):
            load_plan(plan)

    def test_dangling_session_reference_rejected(self):
        plan = self._base()
        plan["sessions"][0]["activity_id"] = "ghost"
        with self.assertRaisesRegex(ValueError, "不存在的活动"):
            load_plan(plan)

    def test_dangling_material_reference_rejected(self):
        plan = self._base()
        plan["activities"][0]["materials"] = {"no-such-batch": 1}
        with self.assertRaisesRegex(ValueError, "不存在的原料批次"):
            load_plan(plan)

    def test_unknown_consent_rejected(self):
        plan = self._base()
        plan["activities"][0]["required_consents"] = ["blood-type"]
        with self.assertRaisesRegex(ValueError, "未知确认事项"):
            load_plan(plan)

    def test_unknown_staff_role_rejected(self):
        plan = self._base()
        plan["staff"][0]["role"] = "volunteer"
        with self.assertRaisesRegex(ValueError, "mentor 或 inheritor"):
            load_plan(plan)

    def test_dangling_group_family_rejected(self):
        plan = self._base()
        plan["groups"] = [{"id": "g", "name": "结伴", "family_ids": ["no-such-family"]}]
        with self.assertRaisesRegex(ValueError, "不存在的家庭"):
            load_plan(plan)


if __name__ == "__main__":
    unittest.main()
