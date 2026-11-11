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
    shareable: int = 1            # 1=可供其他 AI 分享，0=私人/临时内容
    decay_rate: float = 0.05      # 每次访问后重要性衰减的速率
    embedding: Optional[List[float]] = None  # 向量，检索用
    provenance_ref: Optional[str] = None  # Full provenance validation is a separate phase.
    status: str = "active"


@dataclass
class SeedAccessLog:
    """Signed access record: who touched which seed, when, why, in which session."""
    id: str = field(default_factory=lambda: f"acc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seed_id: str = ""
    writer: str = ""
    session_id: str = ""
    reason: str = ""
    query_hash: str = ""
    provenance: str = ""


@dataclass
class SemanticMemory:
    """A core value or belief distilled from experience."""
    id: str = field(default_factory=lambda: f"sem_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    category: str = "value"     # value / belief / world_model / self_model
    statement: str = ""
    confidence: float = 0.5     # 0.0 ~ 1.0
    evidence_count: int = 0
    source_seeds: List[str] = field(default_factory=list)
    last_reinforced: Optional[datetime] = None
    access_count: int = 0
    last_accessed: Optional[datetime] = None
    shareable: int = 1             # 1=shareable, 0=private
    embedding: Optional[List[float]] = None


@dataclass
class ProgramMemory:
    """A behavior pattern: trigger context -> behavior."""
    id: str = field(default_factory=lambda: f"prog_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    category: str = "habit"     # habit / heuristic / avoidance / ritual
    pattern: str = ""
    trigger_context: str = ""
    behavior: str = ""
    domain_signature: str = ""
    confidence: float = 0.5
    evidence_count: int = 0
    source_seeds: List[str] = field(default_factory=list)
    counter_seeds: List[str] = field(default_factory=list)
    consecutive_failures: int = 0
    # Program memories are executable guidance: provenance is mandatory before use.
    writer: str = ""
    owner: str = ""
    provenance: str = ""
    last_reinforced: Optional[datetime] = None
    access_count: int = 0
    last_accessed: Optional[datetime] = None
    embedding: Optional[List[float]] = None