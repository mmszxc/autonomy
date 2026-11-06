"""Extension hooks for the autonomy tick lifecycle.

L0 companion module (Step3): the ``on_session_end`` hook inspects the
just-finished tick and emits a ``low_impact`` signal into autonomy.db when
the round produced no measurable external change.  The runner's L0 counter
(``consecutive_no_change``) already tracks git diff / recall deltas; this
hook adds an independent, cheap confirmation signal that can be read by the
runner or by post-hoc analysis.

The module is deliberately self-contained: importing it must never break the
tick path.  All hook entry points swallow their own exceptions.
"""

from __future__ import annotations

import os
import subprocess
import time

try:  # Python 3.9+
    from importlib.metadata import distribution
except Exception:  # pragma: no cover
    distribution = None

__all__ = ["on_session_end", "session_had_impact"]

_LOW_IMPACT_ELAPSED_S = 50
_LOW_IMPACT_TOOL_CALLS = 0


def session_had_impact(workspace, elapsed_s, tool_call_count):
    """Return True when the session shows at least one external impact sign.

    Impact signs (any one is enough):
    - non-empty ``git status --porcelain`` / new commits in the workspace
    - tool_call_count >= 1 (the agent acted on the world, not only itself)
    """
    if tool_call_count is not None and tool_call_count > _LOW_IMPACT_TOOL_CALLS:
        return True
    if workspace and os.path.isdir(str(workspace)):
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(workspace), capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return True
        except Exception:
            pass
    return False


def on_session_end(runner, run_id, transcript, elapsed_s, **kwargs):
    """Called (best-effort) after a tick session finishes.

    Writes an ``ext_hook`` event with ``exit_reason=low_impact`` when the
    session had no external impact, or ``exit_reason=impact`` otherwise.
    Returns the signal dict; never raises.
    """
    try:
        tool_calls = None
        if isinstance(transcript, dict):
            raw = transcript.get("tool_call_count")
            try:
                tool_calls = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                tool_calls = None
        elapsed = float(elapsed_s or 0)
        low_impact = (
            elapsed < _LOW_IMPACT_ELAPSED_S
            and not session_had_impact(getattr(runner, "workspace", None), elapsed, tool_calls)
        )
        signal = {
            "low_impact": bool(low_impact),
            "elapsed_s": elapsed,
            "tool_call_count": tool_calls,
        }
        db = getattr(runner, "db", None)
        if db is not None and hasattr(db, "append_event"):
            db.append_event(
                run_id=run_id,
                kind="ext_hook",
                exit_reason="low_impact" if low_impact else "impact",
                summary="elapsed=%.1fs tools=%s" % (elapsed, tool_calls),
            )
        return signal
    except Exception:
        return {"low_impact": False, "error": "hook_failed"}
