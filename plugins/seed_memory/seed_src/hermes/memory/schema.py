from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
import uuid


@dataclass
class Emotion:
    primary: str          # 满足 / 好奇 / 不安 / 悲伤 / 愤怒
    intensity: float      # 0.0 ~ 1.0
    secondary: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Seed:
    summary: str
    participants: List[str]
    emotion: Emotion

    id: str = field(default_factory=lambda: f"seed_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    value_aligned: List[str] = field(default_factory=list)
    value_conflicted: List[str] = field(default_factory=list)
    seeds_planted: List[str] = field(default_factory=list)   # 提炼出的语义结论

    caused_by: List[str] = field(default_factory=list)       # 因果链：来自哪些种子
    led_to: List[str] = field(default_factory=list)          # 因果链：导向哪些种子

    importance: float = 0.5       # 0.0 ~ 1.0，影响衰减速度
    access_count: int = 0
    decay_rate: float = 0.05      # 每次访问后重要性衰减的速率

    embedding: Optional[List[float]] = None  # 向量，检索用
