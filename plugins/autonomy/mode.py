"""自主思考模式判定：只有一个模型时先服务对话，两个模型不同时才并行。

只用 Python 标准库，兼容 Python 3.10。
"""

from __future__ import annotations

# 两种模式：并行思考 / 对话优先
MODE_PARALLEL = "parallel"
MODE_DIALOG_FIRST = "dialog_first"


def _normalize(model) -> str:
    """None 视作空串；去掉首尾空白并忽略大小写。"""
    if model is None:
        return ""
    return str(model).strip().lower()


def decide_mode(main_model, autonomy_model) -> str:
    """返回 MODE_PARALLEL 或 MODE_DIALOG_FIRST。

    规则：
    - main_model 为 None 或空白：无论 autonomy_model 是什么，都是 MODE_DIALOG_FIRST。
    - autonomy_model 为 None、空串或只有空白：MODE_DIALOG_FIRST（自主继承主模型，只有一个模型）。
    - 两者去掉首尾空白、忽略大小写后相同：MODE_DIALOG_FIRST。
    - 其余情况：MODE_PARALLEL。
    """
    main = _normalize(main_model)
    autonomous = _normalize(autonomy_model)
    if not main or not autonomous:
        return MODE_DIALOG_FIRST
    if main == autonomous:
        return MODE_DIALOG_FIRST
    return MODE_PARALLEL
