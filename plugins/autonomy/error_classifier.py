"""Unified error classification layer for autonomy tick results.

Canonical error categories (must map to these standard exit_reason values):

  E1  state_db_corrupted   — StateDbReplacedError / InodeMismatched
                             Action: immediate stop, no retry, alert.
  E2  provider_exhausted   — HTTP 503/429/500/502/504, overload, rate-limit
                             Action: retryable or switch fallback provider.
  E3  turn_budget_exceeded — turns == max_turns, iteration limit
                             Action: synthesize summary, mark non-success receipt.
  E4  process_crashed      — confirmed OOM, SIGKILL, non-zero with signal
                             Action: preserve log, circuit-break exit.
  E_UNCLASSIFIED           — failed with no recognized signal
                             Action: quarantine + alert, no circuit break, no gray count.
  E_AUTH_FAILED            — HTTP 401/403, invalid key
                             Action: stop dispatch, alert, never retry.

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

  error_kind  — None means legitimate run; a string is one of the E1-E4 / E_*
                canonical values or a T2 label (empty_response, refusal,
                loop_detected, partial_success).

  should_quarantine — True when output must NOT be persisted as a summary.
                      False for normal runs and for T3-only warnings.

  audit_flags — Zero or more strings describing T3 pattern hits. Callers MUST
                only log at WARNING level and increment a counter; they must not
                alter flow based on these flags.
"""
from __future__ import annotations

import re
import json
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Canonical exit_reason constants
# ---------------------------------------------------------------------------

E1_STATE_DB_CORRUPTED = "state_db_corrupted"
E2_PROVIDER_EXHAUSTED = "provider_exhausted"
E3_TURN_BUDGET_EXCEEDED = "turn_budget_exceeded"
E4_PROCESS_CRASHED = "process_crashed"
E_UNCLASSIFIED = "unclassified"       # failed with no recognized signal
E_AUTH_FAILED = "auth_failed"         # 401/403 — stop dispatch, never retry
E_ENDPOINT_MISCONFIGURED = "endpoint_misconfigured"  # 404; manual endpoint/model check

# T2 content-contract labels (not E1-E4 but still quarantine-worthy)
T2_EMPTY_RESPONSE = "empty_response"
T2_REFUSAL = "refusal"
T2_LOOP_DETECTED = "loop_detected"
T2_PARTIAL_SUCCESS = "partial_success"  # partial output, not full quarantine

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

# Provider-side HTTP codes that mean "retryable, not our fault".
# 500/502/504 are gateway/proxy errors — treat as transient provider failure.
_PROVIDER_EXHAUSTED_HTTP = frozenset({429, 500, 502, 503, 504})

# Auth codes — stop dispatch permanently until credentials/routes are fixed.
_AUTH_FAILED_HTTP = frozenset({401, 403})

# ---------------------------------------------------------------------------
# F3: Shared canonical pattern table used by BOTH classify() entry points.
# Each entry: (error_kind, compiled_re)  — first match wins.
# ---------------------------------------------------------------------------

_CANONICAL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (E1_STATE_DB_CORRUPTED,
     re.compile(r"StateDbReplacedError|InodeMismatched", re.IGNORECASE)),
    (E2_PROVIDER_EXHAUSTED,
     re.compile(
         r"(?:provider|server|service)\s+(?:is\s+)?(?:overloaded|unavailable|503)|"
         r"\b(?:503|500|502|504)\b.*(?:overload|service\s+unavailable|bad\s+gateway|gateway\s+timeout)|"
         r"temporarily\s+unavailable|rate.?limit(?:ed)?|\b429\b",
         re.IGNORECASE,
     )),
    (E3_TURN_BUDGET_EXCEEDED,
     re.compile(r"turns?==max|iteration.?limit|turn.?budget|max[_\s]turns", re.IGNORECASE)),
    (E4_PROCESS_CRASHED,
     re.compile(r"out\s+of\s+memory|oom.?killer|killed.*-9|killed.*signal\s+9", re.IGNORECASE)),
    # Transport/provider failures are retryable, not process crashes (F3).
    (E2_PROVIDER_EXHAUSTED,
     re.compile(r"request_failed|transport.?failed|connection.?(?:refused|reset|timeout)", re.IGNORECASE)),
    (E_AUTH_FAILED,
     re.compile(r"\b(?:401|403)\b.*(?:unauthorized|forbidden)|api[_\s-]?key\s+(?:invalid|required)", re.IGNORECASE)),
]

# ---------------------------------------------------------------------------
# T3 — text pattern matching (advisory only, never reclassifies)
# ---------------------------------------------------------------------------

_T3_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("t3_state_db",   re.compile(r"StateDbReplacedError|InodeMismatched", re.IGNORECASE)),
    ("t3_overload",   re.compile(
        r"(?:provider|server|service)\s+(?:is\s+)?(?:overloaded|unavailable|503)|"
        r"\b(?:503|500|502|504)\b.*(?:overload|service\s+unavailable)|"
        r"temporarily\s+unavailable|rate.?limit(?:ed)?|\b429\b",
        re.IGNORECASE,
    )),
    ("t3_oom",        re.compile(r"out\s+of\s+memory|killed.*oom|oom.?killer", re.IGNORECASE)),
    ("t3_auth_error", re.compile(r"\b(?:401|403)\b.*(?:unauthorized|forbidden)|api[_\s-]?key\s+(?:invalid|required)", re.IGNORECASE)),
]

# ---------------------------------------------------------------------------
# T2 — refusal markers (content structure contract)
# F4: dual condition (refusal phrase AND no substantial recoverable content)
# F4: added Chinese refusal patterns
# ---------------------------------------------------------------------------

_REFUSAL_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"I(?:'m|\sam)\s+(?:sorry|unable|not\s+able)|"
    r"As\s+an\s+AI\b|"
    r"I\s+(?:cannot|can't|won't)\s+(?:help|assist|provide|do|create|generate)|"
    r"I\s+don't\s+have\s+(?:the\s+ability|access|permission)|"
    r"(?:对不起|抱歉)[，,]?我(?:无法|不能|不可以)|"
    r"作为(?:一个)?AI|"
    r"我(?:无法|不能|不可以)(?:帮助|提供|执行|完成|生成)|"
    r"这超出了我的能力范围"
    r")",
    re.IGNORECASE,
)

# A response has substantial content if it contains >= 50 chars of non-whitespace
# outside of any refusal opener.  Partial refusals with real analysis are NOT quarantined.
_SUBSTANTIAL_CONTENT_MIN_CHARS = 50


def _has_substantial_content(text: str) -> bool:
    """Return True if the text contains meaningful recoverable content."""
    cleaned = re.sub(
        r"(?:I(?:'m|\sam)\s+(?:sorry|unable)|As\s+an\s+AI|I\s+(?:cannot|can't|won't)|"
        r"(?:对不起|抱歉)[，,]?我(?:无法|不能|不可以)|作为(?:一个)?AI|我(?:无法|不能|不可以))",
        "", text, flags=re.IGNORECASE
    )
    return len(cleaned.strip()) >= _SUBSTANTIAL_CONTENT_MIN_CHARS


def _t3_scan(text: str) -> List[str]:
    """Return list of T3 audit flag names matched in *text*. Never raises."""
    if not text:
        return []
    return [name for name, pat in _T3_PATTERNS if pat.search(text)]


def _is_refusal(text: str) -> bool:
    """F4: Dual condition — refusal phrase AND no substantial recoverable content."""
    return bool(_REFUSAL_RE.search(text)) and not _has_substantial_content(text)


def _tool_call_signature(tc: dict) -> str:
    """Stable tool signature; strip timestamps/random request IDs (F5)."""
    name = str(tc.get("tool_name") or tc.get("name") or "?")
    args = tc.get("args") or tc.get("parameters") or tc.get("args_preview") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            pass
    volatile = re.compile(r"(?:timestamp|request[_-]?id|trace[_-]?id|random[_-]?id|nonce)", re.I)
    if isinstance(args, dict):
        clean = {k: v for k, v in args.items() if not volatile.search(str(k))}
        parts = sorted("%s=%s" % (k, json.dumps(v, sort_keys=True, default=str)[:80]) for k, v in clean.items())
        return "%s(%s)" % (name, ",".join(parts))
    return "%s(%s)" % (name, str(args)[:80])


def _is_loop(
    text: str,
    previous_outputs: Optional[List[str]] = None,
    tool_calls: Optional[List[dict]] = None,
    previous_tool_calls: Optional[List[List[dict]]] = None,
) -> bool:
    """F5: Loop detection based on tool-call signatures OR identical raw text.

    Tool-call-based detection (preferred when tool_calls provided):
      Checks whether the current turn's tool call sequence matches any recent
      turn's tool call sequence (same names + same args = no new evidence).

    Text-based detection (fallback):
      Returns True if *text* is identical (stripped) to any recent output.

    ``previous_tool_calls`` is the L0 tool call trace: a list of turn-level
    tool call lists, most-recent first (or last).  Callers should source this
    from ``transcript["tool_calls_by_turn"]`` or equivalent.
    """
    # F5: matching calls are a loop only when the prior result supplied no
    # new evidence; missing result data must not be mistaken for evidence.
    if tool_calls and previous_tool_calls:
        current_sigs = tuple(_tool_call_signature(tc) for tc in tool_calls)
        if current_sigs:
            for prev_turn in previous_tool_calls:
                prev_sigs = tuple(_tool_call_signature(tc) for tc in (prev_turn or []))
                if current_sigs == prev_sigs and len(current_sigs) > 0:
                    current_results = tuple(str(tc.get('result_preview') or tc.get('result') or '') for tc in tool_calls)
                    previous_results = tuple(str(tc.get('result_preview') or tc.get('result') or '') for tc in prev_turn)
                    if all(current_results) and all(previous_results) and current_results == previous_results:
                        return True

    # Text-identity fallback
    if not previous_outputs or not text:
        return False
    t = text.strip()
    return any(t == (p or "").strip() for p in previous_outputs if p)


def _safe_int(value) -> Optional[int]:
    """F6: Convert returncode to int without raising; None on conversion failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify(
    raw: str,
    state: dict,
    exit_reason: Optional[str] = None,
    turns: Optional[int] = None,
    max_turns: Optional[int] = None,
    previous_outputs: Optional[List[str]] = None,
    tool_calls: Optional[List[dict]] = None,
    previous_tool_calls: Optional[List[List[dict]]] = None,
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
        Recent raw outputs for text-identity loop detection (last 3–5 is enough).
    tool_calls:
        Tool calls from the current turn (for F5 tool-signature loop detection).
    previous_tool_calls:
        L0 tool call trace: list of prior turn tool-call lists, sourced from
        transcript["tool_calls_by_turn"]. Used for F5 signature-based loop detection.

    Returns
    -------
    (error_kind, should_quarantine, audit_flags)
    """
    evidence = (state.get("evidence") or {}) if isinstance(state, dict) else {}
    status = str((state.get("status") or "") if isinstance(state, dict) else "")
    returncode = _safe_int(evidence.get("exit_code"))
    timed_out = bool(evidence.get("timed_out"))
    # A gateway timeout alone is not proof that our process was killed (F2).
    process_killed = bool(evidence.get("process_killed") or evidence.get("killed_by_timer"))
    oom = bool(evidence.get("oom"))
    # signal field: normalized to positive signal number (handle both 128+N and -N conventions)
    signal_raw = evidence.get("signal")
    signal_number = _safe_int(signal_raw)
    if signal_number is not None:
        # Normalize: POSIX stores 128+N for shell exit codes; some runners store -N
        # We consider both SIGKILL conventions: 128+9=137, -9, or bare 9
        if signal_number < 0:
            signal_number = -signal_number  # -9 → 9
        elif signal_number > 128:
            signal_number = signal_number - 128  # 137 → 9
    http_status = (state.get("http_status") if isinstance(state, dict) else None)

    # ------------------------------------------------------------------
    # T1a — E1: state DB corruption (highest severity, immediate stop)
    # ------------------------------------------------------------------
    if exit_reason in _STATE_DB_EXIT_REASONS:
        return (E1_STATE_DB_CORRUPTED, True, [])

    # ------------------------------------------------------------------
    # T1b — Auth failure (401/403): stop dispatch, never retry
    # ------------------------------------------------------------------
    if isinstance(http_status, int) and http_status in _AUTH_FAILED_HTTP:
        return (E_AUTH_FAILED, True, [])

    # ------------------------------------------------------------------
    # T1c — E2: provider exhausted (retryable)
    # ------------------------------------------------------------------
    if exit_reason in _PROVIDER_EXHAUSTED_EXIT_REASONS:
        return (E2_PROVIDER_EXHAUSTED, False, [])

    if isinstance(http_status, int) and http_status in _PROVIDER_EXHAUSTED_HTTP:
        return (E2_PROVIDER_EXHAUSTED, False, [])
    if isinstance(http_status, int) and http_status == 404:
        return (E_ENDPOINT_MISCONFIGURED, False, [])

    # ------------------------------------------------------------------
    # T1d — E3: turn budget exhausted
    # ------------------------------------------------------------------
    if exit_reason in _TURN_BUDGET_EXIT_REASONS:
        return (E3_TURN_BUDGET_EXCEEDED, True, [])

    if turns is not None and max_turns is not None and int(turns) >= int(max_turns):
        return (E3_TURN_BUDGET_EXCEEDED, True, [])

    # ------------------------------------------------------------------
    # T1e — E4: process crashed — CONFIRMED process death only
    # F2: OOM confirmed via evidence flag, or signal kill (SIGKILL/SIGTERM),
    #     or non-zero exit with an explicit signal. Gateway timeout alone is unknown.
    # ------------------------------------------------------------------
    if oom:
        return (E4_PROCESS_CRASHED, True, [])

    if process_killed:
        return (E4_PROCESS_CRASHED, True, [])

    # Non-zero returncode WITH explicit signal → confirmed process crash.
    # Non-zero returncode WITHOUT signal → unclassified (may be app error, not OS kill).
    if returncode is not None and returncode != 0:
        if signal_number is not None:
            return (E4_PROCESS_CRASHED, True, [])

    # ------------------------------------------------------------------
    # T1f — Failed status or non-zero returncode without signal:
    #        unclassified — quarantine + alert, do NOT count as gray circuit failure.
    # ------------------------------------------------------------------
    if status == "failed" or (returncode is not None and returncode != 0):
        # Keep textual and structured failure entry points aligned (F3).
        for kind, pattern in _CANONICAL_PATTERNS:
            if kind == E2_PROVIDER_EXHAUSTED and pattern.search(str(raw or "")):
                return (kind, False, [])
        return (E_UNCLASSIFIED, True, [])

    # ------------------------------------------------------------------
    # T2 — content structure contract
    # ------------------------------------------------------------------
    raw_stripped = str(raw or "").strip()

    if not raw_stripped:
        return (T2_EMPTY_RESPONSE, True, [])

    if _is_refusal(raw_stripped):
        return (T2_REFUSAL, True, [])

    if _is_loop(raw_stripped, previous_outputs, tool_calls, previous_tool_calls):
        return (T2_LOOP_DETECTED, True, [])

    # ------------------------------------------------------------------
    # T3 — text pattern matching (advisory only, never blocks)
    # ------------------------------------------------------------------
    audit_flags = _t3_scan(raw_stripped)

    # Structurally clean — normal run with optional T3 annotations.
    return (None, False, audit_flags)


def classify_error_text(text: str) -> Tuple[Optional[str], List[str]]:
    """Classify a known-error diagnostic string without quarantine semantics.

    F3: Uses the shared _CANONICAL_PATTERNS table so both entry points
    produce identical labels for the same error symptom.

    Used when the caller *already knows* the run failed (e.g. inside
    ``_record_partial`` or the ``failed`` branch of ``runner.tick``).
    Returns (error_kind, audit_flags).
    """
    text = str(text or "")
    for kind, pat in _CANONICAL_PATTERNS:
        if pat.search(text):
            return (kind, [])
    return (None, _t3_scan(text))


def record_t3_hits(audit_flags: List[str], db=None) -> int:
    """F10: Persist T3 audit flag hits to events table for false-positive rate tracking.

    Writes one event row per flag with kind='t3_audit'. Accumulates hit counts
    so callers can compute false-positive rates over time.

    Parameters
    ----------
    audit_flags:
        List of T3 flag names returned by classify() or _t3_scan().
    db:
        autonomy.db connection (AutonomyDB instance with .append_event).
        If None, the call is a no-op; production caller wiring remains pending (F10).

    Returns
    -------
    int: number of flag rows written (0 if db is None).
    """
    if not audit_flags or db is None:
        return 0
    written = 0
    for flag in audit_flags:
        try:
            db.append_event(
                kind="t3_audit",
                exit_reason=flag,
                summary="t3_hit flag=%s" % flag,
            )
            written += 1
        except Exception:
            pass
    return written


def should_circuit_count(error_kind: Optional[str]) -> bool:
    """F8: Return True iff this error kind should count toward circuit-breaker failures.

    E2 (provider exhausted) and E3 (turn budget) are environment failures,
    not code failures — they must never trip the circuit breaker.
    E_UNCLASSIFIED is unknown-cause — count it so repeated unknown failures trip safely.
    E_AUTH_FAILED is a config issue, not a code bug — do not count.
    """
    if error_kind in (E2_PROVIDER_EXHAUSTED, E3_TURN_BUDGET_EXCEEDED,
                      E_AUTH_FAILED, E_ENDPOINT_MISCONFIGURED):
        return False
    # E1_STATE_DB_CORRUPTED, E4_PROCESS_CRASHED, T2 labels → count
    return True
