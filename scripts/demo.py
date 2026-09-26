"""端到端演示：纸章猜测 -> 后台实时保障的一个市集下午。

运行：python -m scripts.demo
全程仅使用 fixtures/market.json 中的虚构样例数据。
"""

from __future__ import annotations

import json

from src.culture_market.backend import Backend
from src.culture_market.catalog import load_catalog
from src.culture_market.reports import (
    batch_reconciliation, capacity_reconciliation, family_journey,
    reconciliation_pack, staff_dashboard, unserved_report,
)

CATALOG = "fixtures/market.json"


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_board(backend) -> None:
    board = staff_dashboard(backend)
    print("停用设备：")
    for d in board["devices_down"]:
        print(f"  - {d['id']} {d['label']}（{d['activity']}）原因：{d['reason']}")
    print("尚未完成的同意项：")
    for g in board["consent_gaps"]:
        print(f"  - {g['family']}·{g['name']} 缺：{'、'.join(g['missing'])}"
              f"  影响：{'；'.join(g['blocks'])}")
    print("重点场次名额/物料（节选）：")
    focus = {"VR-S2", "MOON-S1", "KIT-1", "POET-S1"}
    for s in board["sessions"]:
        if s["session_id"] not in focus:
            continue
        mat = "，".join(
            f"{m['material']} 在场{m['available']:g}{m['unit']}"
            f"（已发{m['issued']:g}/废{m['wasted']:g}）"
            for m in s["materials"]
        ) or "无物料"
        print(f"  {s['session_id']} {s['activity']} {s['window']} "
              f"{s['venue']}｜名额 已服务{s['served']}+已约{s['booked']}"
              f"/有效容量{s['capacity']}，剩 {s['remaining']}｜{mat}")


def outcome(r: dict) -> str:
    if r["ok"]:
        stamp = f"，盖章「{r['stamp']}」" if r.get("stamp") else ""
        return "成功" + stamp
    return f"被拦：{r['reason_label']}（{r.get('detail', '')}）"


def main() -> None:
    catalog = load_catalog(CATALOG)
    backend = Backend(catalog)
    name = lambda mid: catalog.members[mid].name  # noqa: E731

    hr("① 开场看板：名额、物料、停用设备（VR-04 开场黑屏）、同意项缺口")
    show_board(backend)

    hr("② 家庭结伴预约：李一诺与弟弟李一笑结伴预约月饼 S1（整组同进同出）")
    r = backend.book("req-book-moon", "MOON-S1", ["F01-2", "F01-3"],
                     "13:05", group_id="G-F01-A")
    print("结伴预约：", "成功" if r["ok"] else "失败", r.get("booking_ids", ""))
    print("  -> 李一笑（5 岁）尚未完成过敏提示，扫码时才会被拦截")

    hr("③ 13:10 开场扫码：缺过敏提示被拦，不扣任何物料与名额")
    r1 = backend.serve("req-serve-yinuo-moon", "MOON-S1", "F01-2", "13:10")
    r2 = backend.serve("req-serve-yixiao-moon", "MOON-S1", "F01-3", "13:10")
    print(f"李一诺：{outcome(r1)}")
    print(f"李一笑：{outcome(r2)}")

    hr("④ 网络抖动：李一诺的同一扫码请求被终端重复提交三次")
    for i in range(3):
        rep = backend.serve("req-serve-yinuo-moon", "MOON-S1", "F01-2", "13:10")
        print(f"  第 {i + 1} 次提交：{'幂等返回首次结果' if rep.get('replayed') else '首次执行'}")
    moon = backend.ledger.session_usage("MOON-S1")
    print(f"月饼 S1 饼皮仍只发出 {moon['饼皮']['issued']:g} 份（物料守恒）")

    hr("⑤ 家长补全李一笑的过敏提示后重新扫码放行")
    backend.update_consent("req-consent-yixiao", "F01-3",
                           "allergy_notice", True, "13:18")
    r3 = backend.serve("req-serve-yixiao-moon-2", "MOON-S1", "F01-3", "13:20")
    print(f"李一笑：{outcome(r3)}")

    hr("⑥ VR-S2 进行中 VR-03 故障停用：有效容量 5 -> 4，超额扫码被拦")
    print("故障前 VR-S2 有效容量：", backend.session_counts("VR-S2")["capacity"])
    backend.set_device_status("req-vr03-down", "VR-03", "down",
                              "13:55", "画面卡死，贴停用条")
    print("故障后 VR-S2 有效容量：", backend.session_counts("VR-S2")["capacity"])
    for mid, at in [("F02-2", "13:56"), ("F03-2", "13:57"), ("F01-2", "13:58"),
                    ("F03-1", "13:59"), ("F02-3", "14:00"), ("F01-1", "14:01")]:
        r = backend.serve(f"req-vr2-{mid}", "VR-S2", mid, at)
        print(f"  {name(mid)}：{outcome(r)}")

    hr("⑦ 安全确认未完成 -> 扫码被拦；补确认后放行；临时换场")
    r = backend.book("req-book-wzx-robot", "ROBOT-S3", ["F02-3"], "14:35")
    print("王梓轩预约机器狗 S3：", "成功（同意项在扫码时强制核验）"
          if r["ok"] else outcome(r))
    r = backend.serve("req-serve-wzx-robot", "ROBOT-S3", "F02-3", "14:45")
    print("首次扫码：", outcome(r))
    backend.update_consent("req-consent-wzx", "F02-3",
                           "safety_confirmation", True, "14:50")
    r = backend.serve("req-serve-wzx-robot-2", "ROBOT-S3", "F02-3", "14:52")
    print("补安全确认后：", outcome(r))
    backend.book("req-book-wzh-vr", "VR-S3", ["F02-2"], "14:42")
    vrs3 = next(b.id for b in backend.bookings.values()
                if b.member_id == "F02-2" and b.session_id == "VR-S3"
                and b.status == "booked")
    tr = backend.transfer("req-transfer-wzh", vrs3, "VR-S4", "15:00")
    print("王梓涵临时换场 VR-S3 -> VR-S4：",
          "成功（原名额已释放）" if tr["ok"] else outcome(tr))

    hr("⑧ 断网补传：离线队列按序重放，request_id 去重，不突破容量与物料")
    queued = [
        {"op": "book", "request_id": "req-off-kit-book", "session_id": "KIT-1",
         "member_ids": ["F01-1"], "at": "14:01"},
        {"op": "serve", "request_id": "req-off-kit-serve", "session_id": "KIT-1",
         "member_id": "F01-1", "at": "14:02"},
        # 同一请求被两台离线终端各入队一次
        {"op": "serve", "request_id": "req-off-kit-serve", "session_id": "KIT-1",
         "member_id": "F01-1", "at": "14:02"},
        {"op": "serve", "request_id": "req-off-lamp", "session_id": "LAMP-S2",
         "member_id": "F01-1", "at": "14:25"},
    ]
    for res in backend.sync(queued):
        tag = "（幂等重放）" if res.get("replayed") else ""
        print("  补传：", ("成功" if res["ok"] else outcome(res)) + tag)
    kit = backend.ledger.session_usage("KIT-1")["科普资源包"]
    print(f"科普包只发出 {kit['issued']:g} 份（重复入队未重复发放）")

    hr("⑨ 个人阅读与科普记录独立保存（家庭结伴，记录不结伴）")
    backend.serve("req-poet-yinuo", "POET-S3", "F01-2", "14:50",
                  reading={"title": "水调歌头·明月几时有", "minutes": 12})
    backend.complete_science_task("req-sci-yinuo", "F01-2", "15:10",
                                  "完成月相观测记录页")
    j = family_journey(backend, "F01")
    for m in j["members"]:
        print(f"  {m['name']}：印章 {[s['code'] for s in m['stamps']]}，"
              f"阅读 {len(m['reading_records'])} 条，"
              f"科普 {len(m['science_records'])} 条")

    hr("⑩ 物料报废留痕（拓墨污染 10 毫升，逐批摊分入审计链）")
    backend.record_waste("req-waste-ink", "RUB-S1", "拓墨", 10, "14:40",
                         "棉扑蘸墨过多污染")
    rub = backend.ledger.session_usage("RUB-S1")["拓墨"]
    print(f"RUB-S1 拓墨：分 {rub['allocated']:g} 发 {rub['issued']:g} "
          f"废 {rub['wasted']:g} 在场 {rub['available']:g} 毫升")

    hr("⑪ 闭场：未到预约记 no_show，未用物料退回中央池")
    backend.close("17:30")
    pack = reconciliation_pack(backend)
    cap = capacity_reconciliation(backend)
    print("审计链完整：", pack["audit_chain_ok"],
          f"（{pack['audit_events']} 条事件）")
    print("全部场次已服务人数不超有效容量：", cap["all_within_limit"])
    print("全部批次物料守恒：", pack["materials"]["conserved"])
    t = pack["materials"]["totals"]
    print(f"物料总账：入库 {t['received']:g} = 发放 {t['issued']:g} + "
          f"报废 {t['wasted']:g} + 结余 {t['remaining']:g}")

    hr("⑫ 未服务成功原因汇总（主办方逐案说明）")
    ur = unserved_report(backend)
    print("合计", ur["total"], "条：", ur["summary"])

    hr("⑬ 逐批资源去向（节选三个批次）")
    rec = batch_reconciliation(backend)
    for row in rec["batches"]:
        if row["batch_id"] not in {"B-INK-01", "B-DOUGH-01", "B-KIT-01"}:
            continue
        dest = "，".join(
            f"{sid} 发 {d['issued']:g} 废 {d['wasted']:g}"
            for sid, d in row["sessions"].items()
        ) or "未动用"
        print(f"  {row['batch_id']} {row['material']}：入库 {row['received']:g}"
              f"{row['unit']} -> {dest}；退回中央 {row['returned_to_pool']:g}，"
              f"在场 {row['at_sessions']:g}")

    hr("⑭ 家庭还原：李一家真实参与过程")
    print(json.dumps(family_journey(backend, "F01"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
