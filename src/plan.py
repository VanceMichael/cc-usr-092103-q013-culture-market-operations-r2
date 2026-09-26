"""读取并校验活动排定资料（``fixtures/plan.json``）。

与 :mod:`src.context` 一样只做读取与校验；校验重点除字段完整外，
还包括引用一致性（场次引用的活动/场地/人员/设备存在、活动引用的
原料批次存在、结伴引用的家庭存在等），保证后台启动时资料自洽。
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    Activity,
    Category,
    Consent,
    Device,
    DeviceCheck,
    EventPlan,
    Family,
    FamilyGroup,
    MaterialBatch,
    Member,
    Session,
    StaffMember,
    StampRule,
    Venue,
)

_STAFF_ROLES = {"mentor", "inheritor"}
_RECORD_KINDS = {"reading", "science", None}


def _require(mapping: dict, keys: tuple[str, ...], where: str) -> None:
    missing = [k for k in keys if k not in mapping]
    if missing:
        raise ValueError(f"{where} 缺少必要字段: {', '.join(missing)}")


def _parse_consents(values, where: str) -> tuple[Consent, ...]:
    try:
        return tuple(Consent(v) for v in values)
    except ValueError as exc:
        raise ValueError(f"{where} 含未知确认事项: {exc}") from exc


def plan_from_dict(data: dict) -> EventPlan:
    """把字典资料校验并转换为 :class:`EventPlan`。"""
    _require(data, ("plan_id", "version", "date"), "排定资料")
    if not isinstance(data["version"], int) or data["version"] < 1:
        raise ValueError("排定资料 version 必须为正整数")

    venues = tuple(
        Venue(id=v["id"], name=v["name"], capacity=int(v["capacity"]))
        for v in data.get("venues", [])
    )
    staff = tuple(
        StaffMember(id=s["id"], name=s["name"], role=s["role"])
        for s in data.get("staff", [])
    )
    for s in staff:
        if s.role not in _STAFF_ROLES:
            raise ValueError(f"人员 {s.id} 角色须为 mentor 或 inheritor")

    activities = tuple(
        Activity(
            id=a["id"],
            name=a["name"],
            category=Category(a["category"]),
            required_consents=_parse_consents(a.get("required_consents", []), f"活动 {a['id']}"),
            min_age=int(a.get("min_age", 0)),
            max_age=a.get("max_age"),
            materials={k: int(q) for k, q in a.get("materials", {}).items()},
            device_type=a.get("device_type"),
            record_kind=a.get("record_kind"),
        )
        for a in data.get("activities", [])
    )
    for a in activities:
        if a.record_kind not in _RECORD_KINDS:
            raise ValueError(f"活动 {a.id} record_kind 须为 reading/science")
        if a.max_age is not None and a.max_age < a.min_age:
            raise ValueError(f"活动 {a.id} 年龄上限小于下限")

    batches = tuple(
        MaterialBatch(
            id=b["id"],
            name=b["name"],
            unit=b["unit"],
            initial_quantity=int(b["initial_quantity"]),
        )
        for b in data.get("batches", [])
    )
    devices = tuple(
        Device(id=d["id"], name=d["name"], type=d["type"])
        for d in data.get("devices", [])
    )
    device_checks = tuple(
        DeviceCheck(
            device_id=c["device_id"],
            checked_at=c["checked_at"],
            inspector=c["inspector"],
            result=c["result"],
            note=c.get("note", ""),
        )
        for c in data.get("device_checks", [])
    )
    sessions = tuple(
        Session(
            id=s["id"],
            activity_id=s["activity_id"],
            venue_id=s["venue_id"],
            staff_id=s["staff_id"],
            start=s["start"],
            end=s["end"],
            capacity=int(s["capacity"]),
            device_ids=tuple(s.get("device_ids", [])),
        )
        for s in data.get("sessions", [])
    )
    stamp_rules = tuple(
        StampRule(
            id=r["id"],
            name=r["name"],
            category=Category(r["category"]),
            required_completions=int(r["required_completions"]),
        )
        for r in data.get("stamp_rules", [])
    )
    families = tuple(Family(id=f["id"], name=f["name"]) for f in data.get("families", []))
    members = tuple(
        Member(
            id=m["id"],
            family_id=m["family_id"],
            name=m["name"],
            age=int(m["age"]),
            consents=frozenset(_parse_consents(m.get("consents", []), f"成员 {m['id']}")),
        )
        for m in data.get("members", [])
    )
    groups = tuple(
        FamilyGroup(id=g["id"], name=g["name"], family_ids=tuple(g["family_ids"]))
        for g in data.get("groups", [])
    )

    plan = EventPlan(
        plan_id=data["plan_id"],
        version=data["version"],
        date=data["date"],
        venues=venues,
        staff=staff,
        activities=activities,
        batches=batches,
        devices=devices,
        device_checks=device_checks,
        sessions=sessions,
        stamp_rules=stamp_rules,
        families=families,
        members=members,
        groups=groups,
    )
    _check_references(plan)
    return plan


def _check_references(plan: EventPlan) -> None:
    venue_ids = {v.id for v in plan.venues}
    staff_ids = {s.id for s in plan.staff}
    activity_ids = {a.id for a in plan.activities}
    batch_ids = {b.id for b in plan.batches}
    device_ids = {d.id for d in plan.devices}
    family_ids = {f.id for f in plan.families}

    for a in plan.activities:
        unknown = set(a.materials) - batch_ids
        if unknown:
            raise ValueError(f"活动 {a.id} 引用了不存在的原料批次: {sorted(unknown)}")
    for c in plan.device_checks:
        if c.device_id not in device_ids:
            raise ValueError(f"设备检查记录引用了不存在的设备: {c.device_id}")
    for s in plan.sessions:
        if s.activity_id not in activity_ids:
            raise ValueError(f"场次 {s.id} 引用了不存在的活动: {s.activity_id}")
        if s.venue_id not in venue_ids:
            raise ValueError(f"场次 {s.id} 引用了不存在的场地: {s.venue_id}")
        if s.staff_id not in staff_ids:
            raise ValueError(f"场次 {s.id} 引用了不存在的人员: {s.staff_id}")
        unknown_devices = set(s.device_ids) - device_ids
        if unknown_devices:
            raise ValueError(f"场次 {s.id} 引用了不存在的设备: {sorted(unknown_devices)}")
        if s.capacity < 1:
            raise ValueError(f"场次 {s.id} 容量必须为正整数")
    for m in plan.members:
        if m.family_id not in family_ids:
            raise ValueError(f"成员 {m.id} 引用了不存在的家庭: {m.family_id}")
    for g in plan.groups:
        unknown = set(g.family_ids) - family_ids
        if unknown:
            raise ValueError(f"结伴 {g.id} 引用了不存在的家庭: {sorted(unknown)}")


def load_plan(source: Path | str | dict) -> EventPlan:
    """从路径或字典加载排定资料。"""
    if isinstance(source, dict):
        data = source
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    return plan_from_dict(data)
