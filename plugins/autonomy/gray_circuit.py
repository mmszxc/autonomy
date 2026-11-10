"""Gray-period circuit breaker (observe-period stub; deployment: post 2026-11-14).

Interface definition only — the rollback logic is not wired into the live tick
path during the P0 observe period. When the observer window closes, wire
``GrayCircuit`` into ``Runner.__init__`` and call ``record_tick_outcome`` after
each tick completes.

Behaviour when active (post-observe):
  - After 2 consecutive tick failures *or* any uncaught exception in tick(),
    ``should_rollback()`` returns True.
  - Callers are expected to:
      1. Atomically restore the last known-good git blob snapshot of the
         plugin directory (``rollback_to_last_good``).
      2. Write an alert event to autonomy.db events table with kind="gray_rollback".
      3. Call ``reset()`` so the counter restarts for the new code.

Why git blob rollback instead of DB-level?
  The DB schema is append-only / immutable-evidence. Rolling back the plugin
  source is safe (the DB contract stays intact). Rolling back DB state is not
  safe during the observe period.

Thresholds are intentionally conservative (2 failures) to avoid spurious
rollback on transient provider overloads. The caller may adjust via the
``failure_threshold`` constructor argument.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional


class GrayCircuit:
    """Tracks consecutive tick failures and surfaces rollback intent.

    This class is OBSERVE-ONLY during P0 period: ``should_rollback()``
    returns False and ``rollback_to_last_good()`` is a no-op until
    ``active=True`` is passed to the constructor.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures before rollback is triggered (default 2).
    plugin_dir:
        Path to the autonomy plugin directory. Used to find the last good
        git blob via ``git log``.
    active:
        Set to True to enable rollback. During P0 observe period keep False.
    """

    def __init__(
        self,
        failure_threshold: int = 2,
        plugin_dir: Optional[str] = None,
        active: bool = False,
    ):
        self.failure_threshold = int(failure_threshold)
        self.plugin_dir = Path(plugin_dir or Path(__file__).parent)
        self.active = bool(active)
        self._consecutive_failures = 0
        self._last_failure_ts: Optional[float] = None
        self._last_good_ref: Optional[str] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record_tick_outcome(self, success: bool, uncaught_exception: bool = False) -> None:
        """Call once per tick with the outcome.

        Parameters
        ----------
        success:
            True if the tick completed with status='ok'.
        uncaught_exception:
            True if an uncaught exception propagated out of tick().
        """
        if success and not uncaught_exception:
            self._consecutive_failures = 0
            self._record_last_good_ref()
        else:
            self._consecutive_failures += 1
            self._last_failure_ts = time.time()

    def should_rollback(self) -> bool:
        """Return True when rollback criteria are met and circuit is active."""
        if not self.active:
            return False
        return self._consecutive_failures >= self.failure_threshold

    def rollback_to_last_good(self, event_log=None) -> dict:
        """Atomically restore last known-good git blob snapshot.

        This is a no-op (returns ``{"status": "noop", "reason": "observe_period"}``
        ) when ``active=False``.

        When active, restores plugin files to the last good git ref recorded
        by ``_record_last_good_ref`` and writes an alert event.

        Returns a status dict with keys: status, ref, files_restored, error.
        """
        if not self.active:
            return {"status": "noop", "reason": "observe_period"}

        ref = self._last_good_ref
        if not ref:
            return {"status": "skipped", "reason": "no_good_ref_recorded"}

        result: dict = {"status": "unknown", "ref": ref, "files_restored": 0}
        try:
            # Restore only plugin Python files from the last good ref.
            proc = subprocess.run(
                ["git", "checkout", ref, "--", str(self.plugin_dir)],
                cwd=str(self.plugin_dir.parent.parent),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                result["status"] = "git_error"
                result["error"] = proc.stderr.strip()[:400]
            else:
                result["status"] = "ok"
                # Count restored .py files (approximate).
                result["files_restored"] = len(list(self.plugin_dir.glob("*.py")))
                self._consecutive_failures = 0
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)[:400]

        # Write alert event (best-effort).
        if event_log is not None:
            try:
                event_log.append_event(
                    kind="gray_rollback",
                    exit_reason=result["status"],
                    summary="ref=%s failures=%d error=%s" % (
                        ref, self._consecutive_failures, result.get("error", "")
                    )[:800],
                )
            except Exception:
                pass

        return result

    def reset(self) -> None:
        """Reset consecutive failure counter (call after successful rollback)."""
        self._consecutive_failures = 0

    def status(self) -> dict:
        """Return current circuit state for diagnostics."""
        return {
            "active": self.active,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "should_rollback": self.should_rollback(),
            "last_failure_ts": self._last_failure_ts,
            "last_good_ref": self._last_good_ref,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_last_good_ref(self) -> None:
        """Record the current HEAD git commit as last-good-ref. Best-effort."""
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.plugin_dir.parent.parent),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                self._last_good_ref = proc.stdout.strip()[:40]
        except Exception:
            pass
