"""领域错误类型。

所有被后台拒绝的操作都抛出 :class:`DomainError`，并携带稳定的
``reason`` 代码，便于断网补传时把失败原因一并记入事件日志，
赛后可据此逐条说明未服务成功的原因。
"""


class DomainError(Exception):
    """业务规则拒绝。``reason`` 为机器可读的稳定代码。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# 稳定的拒绝原因代码
CAPACITY_FULL = "capacity_full"            # 名额已满
MATERIAL_SHORTAGE = "material_shortage"    # 物料不足
CONSENT_MISSING = "consent_missing"        # 同意书未齐备
AGE_RESTRICTED = "age_restricted"          # 不符合年龄限制
DEVICE_UNAVAILABLE = "device_unavailable"  # 设备停用或故障
SESSION_CLOSED = "session_closed"          # 场次已取消或结束
BOOKING_STATE = "booking_state"            # 预约状态不允许该操作
NOT_FOUND = "not_found"                    # 引用的对象不存在
VENUE_CAPACITY = "venue_capacity"          # 换场目标场地容纳不下
