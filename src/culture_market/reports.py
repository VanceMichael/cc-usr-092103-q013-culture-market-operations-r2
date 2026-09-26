"""工作人员看板、闭场对账与家庭参与过程还原。"""

from __future__ import annotations

from collections import Counter, defaultdict

from .catalog import CONSENT_LABELS
from .backend import REASON_LABELS, STATUS_LABELS


def _person_name(catalog, person_id: str | None) -> str:
    if not person_id:
        return "—"
    return f"{catalog.staff[person_id]['name']}（{catalog.staff[person_id]['role']}）"


def staff_dashboard(backend) -> dict:
    """开场后工作人员一眼看清：下一场名额、物料、停用设备、未完成同意项。"""
    catalog = backend.catalog
    sessions_view = []
    for sid in sorted(catalog.sessions, key=lambda s: (catalog.sessions[s].start, s)):
        session = catalog.sessions[sid]
        activity = catalog.activities[session.activity_id]
        counts = backend.session_counts(sid)
        usage = backend.ledger.session_usage(sid)
        materials = []
        for spec in activity.materials:
            u = usage.get(spec.material, {"allocated": 0, "issued": 0,
                                          "wasted": 0, "returned": 0, "available": 0})
            materials.append({
                "material": spec.material, "unit": spec.unit,
                "per_person": spec.qty,
                "allocated": _num(u["allocated"]), "issued": _num(u["issued"]),
                "wasted": _num(u["wasted"]), "returned": _num(u["returned"]),
                "available": _num(u["available"]),
                "served_capacity": int(u["allocated"] / spec.qty) if spec.qty else None,
            })
        devices = catalog.session_devices(activity.id)
        device_view = [{
            "id": d["id"], "label": d["label"],
            "slots": d["slots"],
            "status": backend.devices[d["id"]].status,
            "reason": backend.devices[d["id"]].reason,
        } for d in devices]
        down_slots = sum(d["slots"] for d in device_view if d["status"] == "down")
        sessions_view.append({
            "session_id": sid,
            "activity_id": activity.id,
            "activity": activity.name,
            "stamp": activity.stamp_code,
            "window": f"{session.start}-{session.end}",
            "venue": catalog.venues[session.venue_id]["name"],
            "mentor": _person_name(catalog, session.mentor_id),
            "inheritor": _person_name(catalog, session.inheritor_id),
            "capacity": counts["capacity"],
            "declared_capacity": activity.capacity,
            "booked": counts["booked"],
            "served": counts["served"],
            "remaining": counts["remaining"],
            "materials": materials,
            "devices": device_view,
            "down_slots": down_slots,
        })

    devices_down = [{
        "id": did, "label": catalog.devices[did]["label"],
        "activity": catalog.activities[catalog.devices[did]["activity_id"]].name,
        "reason": state.reason, "since": state.since,
    } for did, state in backend.devices.items() if state.status == "down"]

    consent_gaps = []
    for fid, fam in catalog.families.items():
        for p in fam["passports"]:
            member = catalog.members[p["member_id"]]
            missing = [c for c, ok in member.consents.items() if not ok]
            if missing:
                blocking = []
                for aid, act in catalog.activities.items():
                    need = [CONSENT_LABELS[c] for c in act.required_consents
                            if c in missing]
                    if need:
                        blocking.append(f"{act.name}：{'、'.join(need)}")
                consent_gaps.append({
                    "family_id": fid, "family": fam["name"],
                    "member_id": p["member_id"], "name": p["name"],
                    "relation": p["relation"],
                    "missing": [CONSENT_LABELS[c] for c in missing],
                    "blocks": blocking,
                })

    return {
        "event": catalog.event,
        "closed": backend.closed,
        "sessions": sessions_view,
        "devices_down": devices_down,
        "consent_gaps": consent_gaps,
    }


def unserved_report(backend) -> dict:
    """未服务成功台账：按原因归集，逐条说明发生在哪一场、拦了谁、为什么。"""
    by_reason = defaultdict(list)
    for entry in backend.unserved:
        member = backend.catalog.members.get(entry["member_id"])
        row = dict(entry)
        row["member_name"] = member.name if member else "（整组/不适用）"
        session = backend.catalog.sessions.get(entry["session_id"])
        if session:
            row["activity"] = backend.catalog.activities[session.activity_id].name
            row["window"] = f"{session.start}-{session.end}"
        by_reason[entry["reason"]].append(row)
    summary = Counter(e["reason"] for e in backend.unserved)
    return {
        "total": len(backend.unserved),
        "summary": {REASON_LABELS[k]: v for k, v in sorted(summary.items())},
        "by_reason": {REASON_LABELS.get(k, k): rows for k, rows in by_reason.items()},
    }


def batch_reconciliation(backend) -> dict:
    """闭场后逐批说明资源去向，并校验全部守恒恒等式。"""
    rows = []
    for bid in sorted(backend.catalog.batches):
        dest = backend.ledger.batch_destination(bid)
        batch_meta = backend.catalog.batches[bid]
        rows.append({
            "batch_id": bid,
            "material": dest["material"],
            "unit": dest["unit"],
            "supplier": batch_meta.get("supplier", ""),
            "note": batch_meta.get("note", ""),
            "received": _num(dest["received"]),
            "issued": _num(dest["issued"]),
            "wasted": _num(dest["wasted"]),
            "remaining": _num(dest["remaining"]),
            "at_sessions": _num(dest["at_sessions"]),
            "returned_to_pool": _num(dest["returned_to_pool"]),
            "sessions": dest["sessions"],
        })
    totals = defaultdict(float)
    for row in rows:
        for key in ("received", "issued", "wasted", "remaining"):
            totals[key] += row[key]
    return {
        "conserved": backend.ledger.all_conserved(),
        "totals": {k: _num(v) for k, v in totals.items()},
        "batches": rows,
    }


def capacity_reconciliation(backend) -> dict:
    """场次名额对账：有效容量 = 已服务 + 已预约 + 释放空间，且已服务不超限。"""
    rows = []
    for sid, session in backend.catalog.sessions.items():
        counts = backend.session_counts(sid)
        served = counts["served"]
        booked = counts["booked"] if not backend.closed else 0
        cap = counts["capacity"]
        activity = backend.catalog.activities[session.activity_id]
        rows.append({
            "session_id": sid,
            "activity": activity.name,
            "capacity": cap,
            "served": served,
            "booked": 0 if backend.closed else booked,
            "unused": cap - served - booked,
            "within_limit": served <= cap,
        })
    return {"all_within_limit": all(r["within_limit"] for r in rows), "sessions": rows}


def family_journey(backend, family_id: str) -> dict:
    """还原一个家庭的真实参与过程：逐成员的预约、印章、阅读/科普独立记录。"""
    catalog = backend.catalog
    fam = catalog.families[family_id]
    members_view = []
    for p in fam["passports"]:
        mid = p["member_id"]
        member = catalog.members[mid]
        bookings_view = []
        for b in sorted(
            [b for b in backend.bookings.values() if b.member_id == mid],
            key=lambda b: b.id,
        ):
            session = catalog.sessions[b.session_id]
            bookings_view.append({
                "booking_id": b.id,
                "activity": catalog.activities[session.activity_id].name,
                "session_id": b.session_id,
                "window": f"{session.start}-{session.end}",
                "venue": catalog.venues[session.venue_id]["name"],
                "status": b.status,
                "status_label": STATUS_LABELS.get(b.status, b.status),
                "unfulfilled_reason": (
                    REASON_LABELS.get(b.close_reason, b.close_reason)
                    if b.status == "unfulfilled" else None
                ),
                "group_id": b.group_id,
                "booked_at": b.booked_at,
                "served_at": b.served_at,
            })
        stamps = []
        for st in backend.stamps.get(mid, []):
            stamps.append({
                "code": st["code"],
                "activity": catalog.activities[st["activity_id"]].name,
                "rule": catalog.activities[st["activity_id"]].stamp_rule,
                "session_id": st["session_id"], "at": st["at"],
            })
        members_view.append({
            "member_id": mid, "name": p["name"], "relation": p["relation"],
            "age": catalog.age_of(member),
            "consents": {CONSENT_LABELS[k]: member.consents.get(k, False)
                         for k in CONSENT_LABELS},
            "bookings": bookings_view,
            "stamps": stamps,
            "reading_records": list(backend.readings.get(mid, [])),
            "science_records": list(backend.science.get(mid, [])),
        })
    return {
        "family_id": family_id,
        "family": fam["name"],
        "science_kit_issued": family_id in backend.kit_issued,
        "members": members_view,
    }


def reconciliation_pack(backend) -> dict:
    """主办方闭场总包：资源、名额、未服务原因、审计链完整性一次给齐。"""
    return {
        "event": backend.catalog.event,
        "closed": backend.closed,
        "audit_chain_ok": backend.verify_audit_chain(),
        "audit_events": len(backend.audit),
        "capacity": capacity_reconciliation(backend),
        "materials": batch_reconciliation(backend),
        "unserved": unserved_report(backend),
    }


def _num(x: float):
    return int(x) if abs(x - int(x)) < 1e-9 else round(x, 4)
