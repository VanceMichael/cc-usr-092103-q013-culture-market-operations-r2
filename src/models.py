"""公共文化活动保障后台的领域模型。

实体分为两类：

* 排定资料（``EventPlan`` 及其组成部分）——活动前由主办方排定，
  与 ``fixtures/plan.json``、``contracts/plan.schema.json`` 对应；
* 运行状态（预约、物料流水、印章、个人记录）——由
  :class:`src.backend.MarketBackend` 在服务过程中维护。

所有时间统一使用 ISO 8601 字符串（如 ``2026-09-26T10:30:00``），
同日活动可直接按字典序比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Category(str, Enum):
    """活动类别，与领域事实“手作、阅读、非遗、科技”对应。"""

    CRAFT = "craft"        # 手作
    READING = "reading"    # 阅读
    HERITAGE = "heritage"  # 非遗
    TECH = "tech"          # 科技


class Consent(str, Enum):
    """家庭成员须完成的确认事项。"""

    ALLERGY = "allergy"  # 过敏提示
    IMAGE = "image"      # 影像授权
    SAFETY = "safety"    # 安全确认


class DeviceStatus(str, Enum):
    READY = "ready"          # 可用
    DISABLED = "disabled"    # 停用（计划性）
    FAULTY = "faulty"        # 故障（突发性）


class BookingStatus(str, Enum):
    RESERVED = "reserved"        # 已预约
    CHECKED_IN = "checked_in"    # 已扫码核销
    COMPLETED = "completed"      # 已完成体验
    FAILED = "failed"            # 未服务成功（含原因）
    CANCELLED = "cancelled"      # 主动取消
    NO_SHOW = "no_show"          # 爽约


#: 占用名额的预约状态
ACTIVE_BOOKING_STATUSES = {
    BookingStatus.RESERVED,
    BookingStatus.CHECKED_IN,
    BookingStatus.COMPLETED,
}


class MovementKind(str, Enum):
    """物料流水类型。

    库存守恒式：``初始 = 剩余 + 发放 - 归还``；
    去向守恒式：``发放 = 归还 + 损耗 + 在参与者手中``。
    损耗只是已发放物料的去向重分类，不再改变库存。
    """

    ISSUE = "issue"    # 发放给参与者
    RETURN = "return"  # 归还回库
    WASTE = "waste"    # 损耗（已发放但未能完成服务）


@dataclass(frozen=True)
class Venue:
    id: str
    name: str
    capacity: int


@dataclass(frozen=True)
class StaffMember:
    """导师（mentor）或非遗传承人（inheritor）。"""

    id: str
    name: str
    role: str  # "mentor" | "inheritor"


@dataclass(frozen=True)
class Activity:
    """活动模板：流程、同意书要求、年龄限制、单人物料与设备需求。"""

    id: str
    name: str
    category: Category
    required_consents: tuple[Consent, ...] = ()
    min_age: int = 0
    max_age: int | None = None
    materials: dict[str, int] = field(default_factory=dict)  # 批次 id -> 单人用量
    device_type: str | None = None
    record_kind: str | None = None  # "reading" 生成阅读记录 / "science" 生成科普记录


@dataclass(frozen=True)
class MaterialBatch:
    """原料批次：赛后须逐批说明去向。"""

    id: str
    name: str
    unit: str
    initial_quantity: int


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    type: str  # 如 "robot-dog" / "vr-headset"


@dataclass(frozen=True)
class DeviceCheck:
    """开场前的设备检查记录。"""

    device_id: str
    checked_at: str
    inspector: str
    result: str  # "pass" | "fail"
    note: str = ""


@dataclass(frozen=True)
class Session:
    """场次：某活动在某场地、某时段、由某导师/传承人带领的一次开放。"""

    id: str
    activity_id: str
    venue_id: str
    staff_id: str
    start: str
    end: str
    capacity: int
    device_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StampRule:
    """印章规则：完成某类别活动满 N 次，自动发放电子印章。"""

    id: str
    name: str
    category: Category
    required_completions: int


@dataclass(frozen=True)
class Member:
    id: str
    family_id: str
    name: str
    age: int
    consents: frozenset[Consent] = frozenset()


@dataclass(frozen=True)
class Family:
    id: str
    name: str


@dataclass(frozen=True)
class FamilyGroup:
    """结伴：家庭可结伴同行，但个人阅读与科普记录仍按成员独立保存。"""

    id: str
    name: str
    family_ids: tuple[str, ...]


@dataclass(frozen=True)
class EventPlan:
    """一场活动的完整排定资料。"""

    plan_id: str
    version: int
    date: str
    venues: tuple[Venue, ...]
    staff: tuple[StaffMember, ...]
    activities: tuple[Activity, ...]
    batches: tuple[MaterialBatch, ...]
    devices: tuple[Device, ...]
    device_checks: tuple[DeviceCheck, ...]
    sessions: tuple[Session, ...]
    stamp_rules: tuple[StampRule, ...]
    families: tuple[Family, ...]
    members: tuple[Member, ...]
    groups: tuple[FamilyGroup, ...]


# ---- 运行状态 ----


@dataclass
class Booking:
    id: str
    session_id: str
    member_id: str
    status: BookingStatus
    group_id: str | None = None
    failure_reason: str | None = None
    failure_note: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class MaterialMovement:
    """一条物料流水，``ref`` 指向关联的预约或场次。"""

    at: str
    batch_id: str
    kind: MovementKind
    quantity: int
    ref: str
    note: str = ""


@dataclass(frozen=True)
class EventRecord:
    """事件日志条目：断网补传与赛后还原的共同依据。"""

    event_id: str
    kind: str
    at: str
    payload: dict
    result: dict  # {"ok": bool, ...}；被拒绝的操作也如实记录


@dataclass(frozen=True)
class Stamp:
    member_id: str
    rule_id: str
    name: str
    at: str


@dataclass(frozen=True)
class ReadingRecord:
    """个人阅读记录，按成员独立保存，不因结伴而合并。"""

    member_id: str
    session_id: str
    title: str
    at: str


@dataclass(frozen=True)
class ScienceRecord:
    """个人科普记录（如科普资源包领取），按成员独立保存。"""

    member_id: str
    session_id: str
    topic: str
    kit_batch_id: str
    at: str
