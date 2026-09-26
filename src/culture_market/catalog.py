"""载入并校验静态活动目录，排定基础资料之间的引用与冲突。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONSENT_LABELS = {
    "allergy_notice": "过敏提示",
    "image_authorization": "影像授权",
    "safety_confirmation": "安全确认",
}


class CatalogError(ValueError):
    """目录资料不合法或排程存在冲突。"""


@dataclass(frozen=True)
class MaterialSpec:
    material: str
    qty: float
    unit: str


@dataclass(frozen=True)
class Activity:
    id: str
    name: str
    booking: str  # person | family
    capacity: int
    min_age: int
    required_consents: tuple[str, ...]
    stamp_code: str | None
    stamp_rule: str | None
    materials: tuple[MaterialSpec, ...]
    records: tuple[str, ...]
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Session:
    id: str
    activity_id: str
    venue_id: str
    start: str
    end: str
    mentor_id: str | None
    inheritor_id: str | None


@dataclass(frozen=True)
class Member:
    member_id: str
    family_id: str
    name: str
    relation: str
    birth_year: int
    consents: dict[str, bool]


@dataclass
class Catalog:
    raw: dict
    event: dict
    venues: dict[str, dict]
    staff: dict[str, dict]
    activities: dict[str, Activity]
    sessions: dict[str, Session]
    batches: dict[str, dict]
    allocations: list[dict]
    devices: dict[str, dict]
    device_checks: list[dict]
    families: dict[str, dict]
    members: dict[str, Member]

    # ---- 便捷查询 -------------------------------------------------
    def activity_sessions(self, activity_id: str) -> list[Session]:
        return [s for s in self.sessions.values() if s.activity_id == activity_id]

    def session_devices(self, activity_id: str) -> list[dict]:
        return [d for d in self.devices.values() if d["activity_id"] == activity_id]

    def age_of(self, member: Member, year: int | None = None) -> int:
        year = year or int(self.event["date"][:4])
        return year - member.birth_year


def load_catalog(path: str | Path) -> Catalog:
    """读取目录文件并完成全部静态一致性检查。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _build_catalog(data)


def _build_catalog(data: dict) -> Catalog:
    required = {
        "domain", "version", "sample_id", "event", "venues", "staff",
        "activities", "sessions", "material_batches", "batch_allocations",
        "devices", "device_checks", "families",
    }
    missing = required - data.keys()
    if missing:
        raise CatalogError(f"目录缺少必要字段：{sorted(missing)}")
    if data["domain"] != "culture-market-operations":
        raise CatalogError("domain 必须为 culture-market-operations")

    venues = _index(data["venues"], "id", "场地")
    staff = _index(data["staff"], "id", "工作人员/传承人")
    batches = _index(data["material_batches"], "id", "原料批次")
    devices = _index(data["devices"], "id", "设备")

    activities: dict[str, Activity] = {}
    for a in data["activities"]:
        if a["booking"] not in ("person", "family"):
            raise CatalogError(f"活动 {a['id']} 的 booking 非法")
        unknown_consents = set(a["required_consents"]) - set(CONSENT_LABELS)
        if unknown_consents:
            raise CatalogError(f"活动 {a['id']} 存在未知同意项 {unknown_consents}")
        stamp = a.get("stamp")
        activities[a["id"]] = Activity(
            id=a["id"],
            name=a["name"],
            booking=a["booking"],
            capacity=a["capacity"],
            min_age=a["min_age"],
            required_consents=tuple(a["required_consents"]),
            stamp_code=stamp["code"] if stamp else None,
            stamp_rule=stamp["rule"] if stamp else None,
            materials=tuple(
                MaterialSpec(m["material"], m["qty"], m["unit"]) for m in a["materials"]
            ),
            records=tuple(a.get("records", [])),
            extra={k: v for k, v in a.items()
                   if k not in {"id", "name", "booking", "capacity", "min_age",
                                "required_consents", "stamp", "materials", "records"}},
        )

    sessions: dict[str, Session] = {}
    for s in data["sessions"]:
        if s["activity_id"] not in activities:
            raise CatalogError(f"场次 {s['id']} 引用了未知活动 {s['activity_id']}")
        if s["venue_id"] not in venues:
            raise CatalogError(f"场次 {s['id']} 引用了未知场地 {s['venue_id']}")
        for key in ("mentor_id", "inheritor_id"):
            if s.get(key) and s[key] not in staff:
                raise CatalogError(f"场次 {s['id']} 引用了未知人员 {s[key]}")
        if not (s["start"] < s["end"]):
            raise CatalogError(f"场次 {s['id']} 开始时间不早于结束时间")
        sessions[s["id"]] = Session(
            id=s["id"], activity_id=s["activity_id"], venue_id=s["venue_id"],
            start=s["start"], end=s["end"],
            mentor_id=s.get("mentor_id"), inheritor_id=s.get("inheritor_id"),
        )

    _check_venue_conflicts(sessions, venues, activities)
    _check_staff_conflicts(sessions)
    _check_allocations(data["batch_allocations"], sessions, batches, activities)
    _check_devices(data["device_checks"], devices, staff)
    families, members = _build_families(data["families"])

    return Catalog(
        raw=data,
        event=data["event"],
        venues=venues,
        staff=staff,
        activities=activities,
        sessions=sessions,
        batches=batches,
        allocations=list(data["batch_allocations"]),
        devices=devices,
        device_checks=list(data["device_checks"]),
        families=families,
        members=members,
    )


def _index(rows: list[dict], key: str, label: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        k = row[key]
        if k in out:
            raise CatalogError(f"{label}标识重复：{k}")
        out[k] = row
    return out


def _overlaps(a: Session, b: Session) -> bool:
    return a.start < b.end and b.start < a.end


def _check_venue_conflicts(sessions: dict[str, Session], venues: dict[str, dict],
                           activities: dict[str, Activity]) -> None:
    """同一场地的两个场次时间不得重叠；活动容量不得超过场地容量。"""
    by_venue: dict[str, list[Session]] = {}
    for s in sessions.values():
        by_venue.setdefault(s.venue_id, []).append(s)
    for venue_id, items in by_venue.items():
        items.sort(key=lambda s: s.start)
        for earlier, later in zip(items, items[1:]):
            if _overlaps(earlier, later):
                raise CatalogError(
                    f"场地 {venue_id} 场次时间冲突：{earlier.id} 与 {later.id}"
                )
    # 按个人预约的活动，声明容量不得超过所在场地的物理容量；
    # 按家庭发放类活动（如服务台领科普包）容量是发放配额，不受场地座位约束
    for s in sessions.values():
        activity = activities[s.activity_id]
        if activity.booking == "person" and activity.capacity > venues[s.venue_id]["capacity"]:
            raise CatalogError(
                f"场次 {s.id} 容量 {activity.capacity} "
                f"超过场地 {s.venue_id} 容量 {venues[s.venue_id]['capacity']}"
            )


def _check_staff_conflicts(sessions: dict[str, Session]) -> None:
    """同一导师/传承人不得同时出现在两个场次。"""
    by_person: dict[str, list[Session]] = {}
    for s in sessions.values():
        for person_id in (s.mentor_id, s.inheritor_id):
            if person_id:
                by_person.setdefault(person_id, []).append(s)
    for person_id, items in by_person.items():
        items.sort(key=lambda s: s.start)
        for earlier, later in zip(items, items[1:]):
            if _overlaps(earlier, later):
                raise CatalogError(
                    f"人员 {person_id} 场次时间冲突：{earlier.id} 与 {later.id}"
                )


def _check_allocations(allocations: list[dict], sessions: dict[str, Session],
                       batches: dict[str, dict], activities: dict[str, Activity]) -> None:
    """批次分配必须引用真实场次/批次、物料匹配，且不得超过批次入库量。"""
    allocated: dict[str, float] = {}
    for al in allocations:
        sid, bid = al["session_id"], al["batch_id"]
        if sid not in sessions:
            raise CatalogError(f"批次分配引用了未知场次 {sid}")
        if bid not in batches:
            raise CatalogError(f"批次分配引用了未知批次 {bid}")
        batch = batches[bid]
        activity = activities[sessions[sid].activity_id]
        wanted = {m.material for m in activity.materials}
        if batch["material"] not in wanted:
            raise CatalogError(
                f"批次 {bid}（{batch['material']}）不能分配给活动 {activity.id}"
            )
        if batch.get("unit") and any(
            m.material == batch["material"] and m.unit != batch["unit"]
            for m in activity.materials
        ):
            raise CatalogError(f"批次 {bid} 计量单位与活动配方不一致")
        allocated[bid] = allocated.get(bid, 0) + al["qty"]
        if allocated[bid] > batch["qty"] + 1e-9:
            raise CatalogError(
                f"批次 {bid} 超分：已分 {allocated[bid]}，入库 {batch['qty']}"
            )


def _check_devices(checks: list[dict], devices: dict[str, dict],
                   staff: dict[str, dict]) -> None:
    for chk in checks:
        if chk["device_id"] not in devices:
            raise CatalogError(f"设备检查引用了未知设备 {chk['device_id']}")
        if chk["inspector_id"] not in staff:
            raise CatalogError(f"设备检查引用了未知检查人 {chk['inspector_id']}")


def _build_families(raw_families: list[dict]) -> tuple[dict[str, dict], dict[str, Member]]:
    families: dict[str, dict] = {}
    members: dict[str, Member] = {}
    for f in raw_families:
        if f["id"] in families:
            raise CatalogError(f"家庭标识重复：{f['id']}")
        if not f["passports"]:
            raise CatalogError(f"家庭 {f['id']} 至少要有一名成员")
        families[f["id"]] = f
        for p in f["passports"]:
            if p["member_id"] in members:
                raise CatalogError(f"成员标识重复：{p['member_id']}")
            missing_consents = set(CONSENT_LABELS) - set(p["consents"])
            if missing_consents:
                raise CatalogError(
                    f"成员 {p['member_id']} 缺少同意项字段 {sorted(missing_consents)}"
                )
            members[p["member_id"]] = Member(
                member_id=p["member_id"], family_id=f["id"],
                name=p["name"], relation=p["relation"],
                birth_year=p["birth_year"], consents=dict(p["consents"]),
            )
    return families, members
