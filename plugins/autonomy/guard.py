"""宽松熔断判定（autonomy 插件的原子构件）：纯函数，不读写任何文件。

只有两类资源闸门：当日 token 预算、连续失败熔断（带冷却）。
判定结果是一个二元组 ``(allowed, state)``：

- ``(True,  'ok')``         正常放行
- ``(True,  'half_open')``  冷却已过，允许一次探测
- ``(False, 'circuit_open')`` 熔断仍在冷却中
- ``(False, 'budget')``       当日预算已用尽
"""

from __future__ import annotations

from typing import Optional, Tuple

STATE_OK = "ok"
STATE_HALF_OPEN = "half_open"
STATE_CIRCUIT_OPEN = "circuit_open"
STATE_BUDGET = "budget"


def check_guard(
    tokens_used: float,
    daily_tokens: float,
    failures: int,
    threshold: int,
    circuit_opened_at: Optional[float],
    cooldown_s: float,
    now: float,
) -> Tuple[bool, str]:
    """按固定顺序判断是否允许继续自主思考。

    1. 失败已达阈值且熔断刚打开、冷却未结束 -> ``(False, 'circuit_open')``
    2. 失败已达阈值且冷却已过 -> 先看预算，预算够则 ``(True, 'half_open')``（允许一次探测）
    3. 当日预算已用尽（``daily_tokens > 0``）-> ``(False, 'budget')``
    4. 其他 -> ``(True, 'ok')``

    ``daily_tokens <= 0`` 表示不限预算。``failures >= threshold`` 但
    ``circuit_opened_at is None`` 视为未熔断，直接走第 3、4 条。
    """
    tripped = failures >= threshold
    budget_limited = daily_tokens > 0 and tokens_used >= daily_tokens

    if tripped and circuit_opened_at is not None:
        if (now - circuit_opened_at) < cooldown_s:
            return False, STATE_CIRCUIT_OPEN
        # 冷却结束：允许一次探测，但预算仍然优先
        if budget_limited:
            return False, STATE_BUDGET
        return True, STATE_HALF_OPEN

    if budget_limited:
        return False, STATE_BUDGET

    return True, STATE_OK
