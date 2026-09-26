"""批次物料台账：登记、发放、退料、报废与逐批去向。

守恒恒等式（对每个批次始终成立）：

    入库量 = 已发放 + 报废 + 结余
    结余   = 在场未用 + 已退回中央 + 从未分配

场次维度另有：

    分到量 = 已发放 + 报废 + 已退回 + 在场剩余

办理退料时物料从场次退回中央结余，可再分配给其他场次。任何发放/报废都必须
有对应审计事件；未服务成功（缺同意项、超龄、满场、设备停用、断网拦截等）
绝不消耗物料。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class LedgerError(ValueError):
    """物料不足或台账操作非法。"""


@dataclass
class BatchState:
    batch_id: str
    material: str
    unit: str
    received: float            # 入库量
    allocated: float = 0.0     # 累计分配到各场次
    issued: float = 0.0        # 实际发放给参与者
    wasted: float = 0.0        # 报废
    returns: float = 0.0       # 场次未用退回中央

    @property
    def remaining(self) -> float:
        """批次尚未被使用或报废的数量（在场未用 + 已退回 + 未分配）。"""
        return self.received - self.issued - self.wasted

    @property
    def at_sessions(self) -> float:
        """当前仍留在各场次、未发放未报废未退回的数量。"""
        return self.allocated - self.returns - self.issued - self.wasted

    @property
    def unallocated(self) -> float:
        """中央池中可再分配的数量。"""
        return self.received - self.allocated + self.returns

    def conserved(self) -> bool:
        return (
            self.remaining >= -1e-9
            and self.at_sessions >= -1e-9
            and self.unallocated >= -1e-9
            and abs(self.at_sessions + self.unallocated - self.remaining) < 1e-9
        )


@dataclass
class SessionStock:
    session_id: str
    # material -> batch_id -> qty
    allocated: dict[str, dict[str, float]] = field(default_factory=dict)
    issued: dict[str, dict[str, float]] = field(default_factory=dict)
    wasted: dict[str, dict[str, float]] = field(default_factory=dict)
    returned: dict[str, dict[str, float]] = field(default_factory=dict)

    @staticmethod
    def _add(table: dict[str, dict[str, float]], material: str,
             batch_id: str, qty: float) -> None:
        table.setdefault(material, {})
        table[material][batch_id] = table[material].get(batch_id, 0) + qty

    def free_of(self, material: str, batch_id: str) -> float:
        """该场次该批次当前在场可用量。"""
        return (
            self.allocated.get(material, {}).get(batch_id, 0)
            - self.issued.get(material, {}).get(batch_id, 0)
            - self.wasted.get(material, {}).get(batch_id, 0)
            - self.returned.get(material, {}).get(batch_id, 0)
        )

    def available(self, material: str) -> float:
        return sum(
            self.free_of(material, bid)
            for bid in self.allocated.get(material, {})
        )


class MaterialLedger:
    def __init__(self, catalog):
        self.catalog = catalog
        self.batches: dict[str, BatchState] = {
            bid: BatchState(
                batch_id=bid, material=b["material"],
                unit=b.get("unit", ""), received=b["qty"],
            )
            for bid, b in catalog.batches.items()
        }
        self.stocks: dict[str, SessionStock] = {
            sid: SessionStock(sid) for sid in catalog.sessions
        }
        for al in catalog.allocations:
            self.allocate(al["session_id"], al["batch_id"], al["qty"])

    # ---- 分配 -----------------------------------------------------
    def allocate(self, session_id: str, batch_id: str, qty: float) -> None:
        if session_id not in self.stocks:
            raise LedgerError(f"未知场次 {session_id}")
        batch = self.batches.get(batch_id)
        if batch is None:
            raise LedgerError(f"未知批次 {batch_id}")
        activity = self.catalog.activities[self.catalog.sessions[session_id].activity_id]
        if batch.material not in {m.material for m in activity.materials}:
            raise LedgerError(f"批次 {batch_id} 物料与场次 {session_id} 配方不符")
        if qty > batch.unallocated + 1e-9:
            raise LedgerError(
                f"批次 {batch_id} 可分配量不足：需要 {qty}，可分 {batch.unallocated}"
            )
        batch.allocated += qty
        SessionStock._add(self.stocks[session_id].allocated, batch.material, batch_id, qty)

    # ---- 发放 / 报废 / 退料 --------------------------------------
    def _spread(self, session_id: str, material: str, qty: float,
                table: str) -> dict[str, float]:
        """按批次把 qty 摊入指定台账列（issued/wasted），FEFO 顺序。"""
        stock = self._stock(session_id)
        if stock.available(material) + 1e-9 < qty:
            raise LedgerError(
                f"场次 {session_id} 物料 {material} 不足：需要 {qty}，"
                f"在场可用 {stock.available(material)}"
            )
        picked: dict[str, float] = {}
        need = qty
        for bid in sorted(stock.allocated.get(material, {})):
            free = stock.free_of(material, bid)
            if free <= 1e-9:
                continue
            take = min(need, free)
            picked[bid] = picked.get(bid, 0) + take
            batch = self.batches[bid]
            if table == "issued":
                batch.issued += take
            else:
                batch.wasted += take
            SessionStock._add(getattr(stock, table), material, bid, take)
            need -= take
            if need <= 1e-9:
                break
        return picked

    def issue(self, session_id: str, material: str, qty: float) -> dict[str, float]:
        """从场次库存发放物料，返回 {batch_id: qty} 以便逐批追溯。"""
        return self._spread(session_id, material, qty, "issued")

    def waste(self, session_id: str, material: str, qty: float) -> dict[str, float]:
        """场次在场物料报废（破损、污染、过期等），逐批摊分留痕。"""
        return self._spread(session_id, material, qty, "wasted")

    def return_unused(self, session_id: str, material: str,
                      batch_id: str, qty: float) -> None:
        """闭场或临时换场时，把场次未用物料退回中央结余，可供再分配。"""
        stock = self._stock(session_id)
        if qty > stock.free_of(material, batch_id) + 1e-9:
            raise LedgerError(
                f"场次 {session_id} 批次 {batch_id} 可退 "
                f"{stock.free_of(material, batch_id)}，申请退 {qty}"
            )
        self.batches[batch_id].returns += qty
        SessionStock._add(stock.returned, material, batch_id, qty)

    # ---- 查询 -----------------------------------------------------
    def session_usage(self, session_id: str) -> dict[str, dict[str, float]]:
        stock = self._stock(session_id)
        return {
            material: {
                "allocated": sum(stock.allocated[material].values()),
                "issued": sum(stock.issued.get(material, {}).values()),
                "wasted": sum(stock.wasted.get(material, {}).values()),
                "returned": sum(stock.returned.get(material, {}).values()),
                "available": stock.available(material),
            }
            for material in stock.allocated
        }

    def batch_destination(self, batch_id: str) -> dict:
        """逐批说明资源去向。"""
        b = self.batches[batch_id]
        per_session: dict[str, dict[str, float]] = {}
        for sid, stock in self.stocks.items():
            issued = stock.issued.get(b.material, {}).get(batch_id, 0)
            wasted = stock.wasted.get(b.material, {}).get(batch_id, 0)
            if issued or wasted:
                per_session[sid] = {"issued": issued, "wasted": wasted}
        return {
            "batch_id": b.batch_id,
            "material": b.material,
            "unit": b.unit,
            "received": b.received,
            "issued": b.issued,
            "wasted": b.wasted,
            "remaining": b.remaining,
            "at_sessions": b.at_sessions,
            "returned_to_pool": b.returns,
            "sessions": per_session,
        }

    def all_conserved(self) -> bool:
        return all(b.conserved() for b in self.batches.values())

    def _stock(self, session_id: str) -> SessionStock:
        stock = self.stocks.get(session_id)
        if stock is None:
            raise LedgerError(f"未知场次 {session_id}")
        return stock
