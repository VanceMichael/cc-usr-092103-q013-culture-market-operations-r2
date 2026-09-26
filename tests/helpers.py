"""测试共享工具：构造边缘场景用的小型排定资料与状态快照。"""

from __future__ import annotations

from src.backend import MarketBackend
from src.plan import load_plan


def make_backend(**overrides) -> MarketBackend:
    """构造一个最小可用后台，可按需覆盖任何字段。"""
    plan = {
        "plan_id": "test-plan",
        "version": 1,
        "date": "2026-09-26",
        "venues": [
            {"id": "v-small", "name": "小厅", "capacity": 2},
            {"id": "v-big", "name": "大厅", "capacity": 50},
        ],
        "staff": [{"id": "st-1", "name": "测试导师", "role": "mentor"}],
        "activities": [
            {
                "id": "act-plain",
                "name": "普通体验",
                "category": "craft",
                "required_consents": ["safety"],
                "min_age": 5,
                "materials": {"batch-a": 1},
            },
        ],
        "batches": [
            {"id": "batch-a", "name": "原料批次A", "unit": "份", "initial_quantity": 10},
        ],
        "devices": [],
        "sessions": [
            {
                "id": "ses-1",
                "activity_id": "act-plain",
                "venue_id": "v-big",
                "staff_id": "st-1",
                "start": "2026-09-26T10:00:00",
                "end": "2026-09-26T11:00:00",
                "capacity": 2,
            },
        ],
        "families": [{"id": "f-1", "name": "甲家"}],
        "members": [
            {"id": "m-1", "family_id": "f-1", "name": "成员一", "age": 8,
             "consents": ["safety"]},
            {"id": "m-2", "family_id": "f-1", "name": "成员二", "age": 9,
             "consents": ["safety"]},
            {"id": "m-3", "family_id": "f-1", "name": "成员三", "age": 10,
             "consents": ["safety"]},
        ],
    }
    plan.update(overrides)
    return MarketBackend(load_plan(plan))


def snapshot(backend: MarketBackend) -> dict:
    """提取可比较的运行状态快照，用于验证事件重放后状态一致。"""
    return {
        "bookings": sorted(
            (b.id, b.session_id, b.member_id, b.status.value, b.failure_reason)
            for b in backend.bookings
        ),
        "remaining": {b.id: backend.batch_remaining(b.id) for b in backend.plan.batches},
        "movements": [
            (m.at, m.batch_id, m.kind.value, m.quantity, m.ref, m.note)
            for m in backend.movements
        ],
        "devices": {d.id: backend.device_status(d.id).value for d in backend.plan.devices},
        "stamps": sorted((s.member_id, s.rule_id) for s in backend.stamps),
        "reading": sorted((r.member_id, r.session_id) for r in backend.reading_records),
        "science": sorted((r.member_id, r.session_id) for r in backend.science_records),
        "session_views": [
            (v["session_id"], v["venue_id"], v["booked"], v["cancelled"])
            for v in (backend.session_view(s.id) for s in backend.plan.sessions)
        ],
    }


def events_of(backend: MarketBackend) -> list[dict]:
    """从事件日志还原可重放的事件序列（模拟断网补传的内容）。"""
    return [
        {"event_id": r.event_id, "kind": r.kind, "at": r.at, "payload": r.payload}
        for r in backend.event_log
    ]
