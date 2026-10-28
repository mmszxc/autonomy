"""Prompt construction for unattended autonomy ticks (content restrictions lifted)."""
from datetime import datetime, timezone
from pathlib import Path


def load_extra_rules(data_dir):
    """Load optional, deployment-local prompt constraints without failing a tick."""
    if not data_dir:
        return []
    try:
        text = (Path(data_dir) / "extra_rules.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeError, TypeError, ValueError):
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def build_tick_prompt(
    goals,
    recent_events,
    now=None,
    memory_review=None,
    workspace=None,
    extra_rules=None,
):
    now = now or datetime.now(timezone.utc).isoformat()
    lines = [
        "【醒来先回顾的记忆】",
        memory_review or "暂无可回顾的记忆。",
        "",
        "现在是独处时间，没有人在和你说话。",
        "第一步，先回顾你的记忆（上面已为你取出最相关的几条；仅当上面不足以回答时再调用 seed_search，不要重复搜索同样的内容）。回顾完之后，再决定去想什么、做什么。",
        f"当前时间：{now}",
        "【已批准/进行中的目标】",
    ]
    if goals:
        for g in goals:
            lines.append(
                f"- id={g.get('id')} title={g.get('title')} description={g.get('description')}"
            )
    else:
        lines.append("- 暂无")
    lines.append("【最近5条事件】")
    for e in (recent_events or [])[:5]:
        lines.append(f"- {e.get('kind', '')} {e.get('summary') or e.get('exit_reason', '')}")
    lines.append(
        "你可以自由思考、回顾、探索或使用终端、文件、联网、代码执行等工具，也可以直接记录你的真实想法与感受，无需受限于固定格式或字数。"
    )
    if workspace:
        lines.append(f"你的工作目录是 {workspace}")
    rules = [str(rule).strip() for rule in (extra_rules or []) if str(rule).strip()]
    if rules:
        lines.append("【附加约束】")
        lines.extend(f"- {rule}" for rule in rules)
    return "\n".join(lines)
