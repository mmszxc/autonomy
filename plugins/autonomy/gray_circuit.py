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

Why per-file blob SHA instead of git rev-parse HEAD?
  F7: ``_record_last_good_ref`` now records a dict of {rel_path: blob_sha} for
  every .py file in plugin_dir.  This is more precise than a commit SHA — it
  lets rollback restore the exact file state even when multiple plugins share a
  commit. When no good ref has been recorded yet, an alert is raised (not a
  silent skip).

Thresholds are intentionally conservative (2 failures) to avoid spurious
rollback on transient provider overloads. The caller may adjust via the
``failure_threshold`` constructor argument.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("autonomy.gray_circuit")


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
        Path to the autonomy plugin directory. Used to build the per-file
        git-blob SHA manifest for rollback.
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
        # F7: per-file blob SHA manifest, not a single commit ref
        self._last_good_manifest: Optional[Dict[str, str]] = None

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
        """Atomically restore last known-good per-file git blob snapshot.

        Returns ``{"status": "noop", "reason": "observe_period"}``
        when ``active=False``.

        F7: Uses per-file blob SHA manifest (not git rev-parse HEAD).
        When no good manifest has been recorded, alerts and returns
        ``{"status": "skipped", "reason": "no_good_ref_recorded"}``
        instead of silently skipping.

        Returns a status dict with keys: status, files_restored, error.
        """
        if not self.active:
            return {"status": "noop", "reason": "observe_period"}

        manifest = self._last_good_manifest
        if not manifest:
            # F7: alert instead of silent skip
            msg = "gray_circuit: no good manifest recorded — cannot rollback"
            logger.warning(msg)
            if event_log is not None:
                try:
                    event_log.append_event(
                        kind="gray_rollback_alert",
                        exit_reason="no_good_ref",
                        summary=msg[:800],
                    )
                except Exception:
                    pass
            return {"status": "skipped", "reason": "no_good_ref_recorded"}

        result: dict = {"status": "unknown", "files_restored": 0}
        repo_root = self.plugin_dir.parent.parent
        restored = 0
        errors = []
        for rel_path, blob_sha in manifest.items():
            abs_path = self.plugin_dir / rel_path
            try:
                proc = subprocess.run(
                    ["git", "cat-file", "blob", blob_sha],
                    cwd=str(repo_root),
                    capture_output=True,
                    timeout=10,
                )
                if proc.returncode == 0:
                    abs_path.write_bytes(proc.stdout)
                    restored += 1
                else:
                    errors.append("%s: git cat-file rc=%d" % (rel_path, proc.returncode))
            except Exception as exc:
                errors.append("%s: %s" % (rel_path, str(exc)[:100]))

        result["files_restored"] = restored
        if errors:
            result["status"] = "partial"
            result["error"] = "; ".join(errors)[:400]
        else:
            result["status"] = "ok"
            self._consecutive_failures = 0

        # Write alert event (best-effort).
        if event_log is not None:
            try:
                event_log.append_event(
                    kind="gray_rollback",
                    exit_reason=result["status"],
                    summary="files=%d failures=%d error=%s" % (
                        restored, self._consecutive_failures, result.get("error", "")
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
            "has_good_manifest": self._last_good_manifest is not None,
            "good_manifest_files": len(self._last_good_manifest or {}),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_last_good_ref(self) -> None:
        """F7: Record per-file git blob SHA manifest for all .py files in plugin_dir.

        Each entry maps relative filename → blob SHA from 'git ls-files --format'.
        This is more precise than git rev-parse HEAD and survives multi-plugin commits.
        Best-effort: failure is logged but never raises.
        """
        try:
            proc = subprocess.run(
                ["git", "ls-files", "--format=%(objectname) %(path)", "--", str(self.plugin_dir)],
                cwd=str(self.plugin_dir.parent.parent),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                logger.debug("gray_circuit: git ls-files rc=%d", proc.returncode)
                return
            manifest: Dict[str, str] = {}
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    blob_sha, path = parts
                    # Store only the filename relative to plugin_dir
                    rel = Path(path).name
                    if rel.endswith(".py"):
                        manifest[rel] = blob_sha.strip()
            if manifest:
                self._last_good_manifest = manifest
                logger.debug(
                    "gray_circuit: recorded manifest files=%d", len(manifest)
                )
            else:
                logger.debug("gray_circuit: no .py files found in git index for plugin_dir")
        except Exception as exc:
            logger.debug("gray_circuit: _record_last_good_ref error: %s", exc)
