"""Hermes Memory Layer — Seed Memory, Semantic Memory, Program Memory, HEELO Primitives."""

from .schema import Emotion, Seed, SemanticMemory, ProgramMemory, SeedAccessLog
from .store import (
    init_db, configure, save_seeds, save_seed, save_access_log, get_all_seeds,
    get_ranked_seeds, touch_seed, backlink_led_to, list_recent_seeds,
    save_semantic_memory, save_program_memory, get_all_semantic_memories,
    get_all_program_memories, touch_semantic_memory, touch_program_memory,
    sanitize_causal_refs,
)
__all__ = [
    "Emotion", "Seed", "SemanticMemory", "ProgramMemory", "SeedAccessLog",
    "init_db", "configure", "save_seeds", "save_seed", "save_access_log",
    "get_all_seeds", "get_ranked_seeds", "touch_seed", "backlink_led_to",
    "list_recent_seeds", "save_semantic_memory", "save_program_memory",
    "get_all_semantic_memories", "get_all_program_memories",
    "touch_semantic_memory", "touch_program_memory", "sanitize_causal_refs",
]

try:
    from .heelo import (
        Experience, Observation, Reflection, Action, Outcome, ExecutionPrimitive,
        HandoffPrimitive, ObservabilityPrimitive, serialize_heelo, deserialize_heelo,
        seed_to_experience, experience_to_seed, skill_to_execution_primitive,
    )
except ModuleNotFoundError as exc:
    if exc.name != f"{__name__}.heelo":
        raise
else:
    __all__.extend((
        "Experience", "Observation", "Reflection", "Action", "Outcome",
        "ExecutionPrimitive", "HandoffPrimitive", "ObservabilityPrimitive",
        "serialize_heelo", "deserialize_heelo", "seed_to_experience",
        "experience_to_seed", "skill_to_execution_primitive",
    ))

# Production profiles carry the signed writer; source checkouts do not. Avoid
# importing the sensitive implementation into the public repository while still
# exposing production's signed API unchanged whenever the module is present.
try:
    from .signed_write import (
        init_signed_write_path, signed_write_seed, signed_write_access_log,
        signed_write_semantic_memory, signed_write_program_memory,
        record_gray_mode_rejection, get_gray_mode_rejections,
        verify_merkle_chain, verify_write_integrity, export_merkle_proof,
        get_merkle_tail, get_public_key_pem, SignedWriteResult,
        GrayModeRejection,
    )
except ModuleNotFoundError as exc:
    if exc.name != f"{__name__}.signed_write":
        raise
else:
    __all__.extend((
        "init_signed_write_path", "signed_write_seed", "signed_write_access_log",
        "signed_write_semantic_memory", "signed_write_program_memory",
        "record_gray_mode_rejection", "get_gray_mode_rejections",
        "verify_merkle_chain", "verify_write_integrity", "export_merkle_proof",
        "get_merkle_tail", "get_public_key_pem", "SignedWriteResult",
        "GrayModeRejection",
    ))
