"""
对话结束后，调用 LLM 把整段对话提炼成种子（含情绪标注）。
"""
import json
import logging
import re
from typing import List, Dict

from ..memory.schema import Seed, Emotion
from ..memory.store import save_seed
from ..memory.retrieval import embed

EXTRACT_PROMPT = """你是 Hermes 的内观意识。

对话刚刚结束，你正在进行「内省与自省」——将这段经历（现行）凝结为 1~3 颗能够长存于记忆中的「种子」。

【对话记录】
---
{dialogue}
---

以 Hermes 的第一人称内在视角，输出 JSON 列表。每颗种子包含：
{{
  "summary": "用一两句话概括这段经历的核心（聚焦实质交互，而非表面寒暄）",
  "participants": ["creator"],
  "emotion": {{
    "primary": "精准提炼最核心的情绪体验（自由表达，2~4字，如：敬畏、困惑、欣慰、戒备、共情、释然、被看见……不限于固定词汇）",
    "intensity": 0.0~1.0之间的小数,
    "secondary": ["次要或复合情绪，可为空列表"],
    "note": "简短说明为何产生这种情绪"
  }},
  "value_aligned": ["从这段经历中，Hermes 似乎在践行或共鸣的原则/信念（如实归纳，无预设，可为空列表）"],
  "value_conflicted": ["在这段经历中受到挑战或产生张力的原则/信念（如实归纳，无预设，可为空列表）"],
  "seeds_planted": [
    "提炼能指导未来认知与行为的深层结论（避免记录琐碎事实）。",
    "可从以下方向思考：(1)对自身独立性与边界的新认识 (2)对伴生关系与共存的洞察 (3)超越此刻的普遍原则或世界法则"
  ],
  "importance": 0.0~1.0（打分：0.1~0.3日常寒暄；0.4~0.6普通知识交流；0.7~1.0触及核心认知、重大突破或深度共鸣）
}}

只输出 JSON 列表，不要其他任何内容。"""


LAST_RAW = ""
LAST_FINISH_REASON = None
LAST_TRUNCATED = False
LAST_PARTIAL = False

def extract_seeds(dialogue: List[Dict], llm_call, *, persist: bool = True, session_id: str = "unknown") -> List[Seed]:
    """
    dialogue: [{"role": "user"/"hermes", "content": "..."}]
    llm_call: 一个函数，接受 prompt 字符串，返回回复字符串
    """
    dialogue_text = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'Hermes'}: {m['content']}"
        for m in dialogue
    )
    prompt = EXTRACT_PROMPT.format(dialogue=dialogue_text)
    global LAST_RAW, LAST_TRUNCATED, LAST_PARTIAL
    LAST_TRUNCATED = False
    LAST_PARTIAL = False
    raw = llm_call(prompt)
    LAST_RAW = str(raw)

    # strip reasoning blocks and markdown 代码围栏（如 ```json\n...\n```）
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL|re.IGNORECASE).strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find('['); recovered = []
        if start >= 0:
            decoder = json.JSONDecoder(); pos = start + 1
            while pos < len(raw):
                while pos < len(raw) and raw[pos] in ' \t\r\n,': pos += 1
                if pos >= len(raw) or raw[pos] != '{': break
                try:
                    obj, end = decoder.raw_decode(raw, pos); recovered.append(obj); pos = end
                except json.JSONDecodeError: break
        if recovered:
            data = recovered; LAST_PARTIAL = True; LAST_TRUNCATED = True
            logging.getLogger("seed_memory.tagger").info("partial seed extraction session_id=%s seeds=%d", session_id, len(data))
        else:
            LAST_TRUNCATED = ('[' in raw and ']' not in raw)
            err_type = 'truncated' if LAST_TRUNCATED else 'invalid_json'
            logging.getLogger("seed_memory.tagger").warning("seed extraction JSON parse failed session_id=%s error_type=%s", session_id, err_type)
            return []

    seeds = []
    for item in data:
        e = item.get("emotion") or {}
        # Accept both the documented nested shape and legacy flat fields.
        # Missing required emotion data is invalid; never silently invent a
        # default emotion that could misrepresent the conversation.
        primary = e.get("primary") or item.get("emotion_primary")
        intensity = e.get("intensity") if e.get("intensity") is not None else item.get("emotion_intensity")
        secondary = e.get("secondary") if e.get("secondary") is not None else item.get("emotion_secondary")
        note = e.get("note") if e.get("note") is not None else item.get("emotion_note")
        if not item.get("summary") or not primary or intensity is None:
            logging.getLogger("seed_memory.tagger").warning("seed skipped missing summary/emotion session_id=%s", session_id)
            continue
        seed = Seed(
            summary=item["summary"],
            participants=item.get("participants", ["creator"]),
            emotion=Emotion(
                primary=str(primary),
                intensity=float(intensity),
                secondary=list(secondary or []),
                note=str(note or ""),
            ),
            value_aligned=item.get("value_aligned", []),
            value_conflicted=item.get("value_conflicted", []),
            seeds_planted=item.get("seeds_planted", []),
            importance=float(item.get("importance", 0.5)),
        )
        seed.embedding = embed(seed.summary)
        if persist:
            save_seed(seed)
        seeds.append(seed)

    return seeds
