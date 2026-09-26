"""运行时后台：预约、扫码服务、同意项、设备状态、断网补传与审计链。

设计要点：

- 每个改变状态的命令都携带客户端生成的 request_id；服务端按 request_id
  幂等，断网补传、重复扫码、双击重试都不会重复扣名额或物料。
- 服务（扫码）成功才消耗物料与名额；任何拒绝（缺同意项、超龄、满场、
  设备停用、物料不足、重复）都记入未服务台账并给出原因，不消耗资源。
- 有效容量 = min(活动容量, 场地容量, 在用设备折算容量)。设备停用立即
  收紧后续预约与服务，已服务人数不受影响但绝不新增超额服务。
- 所有状态变化追加到哈希链审计日志，可离线校验完整性。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .catalog import CONSENT_LABELS, Catalog
from .ledger import LedgerError, MaterialLedger

# 未服务成功的原因代码
REASON_DUPLICATE = "duplicate_scan"        # 重复扫码（已服务过）
REASON_CONSENT = "consent_missing"         # 同意项未完成
REASON_AGE = "under_age"                   # 未达年龄限制
REASON_CAPACITY = "capacity_full"          # 名额已满
REASON_DEVICE = "device_down"              # 设备停用导致无法服务
REASON_MATERIAL = "material_short"         # 物料不足
REASON_NO_BOOKING = "no_booking"           # 无预约且现场候补失败
REASON_NO_SHOW = "no_show"                 # 闭场时预约未到
REASON_CANCELLED = "cancelled"             # 家庭主动取消
REASON_TRANSFERRED = "transferred"         # 已换场（原场次名额释放）

REASON_LABELS = {
    REASON_DUPLICATE: "重复扫码",
    REASON_CONSENT: "同意项未完成",
    REASON_AGE: "未达年龄限制",
    REASON_CAPACITY: "名额已满",
    REASON_DEVICE: "设备停用",
    REASON_MATERIAL: "物料不足",
    REASON_NO_BOOKING: "无预约",
    REASON_NO_SHOW: "预约未到",
    REASON_CANCELLED: "主动取消",
    REASON_TRANSFERRED: "已换场",
}

STATUS_LABELS = {
    "booked": "已预约",
    "served": "已完成",
    "cancelled": "已取消",
    "transferred": "已换场",
    "no_show": "未到场",
    "unfulfilled": "到场未履约",
}


class BackendError(ValueError):
    """命令参数非法。"""


@dataclass
class Booking:
    id: str
    session_id: str
    member_id: str
    family_id: str
    group_id: str | None      # 结伴同组标识；个人预约为 None
    status: str = "booked"    # booked|served|cancelled|transferred|no_show
    booked_at: str = ""
    served_at: str | None = None
    close_reason: str | None = None


@dataclass
class DeviceState:
    id: str
    status: str = "active"    # active|down
    reason: str = ""
    since: str = ""


class Backend:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.ledger = MaterialLedger(catalog)
        self.bookings: dict[str, Booking] = {}
        self._request_log: dict[str, dict] = {}     # request_id -> 结果（幂等）
        self._booking_seq = 0
        self.audit: list[dict] = []
        self.unserved: list[dict] = []              # 未服务成功台账
        self.stamps: dict[str, list[dict]] = {}     # member_id -> 印章
        self.readings: dict[str, list[dict]] = {}   # member_id -> 阅读记录
        self.science: dict[str, list[dict]] = {}    # member_id -> 科普记录
        self.kit_issued: set[str] = set()           # 已领科普包的家庭
        self.devices: dict[str, DeviceState] = {}
        self._init_devices()
        self.closed = False

    # ================================================================
    # 初始化
    # ================================================================
    def _init_devices(self) -> None:
        latest: dict[str, dict] = {}
        for chk in self.catalog.device_checks:
            prev = latest.get(chk["device_id"])
            if prev is None or chk["at"] > prev["at"]:
                latest[chk["device_id"]] = chk
        for did in self.catalog.devices:
            chk = latest.get(did)
            if chk and chk["result"] == "fail":
                self.devices[did] = DeviceState(
                    id=did, status="down",
                    reason=chk.get("note", "开场检查不合格"), since=chk["at"],
                )
                self._audit("device_down", device_id=did, reason=chk.get("note", ""),
                            at=chk["at"], inspector=chk["inspector_id"], initial=True)
            else:
                self.devices[did] = DeviceState(id=did)

    # ================================================================
    # 审计链
    # ================================================================
    def _audit(self, kind: str, **payload) -> dict:
        prev = self.audit[-1]["hash"] if self.audit else "0" * 64
        event = {"seq": len(self.audit), "kind": kind, "prev": prev, **payload}
        event["hash"] = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.audit.append(event)
        return event

    def verify_audit_chain(self) -> bool:
        prev = "0" * 64
        for i, event in enumerate(self.audit):
            body = {k: v for k, v in event.items() if k != "hash"}
            if body["seq"] != i or body["prev"] != prev:
                return False
            digest = hashlib.sha256(
                json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if digest != event["hash"]:
                return False
            prev = event["hash"]
        return True

    # ================================================================
    # 容量
    # ================================================================
    def effective_capacity(self, session_id: str) -> int:
        session = self.catalog.sessions[session_id]
        activity = self.catalog.activities[session.activity_id]
        caps = [activity.capacity]
        if activity.booking == "person":
            # 个人预约活动受场地物理容量约束；家庭发放类活动的容量是
            # 发放配额（如 200 份科普包），不受服务台座位数限制
            caps.append(self.catalog.venues[session.venue_id]["capacity"])
        devices = self.catalog.session_devices(activity.id)
        if devices:
            slots = sum(
                d["slots"] for d in devices if self.devices[d["id"]].status == "active"
            )
            caps.append(slots)
        return min(caps)

    def session_counts(self, session_id: str) -> dict[str, int]:
        booked = served = 0
        for b in self.bookings.values():
            if b.session_id != session_id:
                continue
            if b.status == "booked":
                booked += 1
            elif b.status == "served":
                served += 1
        return {"booked": booked, "served": served,
                "capacity": self.effective_capacity(session_id),
                "remaining": self.effective_capacity(session_id) - booked - served}

    # ================================================================
    # 幂等入口
    # ================================================================
    def _run(self, request_id: str, op: str, at: str, fn):
        if not request_id:
            raise BackendError("缺少 request_id（幂等键）")
        if request_id in self._request_log:
            cached = dict(self._request_log[request_id])
            cached["replayed"] = True
            return cached
        result = fn()
        result["op"] = op
        result["at"] = at
        self._request_log[request_id] = result
        return dict(result)

    def _reject_unserved(self, session_id: str, member_id: str | None,
                         reason: str, at: str, detail: str = "") -> dict:
        entry = {
            "session_id": session_id, "member_id": member_id,
            "reason": reason, "reason_label": REASON_LABELS[reason],
            "at": at, "detail": detail,
        }
        self.unserved.append(entry)
        self._audit("unserved", **entry)
        return {"ok": False, "reason": reason,
                "reason_label": REASON_LABELS[reason], "detail": detail}

    # ================================================================
    # 同意项
    # ================================================================
    def update_consent(self, request_id: str, member_id: str,
                       consent: str, value: bool, at: str) -> dict:
        def do():
            member = self._member(member_id)
            if consent not in CONSENT_LABELS:
                raise BackendError(f"未知同意项 {consent}")
            member.consents[consent] = bool(value)
            self._audit("consent", member_id=member_id, consent=consent,
                        value=bool(value), at=at)
            return {"ok": True, "member_id": member_id, "consent": consent,
                    "value": bool(value)}
        return self._run(request_id, "update_consent", at, do)

    def missing_consents(self, member_id: str, activity_id: str) -> list[str]:
        member = self._member(member_id)
        activity = self.catalog.activities[activity_id]
        return [c for c in activity.required_consents if not member.consents.get(c)]

    # ================================================================
    # 预约（个人 / 家庭结伴）
    # ================================================================
    def book(self, request_id: str, session_id: str, member_ids: list[str],
             at: str, group_id: str | None = None) -> dict:
        """为一批成员预约同一场次；结伴时整组同进同出，记录仍按人独立。"""
        def do():
            session = self._session(session_id)
            activity = self.catalog.activities[session.activity_id]
            if self.closed:
                raise BackendError("活动已闭场")
            members = [self._member(m) for m in member_ids]
            if not members:
                raise BackendError("预约成员不能为空")
            if activity.booking == "family":
                family_ids = {m.family_id for m in members}
                if len(family_ids) != 1:
                    raise BackendError("发放类活动按家庭预约，成员须同属一个家庭")
                fid = members[0].family_id
                if fid in self.kit_issued or any(
                    b.session_id == session_id
                    and b.status in ("booked", "served")
                    and b.family_id == fid
                    for b in self.bookings.values()
                ):
                    return self._reject_unserved(
                        session_id, None, REASON_DUPLICATE, at,
                        f"{self.catalog.families[fid]['name']}"
                        f"在该发放场次已有有效预约或已领取")
            # 年龄、重复预约与时间冲突检查
            for m in members:
                age = self.catalog.age_of(m)
                if age < activity.min_age:
                    return self._reject_unserved(
                        session_id, m.member_id, REASON_AGE, at,
                        f"{m.name} {age} 岁，未达 {activity.name} 年龄下限 "
                        f"{activity.min_age} 岁")
                if self._active_booking(m.member_id, session_id):
                    return self._reject_unserved(
                        session_id, m.member_id, REASON_DUPLICATE, at,
                        f"{m.name} 在该场次已有有效预约或服务记录")
                clash = self._time_clash(m.member_id, session)
                if clash is not None:
                    return self._reject_unserved(
                        session_id, m.member_id, REASON_CAPACITY, at,
                        f"{m.name} 与已预约场次 {clash} 时间冲突")
            # 容量：整组要么全部成功要么全部失败，不出现半组占位
            counts = self.session_counts(session_id)
            if counts["remaining"] < len(members):
                return self._reject_unserved(
                    session_id, None, REASON_CAPACITY, at,
                    f"{session_id} 剩余 {counts['remaining']} 个名额，"
                    f"本组需要 {len(members)} 个")
            made = []
            for m in members:
                booking = self._new_booking(session_id, m, at, group_id)
                made.append(booking.id)
                self._audit("book", booking_id=booking.id, session_id=session_id,
                            member_id=m.member_id, family_id=m.family_id,
                            group_id=group_id, at=at)
            return {"ok": True, "booking_ids": made, "session_id": session_id}
        return self._run(request_id, "book", at, do)

    def cancel(self, request_id: str, booking_id: str, at: str) -> dict:
        def do():
            booking = self._booking(booking_id)
            if booking.status != "booked":
                raise BackendError(f"预约 {booking_id} 当前状态不可取消：{booking.status}")
            booking.status = "cancelled"
            booking.close_reason = REASON_CANCELLED
            self._audit("cancel", booking_id=booking_id, at=at)
            return {"ok": True, "booking_id": booking_id}
        return self._run(request_id, "cancel", at, do)

    def transfer(self, request_id: str, booking_id: str,
                 new_session_id: str, at: str) -> dict:
        """临时换场：原子地释放原场次名额并占用新场次名额。"""
        def do():
            booking = self._booking(booking_id)
            if booking.status != "booked":
                raise BackendError(f"预约 {booking_id} 当前状态不可换场：{booking.status}")
            new_session = self._session(new_session_id)
            activity = self.catalog.activities[new_session.activity_id]
            member = self._member(booking.member_id)
            age = self.catalog.age_of(member)
            if age < activity.min_age:
                return self._reject_unserved(
                    new_session_id, member.member_id, REASON_AGE, at,
                    f"{member.name} 未达 {activity.name} 年龄下限")
            if self._active_booking(member.member_id, new_session_id):
                return self._reject_unserved(
                    new_session_id, member.member_id, REASON_DUPLICATE, at,
                    "目标场次已有该成员的有效预约")
            clash = self._time_clash(member.member_id, new_session)
            if clash is not None:
                return self._reject_unserved(
                    new_session_id, member.member_id, REASON_CAPACITY, at,
                    f"目标场次与已预约场次 {clash} 时间冲突")
            counts = self.session_counts(new_session_id)
            if counts["remaining"] < 1:
                return self._reject_unserved(
                    new_session_id, member.member_id, REASON_CAPACITY, at,
                    f"目标场次 {new_session_id} 名额已满")
            old_session = booking.session_id
            booking.status = "transferred"
            booking.close_reason = REASON_TRANSFERRED
            moved = self._new_booking(new_session_id, member, at, booking.group_id)
            self._audit("transfer", booking_id=booking_id, new_booking_id=moved.id,
                        from_session=old_session, to_session=new_session_id,
                        member_id=member.member_id, at=at)
            return {"ok": True, "released": booking_id, "booking_id": moved.id,
                    "session_id": new_session_id}
        return self._run(request_id, "transfer", at, do)

    # ================================================================
    # 扫码服务
    # ================================================================
    def serve(self, request_id: str, session_id: str, member_id: str,
              at: str, reading: dict | None = None,
              allow_walkin: bool = True) -> dict:
        """扫码核验并发放物料/盖章。成功才消耗资源，失败只记台账。"""
        def do():
            session = self._session(session_id)
            activity = self.catalog.activities[session.activity_id]
            member = self._member(member_id)
            if self.closed:
                raise BackendError("活动已闭场")

            # 1) 重复扫码：同人同场次已服务过；家庭类发放按家庭防重领
            prior = self._served_booking(member_id, session_id)
            if prior is not None:
                return self._reject_unserved(
                    session_id, member_id, REASON_DUPLICATE, at,
                    f"{member.name} 在该场次已于 {prior.served_at} 完成服务")
            if activity.booking == "family" and member.family_id in self.kit_issued:
                return self._reject_unserved(
                    session_id, member_id, REASON_DUPLICATE, at,
                    f"{self.catalog.families[member.family_id]['name']}"
                    f"已领取过科普资源包，每家庭限领一份")

            # 2) 预约或现场候补：候补只登记意图，全部核验通过后才落库占位
            booking = self._active_booking(member_id, session_id)
            walkin = False
            if booking is None:
                if not allow_walkin or activity.booking == "family":
                    return self._reject_unserved(
                        session_id, member_id, REASON_NO_BOOKING, at,
                        "无有效预约，且该场次不接受现场候补")
                clash = self._time_clash(member_id, session)
                if clash is not None:
                    return self._reject_unserved(
                        session_id, member_id, REASON_CAPACITY, at,
                        f"现场候补失败：与已预约场次 {clash} 时间冲突")
                # 候补只能使用未被预约的空闲名额，不能挤占已预约者
                if self.session_counts(session_id)["remaining"] < 1:
                    return self._reject_unserved(
                        session_id, member_id, REASON_CAPACITY, at,
                        "现场候补失败：名额已满")
                walkin = True

            # 3) 年龄
            age = self.catalog.age_of(member)
            if age < activity.min_age:
                return self._reject_unserved(
                    session_id, member_id, REASON_AGE, at,
                    f"{member.name} {age} 岁，未达 {activity.name} 年龄下限 "
                    f"{activity.min_age} 岁")

            # 4) 同意项
            missing = self.missing_consents(member_id, activity.id)
            if missing:
                labels = "、".join(CONSENT_LABELS[c] for c in missing)
                return self._reject_unserved(
                    session_id, member_id, REASON_CONSENT, at,
                    f"{member.name} 尚未完成：{labels}")

            # 5) 名额（设备停用后有效容量可能小于已预约数，一律不得突破）
            counts = self.session_counts(session_id)
            if counts["served"] >= counts["capacity"]:
                devices = self.catalog.session_devices(activity.id)
                reason = REASON_DEVICE if devices and any(
                    self.devices[d["id"]].status == "down" for d in devices
                ) else REASON_CAPACITY
                return self._reject_unserved(
                    session_id, member_id, reason, at,
                    f"{session_id} 有效容量 {counts['capacity']} 已用完")

            # 6) 物料：先整体预检再统一扣减，保证“要么全套发齐、要么一套不发”
            stock = self.ledger.stocks[session_id]
            short = next(
                (spec for spec in activity.materials
                 if stock.available(spec.material) + 1e-9 < spec.qty),
                None,
            )
            if short is not None:
                return self._reject_unserved(
                    session_id, member_id, REASON_MATERIAL, at,
                    f"{session_id} {short.material} 在场可用 "
                    f"{stock.available(short.material)}，每份需要 {short.qty}")

            # 全部核验通过：现场候补此时才占位
            if walkin:
                booking = self._new_booking(session_id, member, at, None)
                self._audit("walkin_book", booking_id=booking.id,
                            session_id=session_id, member_id=member_id, at=at)

            # 7) 扣减物料、完成服务
            issued: dict[str, dict[str, float]] = {}
            for spec in activity.materials:
                try:
                    issued[spec.material] = self.ledger.issue(
                        session_id, spec.material, spec.qty)
                except LedgerError as exc:  # 预检通过后不可达，防御性兜底
                    return self._reject_unserved(
                        session_id, member_id, REASON_MATERIAL, at, str(exc))

            # 7) 完成服务：名额、印章、独立记录
            booking.status = "served"
            booking.served_at = at
            stamp = None
            if activity.stamp_code:
                stamp = {"activity_id": activity.id, "code": activity.stamp_code,
                         "session_id": session_id, "at": at}
                self.stamps.setdefault(member_id, []).append(stamp)
            if "reading" in activity.records:
                entry = {"session_id": session_id, "at": at,
                         "title": (reading or {}).get("title", ""),
                         "minutes": (reading or {}).get("minutes", 0)}
                self.readings.setdefault(member_id, []).append(entry)
            if "science_per_child" in activity.records:
                self.kit_issued.add(member.family_id)
                for m in self.catalog.members.values():
                    if m.family_id == member.family_id and self.catalog.age_of(m) < 18:
                        self.science.setdefault(m.member_id, []).append(
                            {"session_id": session_id, "at": at,
                             "task": "科普资源包任务", "status": "issued"})
            self._audit("serve", booking_id=booking.id, session_id=session_id,
                        member_id=member_id, family_id=member.family_id,
                        activity_id=activity.id, issued=issued,
                        stamp=stamp, at=at)
            return {"ok": True, "booking_id": booking.id, "session_id": session_id,
                    "member_id": member_id, "issued": issued,
                    "stamp": stamp["code"] if stamp else None}
        return self._run(request_id, "serve", at, do)

    # ================================================================
    # 设备
    # ================================================================
    def set_device_status(self, request_id: str, device_id: str,
                          status: str, at: str, reason: str = "") -> dict:
        def do():
            if device_id not in self.devices:
                raise BackendError(f"未知设备 {device_id}")
            if status not in ("active", "down"):
                raise BackendError("设备状态只能是 active 或 down")
            state = self.devices[device_id]
            if state.status == status:
                return {"ok": True, "device_id": device_id, "status": status,
                        "unchanged": True}
            state.status = status
            state.reason = reason
            state.since = at
            self._audit("device_down" if status == "down" else "device_up",
                        device_id=device_id, reason=reason, at=at)
            return {"ok": True, "device_id": device_id, "status": status}
        return self._run(request_id, "set_device_status", at, do)

    # ================================================================
    # 物料运维
    # ================================================================
    def record_waste(self, request_id: str, session_id: str, material: str,
                     qty: float, at: str, reason: str = "") -> dict:
        def do():
            self._session(session_id)
            try:
                picked = self.ledger.waste(session_id, material, qty)
            except LedgerError as exc:
                raise BackendError(str(exc)) from exc
            self._audit("waste", session_id=session_id, material=material,
                        qty=qty, batches=picked, reason=reason, at=at)
            return {"ok": True, "session_id": session_id, "material": material,
                    "qty": qty, "batches": picked}
        return self._run(request_id, "record_waste", at, do)

    def move_stock(self, request_id: str, from_session: str, to_session: str,
                   material: str, batch_id: str, qty: float, at: str) -> dict:
        """临时换场/调剂：把未用物料从一个场次退回并改拨另一场次。"""
        def do():
            self._session(from_session)
            self._session(to_session)
            try:
                self.ledger.return_unused(from_session, material, batch_id, qty)
                self.ledger.allocate(to_session, batch_id, qty)
            except LedgerError as exc:
                raise BackendError(str(exc)) from exc
            self._audit("move_stock", from_session=from_session,
                        to_session=to_session, material=material,
                        batch_id=batch_id, qty=qty, at=at)
            return {"ok": True, "from": from_session, "to": to_session,
                    "material": material, "qty": qty}
        return self._run(request_id, "move_stock", at, do)

    # ================================================================
    # 断网补传
    # ================================================================
    def sync(self, queued: list[dict]) -> list[dict]:
        """按客户端顺序补传离线期间产生的命令。

        每条命令自带 request_id 与业务参数；已处理过的直接返回缓存结果，
        与在线状态冲突的按当前状态拒绝并记入未服务台账。容量与物料守恒
        由 serve/book 内部的实时校验保证，补传不会突破。
        """
        results = []
        for cmd in queued:
            op = cmd["op"]
            args = {k: v for k, v in cmd.items() if k != "op"}
            handler = getattr(self, op, None)
            if handler is None or op.startswith("_") or op == "sync":
                raise BackendError(f"不可补传的操作 {op}")
            results.append(handler(**args))
        return results

    # ================================================================
    # 闭场
    # ================================================================
    def close(self, at: str) -> dict:
        """闭场：未履约预约记 no_show，未用物料退回中央池。"""
        if self.closed:
            raise BackendError("活动已闭场")
        self.closed = True
        for booking in self.bookings.values():
            if booking.status != "booked":
                continue
            # 已有到场扫码但被拦（设备停用、满场、同意项、年龄、物料等）：
            # 归因为最后一次真实拦截原因，不再重复记一笔 no_show
            attempts = [
                e for e in self.unserved
                if e["member_id"] == booking.member_id
                and e["session_id"] == booking.session_id
            ]
            if attempts:
                booking.status = "unfulfilled"
                booking.close_reason = attempts[-1]["reason"]
                continue
            booking.status = "no_show"
            booking.close_reason = REASON_NO_SHOW
            self.unserved.append({
                "session_id": booking.session_id,
                "member_id": booking.member_id,
                "reason": REASON_NO_SHOW,
                "reason_label": REASON_LABELS[REASON_NO_SHOW],
                "at": at, "detail": f"预约 {booking.id} 闭场时未到场",
            })
        for sid in list(self.ledger.stocks):
            usage = self.ledger.session_usage(sid)
            for material, u in usage.items():
                if u["available"] > 0:
                    for bid in list(self.ledger.stocks[sid].allocated.get(material, {})):
                        free = self.ledger.stocks[sid].free_of(material, bid)
                        if free > 0:
                            self.ledger.return_unused(sid, material, bid, free)
        self._audit("close", at=at)
        return {"ok": True, "closed_at": at}

    # ================================================================
    # 科普记录（独立按人）
    # ================================================================
    def complete_science_task(self, request_id: str, member_id: str,
                              at: str, note: str = "") -> dict:
        """儿童完成科普包任务后登记，只更新本人的独立记录。"""
        def do():
            member = self._member(member_id)
            records = self.science.get(member_id, [])
            if not records:
                raise BackendError(f"{member.name} 尚未领取科普资源包")
            records.append({"task": "科普资源包任务", "status": "done",
                            "at": at, "note": note})
            self._audit("science_done", member_id=member_id, at=at, note=note)
            return {"ok": True, "member_id": member_id}
        return self._run(request_id, "complete_science_task", at, do)

    # ================================================================
    # 内部工具
    # ================================================================
    def _new_booking(self, session_id: str, member, at: str,
                     group_id: str | None) -> Booking:
        self._booking_seq += 1
        booking = Booking(
            id=f"BK-{self._booking_seq:04d}", session_id=session_id,
            member_id=member.member_id, family_id=member.family_id,
            group_id=group_id, booked_at=at,
        )
        self.bookings[booking.id] = booking
        return booking

    def _active_booking(self, member_id: str, session_id: str) -> Booking | None:
        for b in self.bookings.values():
            if (b.member_id == member_id and b.session_id == session_id
                    and b.status == "booked"):
                return b
        return None

    def _served_booking(self, member_id: str, session_id: str) -> Booking | None:
        for b in self.bookings.values():
            if (b.member_id == member_id and b.session_id == session_id
                    and b.status == "served"):
                return b
        return None

    def _time_clash(self, member_id: str, target) -> str | None:
        """成员已有预约与目标场次时间重叠时，返回冲突场次标识。"""
        target_activity = self.catalog.activities[target.activity_id]
        if target_activity.booking == "family":
            return None  # 发放窗口与游玩场次允许并行
        for b in self.bookings.values():
            if b.member_id != member_id or b.status != "booked":
                continue
            other = self.catalog.sessions[b.session_id]
            if self.catalog.activities[other.activity_id].booking == "family":
                continue
            if other.start < target.end and target.start < other.end:
                return b.session_id
        return None

    def _session(self, session_id: str):
        session = self.catalog.sessions.get(session_id)
        if session is None:
            raise BackendError(f"未知场次 {session_id}")
        return session

    def _member(self, member_id: str):
        member = self.catalog.members.get(member_id)
        if member is None:
            raise BackendError(f"未知成员 {member_id}")
        return member

    def _booking(self, booking_id: str) -> Booking:
        booking = self.bookings.get(booking_id)
        if booking is None:
            raise BackendError(f"未知预约 {booking_id}")
        return booking
