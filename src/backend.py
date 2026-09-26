"""公共文化活动保障后台核心服务。

设计要点：

* **一切操作皆事件**：预约、扫码核销、完成、补录同意书、换场、
  设备停用/故障、场次取消等都通过 :meth:`MarketBackend.apply` 进入，
  逐条写入事件日志。断网补传就是把离线期间积攒的事件原样重放。
* **幂等**：同一 ``event_id`` 只生效一次；同一成员在同一场次的
  预约/核销按自然键去重，重复扫码不会重复占名额、重复领物料。
* **守恒**：名额不超过场次与场地容量；每批物料满足
  ``初始 = 剩余 + 发放 - 归还``（损耗只是已发放物料的去向重分类），
  任何异常路径（换场、设备故障、场次取消）都只能在这两条守恒式内
  移动资源。
"""

from __future__ import annotations

import uuid

from . import errors
from .errors import DomainError
from .models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    BookingStatus,
    DeviceStatus,
    EventPlan,
    EventRecord,
    MaterialMovement,
    MovementKind,
    ReadingRecord,
    ScienceRecord,
    Stamp,
)


def _new_event_id() -> str:
    return uuid.uuid4().hex


class MarketBackend:
    """一场公共文化活动的运行后台。"""

    def __init__(self, plan: EventPlan):
        self.plan = plan
        self._venues = {v.id: v for v in plan.venues}
        self._activities = {a.id: a for a in plan.activities}
        self._sessions = {s.id: s for s in plan.sessions}
        self._members = {m.id: m for m in plan.members}
        self._families = {f.id: f for f in plan.families}
        self._groups = {g.id: g for g in plan.groups}
        self._batches = {b.id: b for b in plan.batches}
        self._devices = {d.id: d for d in plan.devices}
        self._stamp_rules = {r.id: r for r in plan.stamp_rules}

        # 运行状态（排定资料保持只读）
        self._remaining = {b.id: b.initial_quantity for b in plan.batches}
        self._device_status = {d.id: DeviceStatus.READY for d in plan.devices}
        self._device_note = {d.id: "" for d in plan.devices}
        self._session_venue = {s.id: s.venue_id for s in plan.sessions}
        self._session_devices = {s.id: list(s.device_ids) for s in plan.sessions}
        self._session_cancelled: dict[str, str] = {}  # session_id -> 原因
        self._consents = {m.id: set(m.consents) for m in plan.members}

        self._bookings: dict[str, Booking] = {}
        self._booking_by_key: dict[tuple[str, str], str] = {}  # (session, member) -> booking
        self._movements: list[MaterialMovement] = []
        self._stamps: list[Stamp] = []
        self._stamp_keys: set[tuple[str, str]] = set()
        self._reading: list[ReadingRecord] = []
        self._science: list[ScienceRecord] = []

        self._log: list[EventRecord] = []
        self._applied: dict[str, dict] = {}

        self._handlers = {
            "book": self._do_book,
            "book_group": self._do_book_group,
            "cancel_booking": self._do_cancel_booking,
            "check_in": self._do_check_in,
            "complete": self._do_complete,
            "no_show": self._do_no_show,
            "record_consent": self._do_record_consent,
            "change_venue": self._do_change_venue,
            "device_status": self._do_device_status,
            "swap_device": self._do_swap_device,
            "fail_session": self._do_fail_session,
        }

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------

    def apply(self, event: dict) -> dict:
        """应用一条事件并返回结果记录；同一 event_id 重复提交直接返回首次结果。"""
        event_id = event.get("event_id")
        if not event_id:
            raise ValueError("事件缺少 event_id")
        if event_id in self._applied:
            return self._applied[event_id]
        kind = event.get("kind")
        handler = self._handlers.get(kind)
        if handler is None:
            raise ValueError(f"未知事件类型: {kind}")
        payload = event.get("payload", {})
        at = event.get("at", "")
        try:
            result = handler(payload, at, event_id)
            record = {"ok": True, **result}
        except DomainError as exc:
            record = {"ok": False, "reason": exc.reason, "detail": str(exc)}
        self._log.append(
            EventRecord(event_id=event_id, kind=kind, at=at, payload=payload, result=record)
        )
        self._applied[event_id] = record
        return record

    def apply_batch(self, events: list[dict]) -> list[dict]:
        """断网补传入口：按到达顺序应用一批离线事件，幂等去重。"""
        return [self.apply(e) for e in events]

    def _submit(self, kind: str, payload: dict, at: str, event_id: str | None) -> dict:
        event = {
            "event_id": event_id or _new_event_id(),
            "kind": kind,
            "at": at,
            "payload": payload,
        }
        return self.apply(event)

    # ------------------------------------------------------------------
    # 对外操作（均为事件的便捷封装）
    # ------------------------------------------------------------------

    def book(self, session_id: str, member_id: str, at: str, group_id: str | None = None,
             event_id: str | None = None) -> dict:
        return self._submit(
            "book",
            {"session_id": session_id, "member_id": member_id, "group_id": group_id},
            at,
            event_id,
        )

    def book_group(self, session_id: str, group_id: str, at: str,
                   event_id: str | None = None) -> dict:
        """结伴预约：同组家庭全体成员同行，名额不足则整组不约。"""
        return self._submit(
            "book_group", {"session_id": session_id, "group_id": group_id}, at, event_id
        )

    def cancel_booking(self, booking_id: str, at: str, event_id: str | None = None) -> dict:
        return self._submit("cancel_booking", {"booking_id": booking_id}, at, event_id)

    def check_in(self, session_id: str, member_id: str, at: str,
                 event_id: str | None = None) -> dict:
        """扫码核销；未预约的现场观众在名额允许时自动补建预约。"""
        return self._submit(
            "check_in", {"session_id": session_id, "member_id": member_id}, at, event_id
        )

    def complete(self, booking_id: str | None = None, at: str = "",
                 session_id: str | None = None, member_id: str | None = None,
                 event_id: str | None = None) -> dict:
        return self._submit(
            "complete",
            {"booking_id": booking_id, "session_id": session_id, "member_id": member_id},
            at,
            event_id,
        )

    def no_show(self, booking_id: str, at: str, event_id: str | None = None) -> dict:
        return self._submit("no_show", {"booking_id": booking_id}, at, event_id)

    def record_consent(self, member_id: str, consent: str, at: str,
                       event_id: str | None = None) -> dict:
        return self._submit(
            "record_consent", {"member_id": member_id, "consent": consent}, at, event_id
        )

    def change_venue(self, session_id: str, venue_id: str, reason: str, at: str,
                     event_id: str | None = None) -> dict:
        return self._submit(
            "change_venue",
            {"session_id": session_id, "venue_id": venue_id, "reason": reason},
            at,
            event_id,
        )

    def set_device_status(self, device_id: str, status: str, note: str, at: str,
                          event_id: str | None = None) -> dict:
        return self._submit(
            "device_status",
            {"device_id": device_id, "status": status, "note": note},
            at,
            event_id,
        )

    def swap_device(self, session_id: str, old_device_id: str, new_device_id: str,
                    at: str, event_id: str | None = None) -> dict:
        return self._submit(
            "swap_device",
            {"session_id": session_id, "old_device_id": old_device_id,
             "new_device_id": new_device_id},
            at,
            event_id,
        )

    def fail_session(self, session_id: str, reason: str, note: str, at: str,
                     salvage_materials: bool = False, event_id: str | None = None) -> dict:
        """取消场次：未核销预约记为未服务成功；已核销者的物料按损耗（或归还）入账。"""
        return self._submit(
            "fail_session",
            {"session_id": session_id, "reason": reason, "note": note,
             "salvage_materials": salvage_materials},
            at,
            event_id,
        )

    # ------------------------------------------------------------------
    # 事件处理器
    # ------------------------------------------------------------------

    def _do_book(self, p: dict, at: str, event_id: str) -> dict:
        session = self._session(p["session_id"])
        member = self._member(p["member_id"])
        existing = self._active_booking(session.id, member.id)
        if existing is not None:
            return {"booking_id": existing.id, "deduplicated": True,
                    "warnings": self._consent_warnings(session, member)}
        self._check_bookable(session, member)
        booking = Booking(
            id=f"bk-{event_id}",
            session_id=session.id,
            member_id=member.id,
            status=BookingStatus.RESERVED,
            group_id=p.get("group_id"),
            created_at=at,
        )
        self._register(booking)
        return {"booking_id": booking.id, "warnings": self._consent_warnings(session, member)}

    def _do_book_group(self, p: dict, at: str, event_id: str) -> dict:
        session = self._session(p["session_id"])
        group = self._groups.get(p["group_id"])
        if group is None:
            raise DomainError(errors.NOT_FOUND, f"结伴不存在: {p['group_id']}")
        members = [m for m in self._members.values() if m.family_id in group.family_ids]
        if not members:
            raise DomainError(errors.NOT_FOUND, f"结伴 {group.id} 没有可预约的成员")
        self._check_open(session)
        # 整组校验：任何人年龄不符或名额不足，整组不约
        for m in members:
            if self._active_booking(session.id, m.id) is None:
                self._check_age(session, m)
        needed = sum(1 for m in members if self._active_booking(session.id, m.id) is None)
        self._check_capacity(session, needed)
        bookings = []
        for m in members:
            existing = self._active_booking(session.id, m.id)
            if existing is not None:
                bookings.append(existing.id)
                continue
            booking = Booking(
                id=f"bk-{event_id}-{m.id}",
                session_id=session.id,
                member_id=m.id,
                status=BookingStatus.RESERVED,
                group_id=group.id,
                created_at=at,
            )
            self._register(booking)
            bookings.append(booking.id)
        return {
            "booking_ids": bookings,
            "warnings": {
                m.id: self._consent_warnings(session, m) for m in members
            },
        }

    def _do_cancel_booking(self, p: dict, at: str, event_id: str) -> dict:
        booking = self._booking(p["booking_id"])
        if booking.status in (BookingStatus.CANCELLED,):
            return {"booking_id": booking.id, "deduplicated": True}
        if booking.status in (BookingStatus.COMPLETED, BookingStatus.FAILED,
                              BookingStatus.NO_SHOW):
            raise DomainError(
                errors.BOOKING_STATE, f"预约 {booking.id} 当前状态不可取消: {booking.status.value}"
            )
        if booking.status == BookingStatus.CHECKED_IN:
            self._return_materials(booking, at, "取消预约归还物料")
        booking.status = BookingStatus.CANCELLED
        return {"booking_id": booking.id}

    def _do_check_in(self, p: dict, at: str, event_id: str) -> dict:
        session = self._session(p["session_id"])
        member = self._member(p["member_id"])
        booking = self._active_booking(session.id, member.id)
        if booking is not None and booking.status in (
            BookingStatus.CHECKED_IN, BookingStatus.COMPLETED,
        ):
            # 重复扫码：直接返回首次核销结果，不重复发物料
            return {"booking_id": booking.id, "deduplicated": True}
        self._check_open(session)
        self._check_age(session, member)
        self._check_consents(session, member)
        self._check_devices(session)
        if booking is None:
            # 现场未预约观众：名额允许时补建预约
            self._check_capacity(session, 1)
            booking = Booking(
                id=f"bk-{event_id}",
                session_id=session.id,
                member_id=member.id,
                status=BookingStatus.RESERVED,
                created_at=at,
            )
            self._register(booking)
        self._issue_materials(session, booking, at)
        booking.status = BookingStatus.CHECKED_IN
        return {"booking_id": booking.id}

    def _do_complete(self, p: dict, at: str, event_id: str) -> dict:
        booking = self._resolve_booking(p)
        if booking.status == BookingStatus.COMPLETED:
            return {"booking_id": booking.id, "deduplicated": True}
        if booking.status != BookingStatus.CHECKED_IN:
            raise DomainError(
                errors.BOOKING_STATE,
                f"预约 {booking.id} 未核销，不能登记完成（当前 {booking.status.value}）",
            )
        booking.status = BookingStatus.COMPLETED
        session = self._sessions[booking.session_id]
        activity = self._activities[session.activity_id]
        result: dict = {"booking_id": booking.id}
        if activity.record_kind == "reading":
            self._reading.append(
                ReadingRecord(member_id=booking.member_id, session_id=session.id,
                              title=activity.name, at=at)
            )
            result["reading_record"] = True
        elif activity.record_kind == "science":
            kit_batch = next(iter(activity.materials), "")
            self._science.append(
                ScienceRecord(member_id=booking.member_id, session_id=session.id,
                              topic=activity.name, kit_batch_id=kit_batch, at=at)
            )
            result["science_record"] = True
        new_stamps = self._evaluate_stamps(booking.member_id, at)
        if new_stamps:
            result["stamps"] = [s.name for s in new_stamps]
        return result

    def _do_no_show(self, p: dict, at: str, event_id: str) -> dict:
        booking = self._booking(p["booking_id"])
        if booking.status == BookingStatus.NO_SHOW:
            return {"booking_id": booking.id, "deduplicated": True}
        if booking.status != BookingStatus.RESERVED:
            raise DomainError(
                errors.BOOKING_STATE, f"预约 {booking.id} 已核销，不能记为爽约"
            )
        booking.status = BookingStatus.NO_SHOW
        return {"booking_id": booking.id}

    def _do_record_consent(self, p: dict, at: str, event_id: str) -> dict:
        member = self._member(p["member_id"])
        from .models import Consent
        try:
            consent = Consent(p["consent"])
        except ValueError:
            raise DomainError(errors.NOT_FOUND, f"未知确认事项: {p['consent']}") from None
        added = consent not in self._consents[member.id]
        self._consents[member.id].add(consent)
        return {"member_id": member.id, "consent": consent.value, "newly_added": added}

    def _do_change_venue(self, p: dict, at: str, event_id: str) -> dict:
        session = self._session(p["session_id"])
        if session.id in self._session_cancelled:
            raise DomainError(errors.SESSION_CLOSED, f"场次 {session.id} 已取消，不能换场")
        new_venue = self._venues.get(p["venue_id"])
        if new_venue is None:
            raise DomainError(errors.NOT_FOUND, f"场地不存在: {p['venue_id']}")
        old_venue_id = self._session_venue[session.id]
        if new_venue.id == old_venue_id:
            return {"session_id": session.id, "venue_id": new_venue.id, "deduplicated": True}
        booked = self._active_count(session.id)
        if new_venue.capacity < booked:
            raise DomainError(
                errors.VENUE_CAPACITY,
                f"场地 {new_venue.name} 容量 {new_venue.capacity} 小于已约人数 {booked}，不能换场",
            )
        self._session_venue[session.id] = new_venue.id
        return {
            "session_id": session.id,
            "from_venue_id": old_venue_id,
            "venue_id": new_venue.id,
            "reason": p.get("reason", ""),
            "effective_capacity": self._effective_capacity(session),
        }

    def _do_device_status(self, p: dict, at: str, event_id: str) -> dict:
        device = self._devices.get(p["device_id"])
        if device is None:
            raise DomainError(errors.NOT_FOUND, f"设备不存在: {p['device_id']}")
        status = DeviceStatus(p["status"])
        self._device_status[device.id] = status
        self._device_note[device.id] = p.get("note", "")
        affected = [
            sid for sid, ids in self._session_devices.items()
            if device.id in ids and sid not in self._session_cancelled
        ]
        return {"device_id": device.id, "status": status.value,
                "affected_sessions": sorted(affected)}

    def _do_swap_device(self, p: dict, at: str, event_id: str) -> dict:
        session = self._session(p["session_id"])
        if session.id in self._session_cancelled:
            raise DomainError(errors.SESSION_CLOSED, f"场次 {session.id} 已取消")
        ids = self._session_devices[session.id]
        if p["old_device_id"] not in ids:
            raise DomainError(errors.NOT_FOUND, f"场次 {session.id} 未配置设备 {p['old_device_id']}")
        new_device = self._devices.get(p["new_device_id"])
        old_device = self._devices[p["old_device_id"]]
        if new_device is None:
            raise DomainError(errors.NOT_FOUND, f"设备不存在: {p['new_device_id']}")
        if new_device.type != old_device.type:
            raise DomainError(errors.BOOKING_STATE, "替换设备类型不匹配")
        if self._device_status[new_device.id] != DeviceStatus.READY:
            raise DomainError(errors.DEVICE_UNAVAILABLE, f"备用设备 {new_device.name} 不可用")
        ids[ids.index(old_device.id)] = new_device.id
        return {"session_id": session.id, "device_ids": list(ids)}

    def _do_fail_session(self, p: dict, at: str, event_id: str) -> dict:
        session = self._session(p["session_id"])
        if session.id in self._session_cancelled:
            return {"session_id": session.id, "deduplicated": True}
        reason = p.get("reason", errors.SESSION_CLOSED)
        note = p.get("note", "")
        salvage = bool(p.get("salvage_materials"))
        self._session_cancelled[session.id] = reason
        failed = 0
        for booking in self._bookings.values():
            if booking.session_id != session.id:
                continue
            if booking.status == BookingStatus.RESERVED:
                booking.status = BookingStatus.FAILED
                booking.failure_reason = reason
                booking.failure_note = note
                failed += 1
            elif booking.status == BookingStatus.CHECKED_IN:
                # 已核销但未能完成服务：物料已发出，按损耗（可挽救时按归还）入账
                if salvage:
                    self._return_materials(booking, at, f"场次取消归还：{note}")
                else:
                    self._waste_materials(booking, at, f"场次取消损耗：{note}")
                booking.status = BookingStatus.FAILED
                booking.failure_reason = reason
                booking.failure_note = note
                failed += 1
        return {"session_id": session.id, "reason": reason, "failed_bookings": failed}

    # ------------------------------------------------------------------
    # 规则校验
    # ------------------------------------------------------------------

    def _check_bookable(self, session, member) -> None:
        self._check_open(session)
        self._check_age(session, member)
        self._check_capacity(session, 1)

    def _check_open(self, session) -> None:
        if session.id in self._session_cancelled:
            raise DomainError(
                errors.SESSION_CLOSED,
                f"场次 {session.id} 已取消（{self._session_cancelled[session.id]}）",
            )

    def _check_age(self, session, member) -> None:
        activity = self._activities[session.activity_id]
        if member.age < activity.min_age or (
            activity.max_age is not None and member.age > activity.max_age
        ):
            raise DomainError(
                errors.AGE_RESTRICTED,
                f"{member.name}（{member.age} 岁）不符合「{activity.name}」年龄限制"
                f"（{activity.min_age} 岁起）",
            )

    def _check_consents(self, session, member) -> None:
        activity = self._activities[session.activity_id]
        missing = [c.value for c in activity.required_consents
                   if c not in self._consents[member.id]]
        if missing:
            raise DomainError(
                errors.CONSENT_MISSING,
                f"{member.name} 尚未完成: {', '.join(missing)}",
            )

    def _check_devices(self, session) -> None:
        for device_id in self._session_devices[session.id]:
            status = self._device_status[device_id]
            if status != DeviceStatus.READY:
                device = self._devices[device_id]
                raise DomainError(
                    errors.DEVICE_UNAVAILABLE,
                    f"设备 {device.name} 当前不可用（{status.value}）",
                )

    def _check_capacity(self, session, needed: int) -> None:
        remaining = self._effective_capacity(session) - self._active_count(session.id)
        if remaining < needed:
            raise DomainError(
                errors.CAPACITY_FULL,
                f"场次 {session.id} 剩余名额 {remaining}，不足 {needed} 人",
            )

    def _effective_capacity(self, session) -> int:
        venue = self._venues[self._session_venue[session.id]]
        return min(session.capacity, venue.capacity)

    def _active_count(self, session_id: str) -> int:
        return sum(
            1 for b in self._bookings.values()
            if b.session_id == session_id and b.status in ACTIVE_BOOKING_STATUSES
        )

    def _active_booking(self, session_id: str, member_id: str) -> Booking | None:
        booking_id = self._booking_by_key.get((session_id, member_id))
        if booking_id is None:
            return None
        booking = self._bookings[booking_id]
        if booking.status not in ACTIVE_BOOKING_STATUSES:
            return None
        return booking

    def _register(self, booking: Booking) -> None:
        self._bookings[booking.id] = booking
        self._booking_by_key[(booking.session_id, booking.member_id)] = booking.id

    # ------------------------------------------------------------------
    # 物料守恒
    # ------------------------------------------------------------------

    def _issue_materials(self, session, booking: Booking, at: str) -> None:
        activity = self._activities[session.activity_id]
        for batch_id, qty in activity.materials.items():
            if self._remaining[batch_id] < qty:
                batch = self._batches[batch_id]
                raise DomainError(
                    errors.MATERIAL_SHORTAGE,
                    f"{batch.name} 剩余 {self._remaining[batch_id]}{batch.unit}，"
                    f"不足本次所需 {qty}{batch.unit}",
                )
        for batch_id, qty in activity.materials.items():
            self._remaining[batch_id] -= qty
            self._movements.append(
                MaterialMovement(at=at, batch_id=batch_id, kind=MovementKind.ISSUE,
                                 quantity=qty, ref=booking.id)
            )

    def _return_materials(self, booking: Booking, at: str, note: str) -> None:
        activity = self._activities[self._sessions[booking.session_id].activity_id]
        for batch_id, qty in activity.materials.items():
            self._remaining[batch_id] += qty
            self._movements.append(
                MaterialMovement(at=at, batch_id=batch_id, kind=MovementKind.RETURN,
                                 quantity=qty, ref=booking.id, note=note)
            )

    def _waste_materials(self, booking: Booking, at: str, note: str) -> None:
        activity = self._activities[self._sessions[booking.session_id].activity_id]
        for batch_id, qty in activity.materials.items():
            self._movements.append(
                MaterialMovement(at=at, batch_id=batch_id, kind=MovementKind.WASTE,
                                 quantity=qty, ref=booking.id, note=note)
            )

    # ------------------------------------------------------------------
    # 印章
    # ------------------------------------------------------------------

    def _evaluate_stamps(self, member_id: str, at: str) -> list[Stamp]:
        completed_categories = [
            self._activities[self._sessions[b.session_id].activity_id].category
            for b in self._bookings.values()
            if b.member_id == member_id and b.status == BookingStatus.COMPLETED
        ]
        new_stamps = []
        for rule in self._stamp_rules.values():
            if (member_id, rule.id) in self._stamp_keys:
                continue
            if completed_categories.count(rule.category) >= rule.required_completions:
                stamp = Stamp(member_id=member_id, rule_id=rule.id, name=rule.name, at=at)
                self._stamps.append(stamp)
                self._stamp_keys.add((member_id, rule.id))
                new_stamps.append(stamp)
        return new_stamps

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def _session(self, session_id: str):
        session = self._sessions.get(session_id)
        if session is None:
            raise DomainError(errors.NOT_FOUND, f"场次不存在: {session_id}")
        return session

    def _member(self, member_id: str):
        member = self._members.get(member_id)
        if member is None:
            raise DomainError(errors.NOT_FOUND, f"成员不存在: {member_id}")
        return member

    def _booking(self, booking_id: str) -> Booking:
        booking = self._bookings.get(booking_id)
        if booking is None:
            raise DomainError(errors.NOT_FOUND, f"预约不存在: {booking_id}")
        return booking

    def _resolve_booking(self, p: dict) -> Booking:
        if p.get("booking_id"):
            return self._booking(p["booking_id"])
        booking = self._active_booking(p["session_id"], p["member_id"])
        if booking is None:
            raise DomainError(errors.NOT_FOUND, "找不到对应的有效预约")
        return booking

    def _consent_warnings(self, session, member) -> list[str]:
        activity = self._activities[session.activity_id]
        return [c.value for c in activity.required_consents
                if c not in self._consents[member.id]]

    # ------------------------------------------------------------------
    # 只读视图（供看板与报告使用）
    # ------------------------------------------------------------------

    @property
    def bookings(self) -> list[Booking]:
        return list(self._bookings.values())

    @property
    def movements(self) -> list[MaterialMovement]:
        return list(self._movements)

    @property
    def event_log(self) -> list[EventRecord]:
        return list(self._log)

    @property
    def stamps(self) -> list[Stamp]:
        return list(self._stamps)

    @property
    def reading_records(self) -> list[ReadingRecord]:
        return list(self._reading)

    @property
    def science_records(self) -> list[ScienceRecord]:
        return list(self._science)

    def batch_remaining(self, batch_id: str) -> int:
        return self._remaining[batch_id]

    def device_status(self, device_id: str) -> DeviceStatus:
        return self._device_status[device_id]

    def session_view(self, session_id: str) -> dict:
        """单场次的实时视图：名额、物料、设备、待补同意书的成员。"""
        session = self._session(session_id)
        activity = self._activities[session.activity_id]
        capacity = self._effective_capacity(session)
        booked = self._active_count(session.id)
        missing_consents = []
        for b in self._bookings.values():
            if b.session_id == session.id and b.status in ACTIVE_BOOKING_STATUSES:
                member = self._members[b.member_id]
                missing = self._consent_warnings(session, member)
                if missing:
                    missing_consents.append(
                        {"member_id": member.id, "name": member.name, "missing": missing}
                    )
        return {
            "session_id": session.id,
            "activity": activity.name,
            "venue_id": self._session_venue[session.id],
            "start": session.start,
            "end": session.end,
            "cancelled": session.id in self._session_cancelled,
            "capacity": capacity,
            "booked": booked,
            "remaining_capacity": capacity - booked,
            "materials": [
                {
                    "batch_id": bid,
                    "name": self._batches[bid].name,
                    "unit": self._batches[bid].unit,
                    "per_person": qty,
                    "remaining": self._remaining[bid],
                }
                for bid, qty in activity.materials.items()
            ],
            "devices": [
                {
                    "device_id": did,
                    "name": self._devices[did].name,
                    "status": self._device_status[did].value,
                    "note": self._device_note[did],
                }
                for did in self._session_devices[session.id]
            ],
            "members_missing_consents": missing_consents,
        }

    def staff_overview(self, at: str | None = None) -> dict:
        """工作人员看板：各场次名额与物料、停用设备、待补同意书的成员。"""
        sessions = sorted(self._sessions.values(), key=lambda s: s.start)
        if at is not None:
            sessions = [s for s in sessions if s.end >= at]
        return {
            "plan_id": self.plan.plan_id,
            "sessions": [self.session_view(s.id) for s in sessions],
            "unavailable_devices": [
                {
                    "device_id": d.id,
                    "name": d.name,
                    "status": self._device_status[d.id].value,
                    "note": self._device_note[d.id],
                }
                for d in self.plan.devices
                if self._device_status[d.id] != DeviceStatus.READY
            ],
        }
