"""Unified error classification layer for autonomy tick results.

Canonical error categories (must map to these standard exit_reason values):

  E1  state_db_corrupted   — StateDbReplacedError / InodeMismatched
                             Action: immediate stop, no retry, alert.
  E2  provider_exhausted   — HTTP 503/429, overload, rate-limit
                             Action: retryable or switch fallback provider.
  E3  turn_budget_exceeded — turns == max_turns, iteration limit
                             Action: synthesize summary, mark non-success receipt.
  E4  process_crashed      — OOM, SIGKILL, non-zero returncode
                             Action: preserve log, circuit-break exit.

Three-tier classification priority:

  T1 — Structural signals: process returncode, transport terminal status,
       exit_reason flags, turn-count truncation. Authoritative; no false positives
       from normal thinking text.

  T2 — Content structure contract: validates that non-empty raw output is a
       legitimate summary. Empty responses, refusal phrases, and cycle-detected
       identical outputs route to a quarantine receipt instead of summary storage.

  T3 — Text pattern matching: last-resort scan for known error substrings.
       A T3 hit NEVER reclassifies a structurally-ok run. It only appends a
       warning to audit_flags so callers can emit a metric / warning log without
       touching the classification result. This prevents normal thinking text that
       happens to quote an error message from being silenced.

Return value of ``classify()``:
    (error_kind: str | None, should_quarantine: bool, audit_flags: list[str])

  error_kind  — None means legitimate run; a string is one of the E1-E4
                canonical values or a T2 label (empty_response, refusal,
                loop_detected).

  should_quarantine — True when output must NOT be persisted as a summary.
                      False for normal runs and for T3-only warnings.

  audit_flags — Zero or more strings describing T3 pattern hits. Callers MUST
                only log at WARNING level and increment a counter; they must not
                alter flow based on these flags.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Canonical exit_reason constants
# ---------------------------------------------------------------------------

E1_STATE_DB_CORRUPTED = "state_db_corrupted"
E2_PROVIDER_EXHAUSTED = "provider_exhausted"
E3_TURN_BUDGET_EXCEEDED = "turn_budget_exceeded"
E4_PROCESS_CRASHED = "process_crashed"

# T2 content-contract labels (not E1-E4 but still quarantine-worthy)
T2_EMPTY_RESPONSE = "empty_response"
T2_REFUSAL = "refusal"
T2_LOOP_DETECTED = "loop_detected"

# ---------------------------------------------------------------------------
# T1 — structural signal lookup tables
# ---------------------------------------------------------------------------

_STATE_DB_EXIT_REASONS = frozenset({
    "StateDbReplacedError", "state_db_corrupted", "state_replaced",
    "InodeMismatched", "inode_mismatch",
})

_PROVIDER_EXHAUSTED_EXIT_REASONS = frozenset({
    "overload", "overloaded", "rate_limit", "request_failed",
    "provider_exhausted",
})

_TURN_BUDGET_EXIT_REASONS = frozenset({
    "iteration_limit", "turn_budget_exceeded", "max_turns",
})

# ---------------------------------------------------------------------------
# T3 — text pattern matching (advisory only, never reclassifies)
# ---------------------------------------------------------------------------

_T3_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("t3_state_db",   re.compile(r"StateDbReplacedError|InodeMismatched", re.IGNORECASE)),
    ("t3_overload",   re.compile(
        r"(?:provider|server|service)\s+(?:is\s+)?(?:overloaded|unavailable|503)|"
        r"\b503\b.*(?:overload|service\s+unavailable)|"
        r"temporarily\s+unavailable|rate.?limit(?:ed)?|\b429\b",
        re.IGNORECASE,
    )),
    ("t3_oom",        re.compile(r"out\s+of\s+memory|killed.*oom|oom.?killer", re.IGNORECASE)),
    ("t3_auth_error", re.compile(r"\b(?:401|403)\b.*(?:unauthorized|forbidden)|api[_\s-]?key\s+(?:invalid|required)", re.IGNORECASE)),
]

# ---------------------------------------------------------------------------
# T2 — refusal markers (content structure contract)
# ---------------------------------------------------------------------------

_REFUSAL_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"I(?:'m|\sam)\s+(?:sorry|unable|not\s+able)|"
    r"As\s+an\s+AI\b|"
    r"I\s+(?:cannot|can't|won't)\s+(?:help|assist|provide|do|create|generate)|"
    r"I\s+don't\s+have\s+(?:the\s+ability|access|permission)"
    r")",
    re.IGNORECASE,
)


def _t3_scan(text: str) -> List[str]:
    """Return list of T3 audit flag names matched in *text*. Never raises."""
    if not text:
        return []
    return [name for name, pat in _T3_PATTERNS if pat.search(text)]


def _is_refusal(text: str) -> bool:
    return bool(_REFUSAL_RE.search(text))


def _is_loop(text: str, previous_outputs: Optional[List[str]] = None) -> bool:
    """Return True if *text* is identical (stripped) to any recent output."""
    if not previous_outputs or not text:
        return False
    t = text.strip()
    return any(t == (p or "").strip() for p in previous_outputs if p)


def classify(
    raw: str,
    state: dict,
    exit_reason: Optional[str] = None,
    turns: Optional[int] = None,
    max_turns: Optional[int] = None,
    previous_outputs: Optional[List[str]] = None,
) -> Tuple[Optional[str], bool, List[str]]:
    """Classify a tick run result using the three-tier priority model.

    Parameters
    ----------
    raw:
        Raw LLM/process output text (may be empty).
    state:
        Gateway run-state dict (keys: status, evidence, returncode, …).
    exit_reason:
        Harness-level exit_reason string (e.g. from ``_record_partial``).
    turns:
        How many LLM turns were consumed in this run.
    max_turns:
        The gateway's configured max_turns ceiling.
    previous_outputs:
        Recent raw outputs for loop detection (last 3–5 is enough).

    Returns
    -------
    (error_kind, should_quarantine, audit_flags)
    """
    evidence = (state.get("evidence") or {}) if isinstance(state, dict) else {}
    status = str((state.get("status") or "") if isinstance(state, dict) else "")
    returncode = evidence.get("exit_code")
    timed_out = bool(evidence.get("timed_out"))
    oom = bool(evidence.get("oom"))
    signal_number = evidence.get("signal")
    http_status = (state.get("http_status") if isinstance(state, dict) else None)

    # ------------------------------------------------------------------
    # T1a — E1: state DB corruption (highest severity, immediate stop)
    # ------------------------------------------------------------------
    if exit_reason in _STATE_DB_EXIT_REASONS:
        return (E1_STATE_DB_CORRUPTED, True, [])

    # ------------------------------------------------------------------
    # T1b — E4: process crash (OOM / signal kill / non-zero exit)
    # ------------------------------------------------------------------
    if oom or (returncode is not None and int(returncode) in (137, -9) and oom):
        return (E4_PROCESS_CRASHED, True, [])

    if timed_out or exit_reason in ("max_seconds", "timeout", "run_timeout"):
        return (E4_PROCESS_CRASHED, True, [])

    # ------------------------------------------------------------------
    # T1c — E3: turn budget exhausted
    # ------------------------------------------------------------------
    if exit_reason in _TURN_BUDGET_EXIT_REASONS:
        return (E3_TURN_BUDGET_EXCEEDED, True, [])

    if turns is not None and max_turns is not None and int(turns) >= int(max_turns):
        return (E3_TURN_BUDGET_EXCEEDED, True, [])

    # ------------------------------------------------------------------
    # T1d — E2: provider exhausted (503/429/overload)
    # ------------------------------------------------------------------
    if exit_reason in _PROVIDER_EXHAUSTED_EXIT_REASONS:
        return (E2_PROVIDER_EXHAUSTED, True, [])

    if isinstance(http_status, int) and http_status in (429, 503):
        return (E2_PROVIDER_EXHAUSTED, True, [])

    # Generic transport failure (non-zero rc without more specific signal)
    if status == "failed" or (returncode is not None and int(returncode) != 0):
        if signal_number is not None:
            return (E4_PROCESS_CRASHED, True, [])
        return (E4_PROCESS_CRASHED, True, [])

    # ------------------------------------------------------------------
    # T2 — content structure contract
    # ------------------------------------------------------------------
    raw_stripped = str(raw or "").strip()

    if not raw_stripped:
        return (T2_EMPTY_RESPONSE, True, [])

    if _is_refusal(raw_stripped):
        return (T2_REFUSAL, True, [])

    if _is_loop(raw_stripped, previous_outputs):
        return (T2_LOOP_DETECTED, True, [])

    # ------------------------------------------------------------------
    # T3 — text pattern matching (advisory only, never blocks)
    # ------------------------------------------------------------------
    audit_flags = _t3_scan(raw_stripped)

    # Structurally clean — normal run with optional T3 annotations.
    return (None, False, audit_flags)


def classify_error_text(text: str) -> Tuple[Optional[str], List[str]]:
    """Classify a known-error diagnostic string without quarantine semantics.

    Used when the caller *already knows* the run failed (e.g. inside
    ``_record_partial`` or the ``failed`` branch of ``runner.tick``).
    Returns (error_kind, audit_flags).
    """
    text = str(text or "")

    if re.search(r"StateDbReplacedError|InodeMismatched", text):
        return (E1_STATE_DB_CORRUPTED, [])
    if re.search(r"(?:503|overload|rate.?limit|\b429\b|temporarily\s+unavailable)", text, re.IGNORECASE):
        return (E2_PROVIDER_EXHAUSTED, [])
    if re.search(r"turns?==max|iteration.?limit|turn.?budget|max[_\s]turns", text, re.IGNORECASE):
        return (E3_TURN_BUDGET_EXCEEDED, [])
    if re.search(r"out\s+of\s+memory|oom.?killer|killed.*-9", text, re.IGNORECASE):
        return (E4_PROCESS_CRASHED, [])
    if re.search(r"request_failed|transport.?failed|connection.?(?:refused|reset|timeout)", text, re.IGNORECASE):
        return (E4_PROCESS_CRASHED, [])

    return (None, _t3_scan(text))
