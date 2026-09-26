"""赛后报告与参与还原。

* :func:`resource_report` —— 逐批说明物料去向，并校验物料守恒；
* :func:`service_report` —— 逐场次说明服务结果与未服务成功的原因；
* :func:`family_journey` —— 家庭视角的真实参与过程还原；
* :func:`assert_conservation` —— 人数与物料守恒的总校验，
  任何断网补传、重复扫码、换场、设备故障之后都必须仍然成立。
"""

from __future__ import annotations

from .backend import MarketBackend
from .models import BookingStatus, MovementKind


def assert_conservation(backend: MarketBackend) -> None:
    """校验两条守恒式，违反时抛出 AssertionError。

    * 物料库存：每批 ``初始 = 剩余 + 发放 - 归还``
      （损耗只是已发放物料的去向重分类，不再改变库存）
    * 物料去向：每批 ``发放 = 归还 + 损耗 + 在参与者手中``
    * 人数：每场次有效预约数不超过有效容量（场次与场地取小）
    """
    problems = []
    for batch in backend.plan.batches:
        issued = returned = wasted = 0
        for mv in backend.movements:
            if mv.batch_id != batch.id:
                continue
            if mv.kind == MovementKind.ISSUE:
                issued += mv.quantity
            elif mv.kind == MovementKind.RETURN:
                returned += mv.quantity
            elif mv.kind == MovementKind.WASTE:
                wasted += mv.quantity
        remaining = backend.batch_remaining(batch.id)
        if batch.initial_quantity != remaining + issued - returned:
            problems.append(
                f"批次 {batch.id} 库存不守恒: 初始 {batch.initial_quantity} != "
                f"剩余 {remaining} + 发放 {issued} - 归还 {returned}"
            )
        if issued < returned + wasted:
            problems.append(
                f"批次 {batch.id} 去向不明: 发放 {issued} < 归还 {returned} + 损耗 {wasted}"
            )
        if remaining < 0:
            problems.append(f"批次 {batch.id} 剩余数量为负: {remaining}")
    for session in backend.plan.sessions:
        view = backend.session_view(session.id)
        if view["booked"] > view["capacity"]:
            problems.append(
                f"场次 {session.id} 超容量: 有效预约 {view['booked']} > 容量 {view['capacity']}"
            )
    if problems:
        raise AssertionError("守恒校验失败:\n" + "\n".join(problems))


def resource_report(backend: MarketBackend) -> dict:
    """逐批说明资源去向：初始、发放、归还、损耗、剩余与全部流水。"""
    batches = []
    for batch in backend.plan.batches:
        movements = [mv for mv in backend.movements if mv.batch_id == batch.id]
        issued = sum(m.quantity for m in movements if m.kind == MovementKind.ISSUE)
        returned = sum(m.quantity for m in movements if m.kind == MovementKind.RETURN)
        wasted = sum(m.quantity for m in movements if m.kind == MovementKind.WASTE)
        remaining = backend.batch_remaining(batch.id)
        in_use = issued - returned - wasted  # 仍在参与者手中
        batches.append(
            {
                "batch_id": batch.id,
                "name": batch.name,
                "unit": batch.unit,
                "initial": batch.initial_quantity,
                "issued": issued,
                "returned": returned,
                "wasted": wasted,
                "in_use": in_use,
                "remaining": remaining,
                "conservation_ok": batch.initial_quantity == remaining + issued - returned,
                "movements": [
                    {
                        "at": m.at,
                        "kind": m.kind.value,
                        "quantity": m.quantity,
                        "ref": m.ref,
                        "note": m.note,
                    }
                    for m in movements
                ],
            }
        )
    return {
        "plan_id": backend.plan.plan_id,
        "batches": batches,
        "conservation_ok": all(b["conservation_ok"] for b in batches),
    }


def service_report(backend: MarketBackend) -> dict:
    """逐场次说明服务结果，并汇总未服务成功的明细与原因。"""
    sessions = []
    unserved = []
    members = {m.id: m for m in backend.plan.members}
    for session in backend.plan.sessions:
        view = backend.session_view(session.id)
        bookings = [b for b in backend.bookings if b.session_id == session.id]
        served = [b for b in bookings if b.status == BookingStatus.COMPLETED]
        failed = [b for b in bookings if b.status == BookingStatus.FAILED]
        no_show = [b for b in bookings if b.status == BookingStatus.NO_SHOW]
        for b in failed + no_show:
            member = members[b.member_id]
            unserved.append(
                {
                    "session_id": session.id,
                    "member_id": b.member_id,
                    "member_name": member.name,
                    "family_id": member.family_id,
                    "status": b.status.value,
                    "reason": b.failure_reason or b.status.value,
                    "note": b.failure_note,
                }
            )
        sessions.append(
            {
                "session_id": session.id,
                "activity": view["activity"],
                "cancelled": view["cancelled"],
                "capacity": view["capacity"],
                "served": len(served),
                "failed": len(failed),
                "no_show": len(no_show),
                "cancelled_bookings": sum(
                    1 for b in bookings if b.status == BookingStatus.CANCELLED
                ),
            }
        )
    return {
        "plan_id": backend.plan.plan_id,
        "sessions": sessions,
        "unserved": unserved,
    }


def family_journey(backend: MarketBackend, family_id: str) -> dict:
    """还原一个家庭的真实参与过程（含被拒绝的尝试及原因）。

    时间线来自事件日志与派生记录（印章、阅读、科普），按时间排序；
    结伴不影响归属——只含本家庭成员的条目，个人阅读与科普记录
    始终按成员独立列出。
    """
    member_ids = {m.id for m in backend.plan.members if m.family_id == family_id}
    if not member_ids:
        raise ValueError(f"家庭不存在或没有成员: {family_id}")
    group_ids = {
        g.id for g in backend.plan.groups if family_id in g.family_ids
    }
    booking_by_id = {b.id: b for b in backend.bookings}

    def concerns(payload: dict) -> bool:
        if payload.get("member_id") in member_ids:
            return True
        if payload.get("group_id") in group_ids:
            return True
        booking = booking_by_id.get(payload.get("booking_id") or "")
        return booking is not None and booking.member_id in member_ids

    timeline = []
    for record in backend.event_log:
        if concerns(record.payload):
            timeline.append(
                {
                    "at": record.at,
                    "kind": record.kind,
                    "ok": record.result.get("ok", False),
                    "detail": record.result,
                }
            )
        elif record.kind == "fail_session":
            # 场次取消影响本家庭已约成员时，也应出现在家庭时间线里
            session_id = record.payload.get("session_id")
            if any(
                b.session_id == session_id and b.member_id in member_ids
                for b in backend.bookings
            ):
                timeline.append(
                    {
                        "at": record.at,
                        "kind": record.kind,
                        "ok": record.result.get("ok", False),
                        "detail": record.result,
                    }
                )
    for stamp in backend.stamps:
        if stamp.member_id in member_ids:
            timeline.append(
                {"at": stamp.at, "kind": "stamp", "ok": True,
                 "detail": {"member_id": stamp.member_id, "name": stamp.name}}
            )
    timeline.sort(key=lambda e: e["at"])

    return {
        "family_id": family_id,
        "timeline": timeline,
        "reading_records": [r for r in backend.reading_records if r.member_id in member_ids],
        "science_records": [r for r in backend.science_records if r.member_id in member_ids],
        "stamps": [s for s in backend.stamps if s.member_id in member_ids],
    }
