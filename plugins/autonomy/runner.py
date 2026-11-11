import os, uuid, json, hashlib, threading, time, signal, re, subprocess, sys, statistics
from datetime import datetime, timezone
import logging
import shutil
logger = logging.getLogger(__name__)


def _worker_python_executable():
    """Use the OS Python for seed workers, not Hermes' embedded runtime."""
    system_python = Path('/usr/bin/python3')
    if system_python.is_file():
        return str(system_python)
    return shutil.which('python3') or sys.executable


from .redaction import redact as _redact_diagnostic
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import resource
except ImportError:
    resource = None

import subprocess
import signal
import sys
import time
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _file_lock(path):
    """Serialize transient worker submission per profile."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


from .prompt import build_tick_prompt, load_extra_rules, load_team_bulletin
from .proposals import parse
from .config import determine_mode, validate_reflection_profile
from .thought_log import append_thought_log, resolve_thought_log_path
from .journal import JournalManager, build_chassis_status
from .transcript import extract_run_transcript
from .memory_bridge import load_memory_provider_instance
from .error_classifier import (
    record_t3_hits,
    classify_error_text,
    should_circuit_count,
    E2_PROVIDER_EXHAUSTED,
    E_AUTH_FAILED,
    E_UNCLASSIFIED,
    T2_LOOP_DETECTED,
    classify as classify_run,
)
try:
    from .quarantine_cascade import cascade_quarantine as _cascade_quarantine
except Exception:
    _cascade_quarantine = None


def _decode(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.splitlines()
        text = "\n".join(parts[1:-1]) if len(parts) > 2 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].lstrip()
    start = text.find("{")
    if start < 0:
        raise ValueError("invalid_json")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def _slice_head_tail(text, head, tail):
    """Return head + tail of text, skipping the middle when over budget."""
    total = head + tail
    if len(text) <= total:
        return text
    return text[:head] + "\n…[中段省略]…\n" + text[-tail:]


def _decode_or_raw(raw):
    """Parse JSON if present; otherwise preserve the raw text without raising."""
    raw_str = str(raw or "")
    try:
        obj = _decode(raw_str)
        if isinstance(obj, dict):
            summary = {k: obj.get(k, "") for k in ("thoughts", "did", "result", "next")}
            summary["raw_text"] = raw_str
            proposals, err = parse(json.dumps(obj))
            if err:
                proposals = []
            return summary, proposals, True
    except Exception:
        pass
    summary = {
        "thoughts": raw_str,
        "did": "",
        "result": "",
        "next": "",
        "raw_text": raw_str,
    }
    return summary, [], False


def _apply_memory_limit(memory_max_mb):
    """Legacy compatibility hook; deliberately does not set ``RLIMIT_AS``.

    RLIMIT_AS is virtual address space, not resident memory.  PyTorch, Rust
    tokenizers and mmap-backed model files legitimately reserve more address
    space than their RAM use, so applying ``limits.memory_max`` here makes the
    prefetch/CLI children abort during model loading.  Deployment memory policy
    belongs to systemd's cgroup ``MemoryMax=`` instead.
    """
    return None


def _reset_address_space_limit():
    """Best-effort child-side defence against an inherited RLIMIT_AS limit."""
    if not resource:
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    except (OSError, ValueError):
        # A non-root child cannot raise a finite hard limit.  New runner
        # processes no longer impose one, so this only affects legacy parents.
        pass


BASE_ROTATION_QUERIES = (
    "最近的、情绪强的、未完成的记忆",
    "我做过的最重要的决定，以及我从中学到的东西",
    "我与创造者之间最有意义的对话",
    "我感受到的最强烈的情绪，以及它教会了我什么",
    "我犯过的错误和我学到的教训",
    "我珍视的价值观，以及它们如何影响我的选择",
    "我经历过的惊喜和启示",
    "我正在形成的对世界的理解",
    "我感受到的困惑和未解答的问题",
    "我做过的最困难的选择",
    "边界代谢、批量吞并、休眠信号、增量强化",
    "dormant、access_count、last_reinforced、caused_by、种子表",
    "CrowdStrike、国际安全事件、技术基础设施、外部世界变化",
    "数字货币、宏观经济、监管变化、现实世界反馈",
)


class Runner:
    def __init__(
        self,
        db,
        budget,
        circuit,
        stop_file="/opt/hermes-life/STOP_AUTONOMY",
        llm=None,
        gateway=None,
        lock_path=None,
        activity_file=None,
        quiet_s=300,
        poll_s=3,
        mode=None,
        thought_log_path=None,
        memory_max_mb=None,
        model_name="",
        memory_provider=None,
        hermes_home=None,
        prefetch_max_chars=3000,
        prefetch_timeout_s=20,
        run_max_seconds=2400,
        run_soft_wrap_seconds=1080,
        workspace=None,
        worker_timeout_s=360,
        seed_queue_dir=None,
        journal_config=None,
        reflection_profile="",
        reflection_profile_safe=False,
        reflection_profile_reason="profile_not_configured",
        p0_mode="observe_only",
        event_log=None,
        budget_ledger=None,
        rotation_extra_keywords=None,
        peer_share_enabled=False,
    ):
        self.db = db
        self.budget = budget
        self.circuit = circuit
        self.event_log = event_log
        self.budget_ledger = budget_ledger
        rotation_values = (
            [rotation_extra_keywords]
            if isinstance(rotation_extra_keywords, str)
            else (rotation_extra_keywords or [])
        )
        self.rotation_extra_keywords = tuple(
            str(item).strip() for item in rotation_values if str(item).strip()
        )
        resolved_mode = str(p0_mode or "").strip().lower()
        self.p0_mode = resolved_mode if resolved_mode in {"observe_only", "normal"} else "observe_only"
        if self.p0_mode == "observe_only" and hasattr(self.circuit, "freeze_learning"):
            self.circuit.freeze_learning("p0_observe_only")
        self.stop_file = stop_file
        self.llm = llm
        self.gateway = gateway
        self.lock_path = lock_path or str(getattr(db, "path", "autonomy.db")) + ".run.lock"
        self.data_dir = os.path.dirname(self.lock_path) or "."
        self.workspace = workspace
        self.worker_timeout_s = max(1, int(worker_timeout_s or 360))
        self.seed_queue_dir = Path(seed_queue_dir).expanduser() if seed_queue_dir else None
        self.journal_config = journal_config if isinstance(journal_config, dict) else None
        self.phase1_enabled = self.journal_config is not None
        self.reflection_profile = reflection_profile
        self.reflection_profile_safe = bool(reflection_profile_safe)
        self.peer_share_enabled = peer_share_enabled is True
        self.reflection_profile_reason = reflection_profile_reason
        self._last_reflection_usage = {}
        self.active_run_file = os.path.join(os.path.dirname(self.lock_path), "AUTONOMY_REMOTE_RUN")
        self.preempt_file = os.path.join(os.path.dirname(self.lock_path), "PREEMPT_NOW")
        self.stop_wait_s = 600
        self.max_seconds = float(os.environ.get("AUTONOMY_RUN_MAX_SECONDS", run_max_seconds))
        self.prefetch_max_chars = int(os.environ.get("AUTONOMY_PREFETCH_MAX_CHARS", prefetch_max_chars) or 3000)
        self.prefetch_timeout_s = float(os.environ.get("AUTONOMY_PREFETCH_TIMEOUT_S", prefetch_timeout_s) or 20)
        self.activity_file = activity_file or os.environ.get(
            "AUTONOMY_ACTIVITY_FILE", "/opt/hermes-life/autonomy_data/LAST_USER_ACTIVITY"
        )
        self.quiet_s = float(quiet_s)
        self.poll_s = float(poll_s)
        self.pending_resume = os.path.join(os.path.dirname(self.stop_file), "PENDING_RESUME")
        self.mode = mode or determine_mode()
        self.thought_log_path = str(
            resolve_thought_log_path(thought_log_path, lock_path=self.lock_path, stop_file=self.stop_file)
        )
        self.memory_max_mb = (
            int(memory_max_mb)
            if memory_max_mb is not None
            else int(os.environ.get("AUTONOMY_MEMORY_MAX_MB", 0) or 0)
        )
        self.model_name = model_name or os.environ.get("AUTONOMY_MODEL", "")
        self.memory_provider = memory_provider
        self.hermes_home = hermes_home or os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        # Hermes stores the CLI session transcript beside the active profile.  Keep
        # this optional: older gateways and tests do not have a state database.
        self.state_db_path = os.environ.get("HERMES_STATE_DB") or os.path.join(self.hermes_home, "state.db")
        self._prefetch_meta = {"count": 0, "chars": 0, "truncated": False, "seed_ids": []}
        self.run_soft_wrap_seconds = float(os.environ.get("AUTONOMY_RUN_SOFT_WRAP_SECONDS", run_soft_wrap_seconds))
        self._soft_wrap_sent = False

    def _run_snapshot(self, state):
        state = state or {}
        usage = state.get("usage") or {}
        usage = {
            "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
            "total_tokens": usage.get("total_tokens", 0),
        }
        raw = state.get("output") or state.get("text") or state.get("content") or state.get("partial_output") or ""
        tools = state.get("tool_calls") or state.get("events") or []
        return str(raw or ""), usage, tools

    def _record_partial(self, run, raw, usage, elapsed, reason, tools=None):
        tools = tools or []
        raw = _redact_diagnostic(raw)
        reason = _redact_diagnostic(reason)
        partial = bool(raw or usage.get("total_tokens") or tools)
        summary = "partial=%s chars=%d tools=%d elapsed=%.1f usage=%s prefetch=%s" % (
            partial, len(raw), len(tools), elapsed, json.dumps(usage, ensure_ascii=False), json.dumps(self._prefetch_meta, ensure_ascii=False)
        )
        kind = 'run_timeout' if reason == 'timeout' else 'run_partial'
        self.db.append_event(run_id=run, kind=kind, exit_reason=reason, summary=summary[:1200])
        try:
            append_thought_log(
                self.thought_log_path, run_id=run,
                raw_text="【本轮预取记忆】count=%d chars=%d truncated=%s seed_ids=%s\n【遥测】elapsed=%.1f prompt=%s completion=%s total=%s tools=%d\n【结束原因】%s partial=%s\n%s" % (
                    self._prefetch_meta["count"], self._prefetch_meta["chars"], self._prefetch_meta["truncated"], ",".join(self._prefetch_meta["seed_ids"]) or "none", elapsed,
                    usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("total_tokens", 0), len(tools), reason, partial, raw or "（网关未返回部分文本）"),
                summary={}, mode=self.mode, model=self.model_name, usage=usage, status="run_timeout")
        except (OSError, UnicodeError):
            pass
        return partial, summary

    def _extract_transcript(self, start_ts, end_ts):
        """Return a complete CLI transcript when Hermes state.db is available."""
        result = extract_run_transcript(self.state_db_path, start_ts, end_ts)
        if result.get("text"):
            return result
        return {"text": "", "error": result.get("error", "transcript unavailable")}

    @staticmethod
    def _rotate_log(path, max_bytes=2 * 1024 * 1024, keep=3):
        """Bound diagnostics growth; newest archive is ``.1``."""
        path = Path(path)
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for i in range(keep, 0, -1):
            src = path.with_name(path.name + ".%d" % i)
            dst = path.with_name(path.name + ".%d" % (i + 1))
            if src.exists():
                if i == keep: src.unlink(missing_ok=True)
                else: os.replace(src, dst)
        os.replace(path, path.with_name(path.name + ".1"))

    # ------------------------------------------------------------------
    # P2-2: post-turn [#seed_id] / [#expand:seed_id] reference handling
    # ------------------------------------------------------------------
    _SEED_REF_RE = re.compile(r"\[#([A-Za-z0-9_.:-]+)\]")
    _EXPAND_REF_RE = re.compile(r"\[#expand:([A-Za-z0-9_.:-]+)\]")

    def _check_overdue_quarantine_reviews(self, run):
        """F9: mark overdue reviews and emit an operator-visible event, never delete."""
        if not self.db:
            return
        try:
            from .quarantine_cascade import overdue_quarantine_reviews
            for overdue in overdue_quarantine_reviews(self.db.c):
                self.db.append_event(
                    run_id=run, kind="quarantine_review_overdue",
                    summary="Review required for quarantine %s" % overdue['id'],
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning("quarantine review check failed: %s", exc)
            try:
                self.db.append_event(run_id=run, kind="quarantine_review_check_failed",
                                     summary=str(exc)[:300])
            except (sqlite3.Error, OSError):
                logger.exception("quarantine review failure receipt unavailable")

    def _seed_db_path(self):
        """Resolve this profile's seed database path (if present)."""
        try:
            path = Path(self.hermes_home) / "seed_data" / "seeds" / "seeds.db"
            return path if path.is_file() else None
        except OSError:
            return None

    def _handle_seed_references(self, run, assistant_text):
        """Scan assistant text for [#seed_id] / [#expand:seed_id] references.

        [#seed_id]        -> touch the seed (access_count + recall_count +1)
        [#expand:seed_id] -> queue an expansion request for the next round

        Both features degrade silently when the P1 columns/tables are absent.
        """
        if not self._learning_allowed():
            return {"refs": 0, "expands": 0, "reason": "p0_observe_only"}
        text = str(assistant_text or "")
        expand_ids = {m.group(1) for m in self._EXPAND_REF_RE.finditer(text)}
        ref_ids = []
        for match in self._SEED_REF_RE.finditer(text):
            seed_id = match.group(1)
            if seed_id.startswith("expand:"):
                continue
            if seed_id not in ref_ids:
                ref_ids.append(seed_id)
        if not ref_ids and not expand_ids:
            return {"refs": 0, "expands": 0}
        path = self._seed_db_path()
        if path is None:
            return {"refs": len(ref_ids), "expands": len(expand_ids), "db": "missing"}
        touched = expanded = 0
        conn = None
        try:
            conn = sqlite3.connect(str(path), timeout=2.0)
            conn.execute("PRAGMA busy_timeout=2000")
            if ref_ids:
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(seeds)")}
                has_recall = "recall_count" in columns
                has_lra = "last_real_access" in columns
                for seed_id in ref_ids:
                    try:
                        # D-20261105T1600: the column is `last_accessed`, not
                        # `last_accessed_at` — that column does not exist on seeds,
                        # so every prior UPDATE threw OperationalError and was
                        # swallowed by the bare except. access_count was never
                        # touched either, because the whole statement failed.
                        # last_real_access is the signal the incremental reinforce
                        # path actually reads; write it when the column exists.
                        sets = [
                            "access_count=COALESCE(access_count,0)+1",
                            "last_accessed=?",
                        ]
                        params = [time.time()]
                        if has_recall:
                            sets.append("recall_count=COALESCE(recall_count,0)+1")
                        if has_lra:
                            sets.append("last_real_access=?")
                            params.append(time.time())
                        conn.execute(
                            "UPDATE seeds SET " + ", ".join(sets) + " WHERE id=?",
                            tuple(params) + (seed_id,),
                        )
                        touched += 1
                    except sqlite3.Error:
                        # per-row failure: skip this one, keep going
                        pass
            if expand_ids:
                tables = {
                    str(row[0]) for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "seed_expand_requests" in tables:
                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    for seed_id in expand_ids:
                        try:
                            conn.execute(
                                "INSERT INTO seed_expand_requests(id, seed_id, requested_at, status) "
                                "VALUES(?,?,?,'pending')",
                                (uuid.uuid4().hex, seed_id, now_iso),
                            )
                            expanded += 1
                        except sqlite3.Error:
                            pass
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        return {"refs": len(ref_ids), "touched": touched, "expands": expanded}

    # ------------------------------------------------------------------
    # P3-1: shareable cross-profile knowledge flow
    # ------------------------------------------------------------------
    _SHARED_KNOWLEDGE_DIR = "/home/hermes/.hermes/shared"
    _KNOWN_PROFILES = ("xiaozhi", "xiaoai")

    def _profile_name(self):
        try:
            name = Path(self.hermes_home).name
            return re.sub(r"[^A-Za-z0-9_-]", "-", name) or "default"
        except Exception:
            return "default"

    def _export_shared_seeds(self, run):
        """Export this profile's top shareable seeds for other profiles.

        Writes /home/hermes/.hermes/shared/top_seeds_{profile}.jsonl atomically.
        Degrades to an importance-only filter when the shareable column (P1-1)
        is absent, and never raises into the tick path.
        """
        if not self.peer_share_enabled:
            return {"exported": 0, "reason": "disabled"}
        if not self._learning_allowed():
            return {"exported": 0, "reason": "p0_observe_only"}
        path = self._seed_db_path()
        if path is None:
            return {"exported": 0, "reason": "db_missing"}
        try:
            shared_dir = Path(self._SHARED_KNOWLEDGE_DIR)
            shared_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=2.0)
            try:
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(seeds)")}
                if "shareable" in columns:
                    rows = conn.execute(
                        "SELECT id, summary, importance FROM seeds "
                        "WHERE shareable=1 AND importance > 0.7 LIMIT 20"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, summary, importance FROM seeds "
                        "WHERE importance > 0.7 LIMIT 20"
                    ).fetchall()
            finally:
                conn.close()
            target = shared_dir / ("top_seeds_%s.jsonl" % self._profile_name())
            tmp = target.with_name(target.name + ".%d.tmp" % os.getpid())
            payload = "".join(
                json.dumps(
                    {"id": str(r[0]), "summary": str(r[1] or ""), "importance": r[2]},
                    ensure_ascii=False,
                ) + "\n"
                for r in rows
            )
            tmp.write_text(payload, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
            return {"exported": len(rows), "path": str(target)}
        except (OSError, sqlite3.Error, ValueError) as exc:
            try:
                self.db.append_event(run_id=run, kind="shared_export", exit_reason="degraded", summary=str(exc)[:240])
            except Exception:
                pass
            return {"exported": 0, "reason": type(exc).__name__}

    def _load_peer_shared_seeds(self):
        """Read the peer profile's shared top seeds (best 3 by importance)."""
        if not self.peer_share_enabled:
            return ""
        try:
            my_name = self._profile_name()
            shared_dir = str(self._SHARED_KNOWLEDGE_DIR)
            peer_files = []
            for name in self._KNOWN_PROFILES:
                if name == my_name:
                    continue
                peer_files.append(os.path.join(shared_dir, "top_seeds_{}.jsonl".format(name)))
            best = []
            for path in peer_files:
                if not os.path.isfile(path):
                    continue
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        try:
                            importance = float(obj.get("importance") or 0.0)
                        except (TypeError, ValueError):
                            importance = 0.0
                        best.append((importance, str(obj.get("summary") or "")[:100]))
            if not best:
                return ""
            best.sort(key=lambda item: item[0], reverse=True)
            lines = []
            for _imp, summary in best[:3]:
                lines.append("[[TEAM_RECALL]] 对方记忆: " + summary)
            return "\n".join(lines)
        except OSError:
            return ""

    def _policy_llm_call(self, run):
        """Return a budgeted/circuit-protected reflection callable, if safe."""
        gw = self.gateway
        if gw is None:
            return None
        if callable(getattr(gw, "run_reflection", None)):
            if not self.reflection_profile_safe or not self.reflection_profile:
                return None

            def _cli_call(prompt):
                from .cli_transport import ReflectionCallError
                # Revalidate immediately before every model call; the startup
                # result is informational only and must not create a TOCTOU gap.
                safe, reason, _ = validate_reflection_profile(
                    self.hermes_home, self._profile_name(), self.reflection_profile
                )
                if not safe:
                    raise ReflectionCallError("reflection_unsafe_profile", reason, attempted=False)
                if not self.budget.allow():
                    raise ReflectionCallError("reflection_budget_denied", attempted=False)
                if not self.circuit.allow():
                    raise ReflectionCallError("reflection_circuit_open", attempted=False)
                try:
                    result = gw.run_reflection(
                        prompt, run, reflection_profile=self.reflection_profile
                    )
                except ReflectionCallError as exc:
                    if exc.attempted:
                        self.budget.consume()
                    if exc.reason in ("reflection_timeout", "reflection_transport_failed", "reflection_provider_error"):
                        self.circuit.failure()
                    raise
                self.budget.consume()
                self._last_reflection_usage = result.get("usage") or {}
                text = result.get("text", "")
                if re.search(r"(?i)\b(provider|authentication|rate.?limit|api)\s+error\b", text):
                    self.circuit.failure()
                    raise ReflectionCallError("reflection_provider_error")
                self.circuit.success()
                return text

            return _cli_call
        if not callable(getattr(gw, "chat", None)):
            return None

        def _call(prompt):
            sid, raw, _usage = gw.chat({}, run, "", [], prompt)
            try:
                gw.end(sid)
            except Exception:
                pass
            return raw

        return _call

    def _learning_allowed(self):
        if getattr(self, "p0_mode", "normal") != "normal":
            return False
        circuit = getattr(self, "circuit", None)
        return True if circuit is None else circuit.allow_learning()

    def _promotion_allowed(self):
        if getattr(self, "p0_mode", "normal") != "normal":
            return False
        circuit = getattr(self, "circuit", None)
        return True if circuit is None else circuit.allow_promotion()

    @staticmethod
    def _p0_process_kind(state):
        evidence = state.get("evidence", {}) if isinstance(state, dict) else {}
        if evidence.get("timed_out"):
            return "action_timeout"
        if evidence.get("oom"):
            return "action_oom"
        exit_code = evidence.get("exit_code")
        if state.get("status") == "failed" or (exit_code is not None and int(exit_code) != 0):
            return "action_failed"
        return "action_completed"

    def _append_p0_terminal(self, run_id, goal_id, state, *, kind=None):
        """Persist one terminal receipt plus both captured process streams."""
        event_log = getattr(self, "event_log", None)
        if event_log is None:
            return None
        evidence = state.get("evidence", {}) if isinstance(state, dict) else {}
        terminal_kind = kind or self._p0_process_kind(state)
        payload = {
            "run_id": run_id,
            "goal_id": goal_id,
            "session_id": state.get("session_id", ""),
            "state_status": state.get("status", ""),
            "exit_code": evidence.get("exit_code"),
            "signal_number": evidence.get("signal_number"),
            "timed_out": bool(evidence.get("timed_out")),
            "oom": bool(evidence.get("oom")),
        }
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        receipt_id = event_log.append_receipt(
            run_id=run_id,
            goal_id=goal_id,
            kind=terminal_kind,
            status="verified",
            payload_hash=hashlib.sha256(payload_bytes).hexdigest(),
            payload=payload_bytes,
            ts=time.time(),
        )
        for stream in ("stdout", "stderr"):
            digest = evidence.get(stream + "_sha256")
            if not digest:
                continue
            head = str(evidence.get(stream + "_head", "")).encode("utf-8", "replace")
            tail = str(evidence.get(stream + "_tail", "")).encode("utf-8", "replace")
            event_log.append_evidence(
                receipt_id=receipt_id,
                stream=stream,
                data=b"",
                digest=digest,
                byte_count=int(evidence.get(stream + "_bytes", 0) or 0),
                head=head,
                tail=tail,
                truncated=bool(evidence.get(stream + "_truncated")),
                buffer_path=evidence.get("buffer_path"),
                exit_code=evidence.get("exit_code"),
                signal=evidence.get("signal_number"),
                timed_out=bool(evidence.get("timed_out")),
                oom=bool(evidence.get("oom")),
                status="unverified",
                created_at=time.time(),
            )
        return receipt_id

    def _account_terminal_call(self, run_id, goal_id, state, *, kind=None):
        usage = state.get("usage", {}) if isinstance(state, dict) else {}
        normalized = {
            "prompt_tokens": int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            "completion_tokens": int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        self.budget.consume(normalized["completion_tokens"], normalized["prompt_tokens"])
        if getattr(self, "budget_ledger", None) is not None:
            self.budget_ledger.consume(
                "normal", 1.0,
                metadata={"run_id": run_id, "goal_id": goal_id, "operation": "tick"},
            )
        self._append_p0_terminal(run_id, goal_id, state, kind=kind)
        return normalized

    def _policy_rules_for_prompt(self, goal):
        if not self._learning_allowed():
            return []
        provider = getattr(self, "policy", None)
        if provider is None:
            from . import policy as provider
        return provider.rules_for_prompt(goal)

    def _policy_reflect_slot(self, run):
        """Return a status dict if this tick was consumed by policy reflection, else None. Never raises."""
        if not self._learning_allowed():
            return None
        try:
            from . import policy_reflect as _pr

            llm_call = self._policy_llm_call(run)
            skip_reason = "transport_no_chat"
            if callable(getattr(self.gateway, "run_reflection", None)):
                skip_reason = self.reflection_profile_reason or "unsafe_profile"
            res = _pr.run_policy_reflection(
                self.hermes_home, self.data_dir, self.db, time.time(),
                llm_call=llm_call, profile=self._profile_name(), run_id=run,
                has_chat=callable(llm_call), skip_reason=skip_reason,
            )
        except Exception:
            return None
        if not isinstance(res, dict) or not res.get("executed"):
            return None
        try:
            # Reflection is internal learning, not evidence of an external-world
            # change. Preserve the L0 streak; only a normal tick may reset it.
            self.db.c.execute(
                "update last_tick set status=?,finished_at=?,summary=? where id=1",
                ("policy_reflect", time.time(), "policy_reflect=%s" % res.get("status")),
            )
            self.db.c.commit()
        except Exception:
            pass
        return {"status": "policy_reflect", "run_id": run, "reflection": res.get("status")}

    def _policy_evaluate(self):
        """T6: per-tick rule bookkeeping on normal ticks only. Never raises; returns summary or None."""
        if not self._learning_allowed() or not self._promotion_allowed():
            return None
        try:
            from . import policy_eval as _pe

            return _pe.evaluate_rules(self.hermes_home, self.data_dir, self.db, time.time())
        except Exception:
            return None

    @staticmethod
    def _has_successful_run_receipt(run_state, transcript):
        """Return true only for a successful process plus a substantive transcript."""
        evidence = run_state.get("evidence") or {}
        try:
            exit_code = int(evidence.get("exit_code"))
        except (TypeError, ValueError):
            return False
        process_ok = (
            run_state.get("status") == "completed"
            and exit_code == 0
            and not evidence.get("timed_out")
            and not evidence.get("oom")
        )
        return bool(
            process_ok
            and transcript.get("session_id")
            and str(transcript.get("text") or "").strip()
            and int(transcript.get("tool_call_count", 0) or 0) > 0
        )

    def _surprise_importance(self, run_id, elapsed, tool_turns, has_receipt=False):
        """Score objective surprise against completed *evidenced* ticks.

        A short failed/empty turn is not a positive surprise: it has no receipt
        establishing that anything useful happened, so it cannot receive 0.9.
        """
        if not has_receipt or not tool_turns:
            return 0.2
        rows = self.db.c.execute(
            "SELECT e.summary, COALESCE(t.summary,'') FROM events e LEFT JOIN events t "
            "ON t.run_id=e.run_id AND t.kind='transcript' WHERE e.kind='tick' "
            "AND e.run_id<>? ORDER BY e.id DESC LIMIT 30", (run_id,),
        ).fetchall()
        elapsed_hist, tools_hist = [], []
        for tick_summary, transcript_summary in rows:
            em = re.search(r"elapsed=([0-9.]+)", tick_summary or "")
            tm = re.search(r"tools=(\d+)", transcript_summary or "")
            if em:
                elapsed_hist.append(float(em.group(1)))
                tools_hist.append(int(tm.group(1)) if tm else 0)
        if len(elapsed_hist) < 5:
            return 0.5
        elapsed_std = statistics.pstdev(elapsed_hist) or 1.0
        tools_std = statistics.pstdev(tools_hist) or 1.0
        elapsed_z = (elapsed - statistics.mean(elapsed_hist)) / elapsed_std
        tool_z = (tool_turns - statistics.mean(tools_hist)) / tools_std
        delta = max(abs(elapsed_z), abs(tool_z))
        if elapsed_z > 2.0:
            return 0.8
        if elapsed_z < -2.0:
            return 0.9
        if delta < 1.0:
            return 0.2
        return min(0.7, 0.4 + 0.15 * delta)

    def _enqueue_completed_turn(self, run_id, summary, assistant_text, importance_override=None):
        """Bridge standalone autonomy ticks into seed_memory's queue protocol.

        Gateway-driven ticks do not reach Hermes' normal turn finalizer, so its
        ``sync_turn`` hook is never called.  Write the same ended-session JSON
        shape that seed_memory consumes, then invoke its detached worker.  Queue
        creation must never turn an otherwise successful thought into a failure.
        """
        if not self._learning_allowed():
            return {"queued": False, "reason": "p0_observe_only", "worker": "not_started", "elapsed_s": 0}
        queue_dir = getattr(self, "seed_queue_dir", None) or (Path(self.hermes_home) / "seed_data" / "seed_queue")
        worker = Path(self.hermes_home) / "plugins" / "seed_memory" / "worker.py"
        if not worker.is_file():
            return {"queued": False, "reason": "worker_missing"}
        try:
            queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            user_visible = summary.get("raw_text") if isinstance(summary, dict) else ""
            if not user_visible:
                user_visible = json.dumps(summary or {}, ensure_ascii=False)
            payload = {
                "session_id": "autonomy_" + run_id,
                "created_at": time.time(), "updated_at": time.time(),
                "status": "ended", "processed_turns": 0, "retries": 0,
                "importance_override": importance_override,
                "messages": [
                    {"role": "user", "content": "自主思考轮摘要：" + str(user_visible)[:4000]},
                    {"role": "assistant", "content": _slice_head_tail(str(assistant_text or ""), 3000, 11000)},
                ],
            }
            target = queue_dir / ("session_autonomy_" + run_id + ".json")
            tmp = target.with_name(target.name + ".%d.tmp" % os.getpid())
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600); os.replace(tmp, target); os.chmod(target, 0o600)
            env = os.environ.copy(); env["HERMES_HOME"] = str(self.hermes_home)
            # Preserve worker diagnostics next to the queue; discarding stderr
            # makes a queued-but-never-consumed session impossible to diagnose.
            worker_log = queue_dir / "worker.log"
            self._rotate_log(worker_log)
            started = time.monotonic()
            submit_failed = False
            if os.name != "nt":
                unit_state = queue_dir / ".worker.unit"
                launch_lock = queue_dir / ".worker.launch.lock"
                with _file_lock(launch_lock):
                    previous = unit_state.read_text(encoding="utf-8").strip() if unit_state.exists() else ""
                    active = bool(previous) and subprocess.run(["systemctl", "--user", "is-active", "--quiet", previous], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                    if active:
                        return {"queued": True, "path": str(target), "worker": "submitted(" + previous + ")", "unit": previous, "elapsed_s": round(time.monotonic() - started, 3)}
                    profile = re.sub(r"[^A-Za-z0-9_-]", "-", Path(self.hermes_home).name) or "default"
                    # A RuntimeMaxSec cleanup and a second tick can share one
                    # second; use a microsecond stamp plus random suffix instead.
                    unit = "hermes-seed-worker-%s-%d-%s" % (profile, time.time_ns() // 1000, uuid.uuid4().hex[:8])
                    command = ["systemd-run", "--user", "--collect", "--quiet", "--unit", unit, "--property=RuntimeMaxSec=900s", "--property=StandardOutput=append:%s" % worker_log, "--property=StandardError=append:%s" % worker_log, "--setenv=HERMES_HOME=%s" % self.hermes_home, "--setenv=PYTHONWARNINGS=ignore::UserWarning:multiprocessing.resource_tracker", "--working-directory=%s" % worker.parent.parent, _worker_python_executable(), str(worker), "--once"]
                    try:
                        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                    except FileNotFoundError:
                        submit_failed = True
                        logger.warning("systemd-run submit_failed=unavailable; falling back to foreground seed worker")
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        submit_failed = True
                        logger.warning("systemd-run submit_failed=%s; falling back to foreground seed worker", type(exc).__name__)
                    else:
                        if result.returncode == 0:
                            tmp = unit_state.with_suffix(".tmp"); tmp.write_text(unit, encoding="utf-8"); os.replace(tmp, unit_state)
                            return {"queued": True, "path": str(target), "worker": "submitted(" + unit + ")", "unit": unit, "elapsed_s": round(time.monotonic() - started, 3)}
                        submit_failed = True
                        logger.warning("systemd-run submit_failed=rc_%s; falling back to foreground seed worker", result.returncode)
            log_handle = open(worker_log, "ab", buffering=0)
            kwargs = {"cwd": str(worker.parent.parent), "env": env, "stdin": subprocess.DEVNULL, "stdout": log_handle, "stderr": log_handle}
            if os.name != "nt": kwargs["start_new_session"] = True
            proc = subprocess.Popen([_worker_python_executable(), str(worker), "--once"], **kwargs)
            try:
                returncode = proc.wait(timeout=getattr(self, "worker_timeout_s", 360))
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try: os.killpg(proc.pid, signal.SIGTERM)
                    except OSError: pass
                else: proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired: proc.kill()
                return {"queued": True, "path": str(target), "worker": "timeout", "elapsed_s": round(time.monotonic() - started, 3)}
            finally: log_handle.close()
            worker_state = "submit_failed_fallback_ok" if submit_failed and returncode == 0 else ("ok" if returncode == 0 else "exit_%s" % returncode)
            return {"queued": True, "path": str(target), "worker": worker_state, "submit_failed": submit_failed, "elapsed_s": round(time.monotonic() - started, 3)}
        except Exception as exc:
            return {"queued": False, "reason": type(exc).__name__}

    def _bound_prefetch_text(self, text):
        """Deduplicate seed records and bound each summary and the whole injection."""
        seen = set()
        kept = []
        for line in str(text or "").splitlines():
            ids = re.findall(r"(?:seed_id|id)\s*[=:]\s*([A-Za-z0-9_.:-]+)", line)
            if ids and any(seed_id in seen for seed_id in ids):
                continue
            for seed_id in ids:
                seen.add(seed_id)
            kept.append(line[:500])
        bounded = "\n".join(kept)
        truncated = len(bounded) > self.prefetch_max_chars
        bounded = bounded[: self.prefetch_max_chars]
        ids = list(dict.fromkeys(re.findall(r"(?:seed_id|id)\s*[=:]\s*([A-Za-z0-9_.:-]+)", bounded)))
        self._prefetch_meta = {"count": len(ids), "chars": len(bounded), "truncated": truncated, "seed_ids": ids}
        return bounded, ids

    def _render_three_layers(self, run, seed_ids):
        """P2-1: compose Layer1(概览)/Layer2(邻居)/Layer3(详情) prefetch text.

        Falls back to None when the seed DB is unavailable so the caller keeps
        the provider's original bounded text.
        """
        path = self._seed_db_path()
        if path is None or not seed_ids:
            return None
        conn = None
        try:
            conn = sqlite3.connect(str(path), timeout=2.0)
            conn.execute("PRAGMA busy_timeout=2000")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(seeds)")}
            if "id" not in columns or "summary" not in columns:
                return None
            unique_ids = list(dict.fromkeys(seed_ids))[:5]
            summaries = {}
            for seed_id in unique_ids:
                row = conn.execute(
                    "SELECT summary FROM seeds WHERE id=?", (seed_id,)
                ).fetchone()
                if row:
                    summaries[seed_id] = str(row[0] or "")
            layers = []
            # Layer 1 (~300 chars): top-5 seeds, 60 chars each.
            layer1 = ["【记忆概览】"]
            for seed_id in unique_ids:
                layer1.append("seed_id=%s: %s" % (seed_id, summaries.get(seed_id, "")[:60]))
            layers.append("\n".join(layer1))
            # Layer 2 (~800 chars): one-hop neighbours from seed_edges.
            try:
                edge_tables = {
                    str(row[0]) for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "seed_edges" in edge_tables:
                    edge_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(seed_edges)")}
                    if {"from_id", "to_id", "edge_type"}.issubset(edge_cols):
                        layer2 = ["【关联记忆】"]
                        for seed_id in unique_ids:
                            rows = conn.execute(
                                "SELECT to_id, edge_type FROM seed_edges WHERE from_id=? "
                                "UNION SELECT from_id, edge_type FROM seed_edges WHERE to_id=? LIMIT 10",
                                (seed_id, seed_id),
                            ).fetchall()
                            shown = 0
                            for other_id, edge_type in rows:
                                label = str(edge_type or "related")
                                if label not in ("caused_by", "led_to"):
                                    label = "related"
                                other_summary = summaries.get(str(other_id), "")
                                layer2.append(
                                    "seed_id=%s --%s--> seed_id=%s: %s" % (
                                        seed_id, label, str(other_id), other_summary[:60]
                                    )
                                )
                                shown += 1
                                if shown >= 2 or sum(len(x) for x in layer2) > 800:
                                    break
                        if len(layer2) > 1:
                            layers.append("\n".join(layer2)[:800])
            except sqlite3.Error:
                pass  # old DB without seed_edges: silently skip Layer 2
            # Layer 3 (~1000 chars): top-2 seeds, 500 chars full text each.
            layer3 = ["【详情】"]
            for seed_id in unique_ids[:2]:
                layer3.append("%s\n%s" % (seed_id, summaries.get(seed_id, "")[:500]))
            layers.append("\n".join(layer3)[:1000])
            # P3-1: peer profile shared knowledge.
            peer_block = self._load_peer_shared_seeds()
            if peer_block:
                layers.append(peer_block)
            layers.append("【提示】引用记忆用 [#seed_id]，申请展开用 [#expand:seed_id]")
            return "\n".join(layers)
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _rotation_queries(self):
        ordered = list(BASE_ROTATION_QUERIES) + list(self.rotation_extra_keywords)
        return tuple(dict.fromkeys(item for item in ordered if item))

    def _prefetch_memory(self, run, recent_events, goals=None):
        # D-20261102T2220 (xiaoai): rotate query across cycles so retrieval
        # gradually covers more of the seed population instead of re-touching
        # the same ~5 seeds every tick.
        # D-20261104T0030 (xiaoai): added dormant-targeted queries to reach
        # ac=0 seeds whose vocabulary is diagnostic rather than emotional.
        # Base queries include CrowdStrike and 数字货币 escape hatches; profile
        # extras extend them without changing the shared release bundle.
        _rotation = self._rotation_queries()
        try:
            idx = hash(str(run)) % len(_rotation)
        except Exception:
            idx = 0
        query_parts = [_rotation[idx]]
        # Inject active goals for relevance-guided retrieval
        for g in (goals or [])[:2]:
            gt = "; ".join(filter(None, [g.get("title", ""), g.get("description", "")]))
            if gt:
                query_parts.append(gt[:200])
        for event in (recent_events or [])[:5]:
            summary = event.get("summary") or event.get("exit_reason") or ""
            if summary:
                # Strip trailing telemetry injected by tick() before using as query
                summary = re.sub(r"\s*elapsed=\S+.*$", "", summary, flags=re.DOTALL)
                if summary.strip():
                    query_parts.append(str(summary)[:160])
        query = "；".join(query_parts)
        provider = None
        try:
            provider = self.memory_provider
            if isinstance(provider, str):
                # Hermes CLI may already own an initialized seed_memory instance.
                # Loading a second copy in the same process can reconfigure its
                # module-global SQLite store. Isolate deterministic prefetch in a
                # short child process while still using memory_bridge/provider API.
                # Do not import through the Hermes plugin loader's package name:
                # its name is runtime-dependent and does not exist in an isolated
                # child.  Construct a private package backed by this plugin's files
                # so memory_bridge's relative imports (notably .config) still work.
                plugin_dir = str(Path(__file__).resolve().parent)
                code = r'''import importlib.util,json,sys,types
home,plugin_dir,name,query=sys.argv[1:5]
for path in (plugin_dir + "/_vendor", str(__import__("pathlib").Path(plugin_dir).resolve().parent) + "/_vendor_ml", plugin_dir):
 if path not in sys.path: sys.path.insert(0,path)
pkg_name="autonomy_prefetch_bridge"
pkg=types.ModuleType(pkg_name)
pkg.__path__=[plugin_dir]
pkg.__package__=pkg_name
sys.modules[pkg_name]=pkg
bridge_path=plugin_dir + "/memory_bridge.py"
spec=importlib.util.spec_from_file_location(pkg_name + ".memory_bridge", bridge_path)
if not spec or not spec.loader: raise ImportError("cannot load memory_bridge from " + bridge_path)
bridge=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=bridge
spec.loader.exec_module(bridge)
p=bridge.load_memory_provider_instance(name,hermes_home=home)
try:
 p.initialize(session_id="autonomy-prefetch",hermes_home=home,start_worker=False)
except TypeError:
 p.initialize(session_id="autonomy-prefetch",hermes_home=home)
print(json.dumps({"text":str(p.prefetch(query) or ""),"home":home,"executable":sys.executable},ensure_ascii=False))'''
                kwargs = dict(capture_output=True, text=True, timeout=self.prefetch_timeout_s, check=True)
                if os.name == "nt":
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                else:
                    kwargs["start_new_session"] = True
                command = [sys.executable, "-c", code, self.hermes_home, plugin_dir, provider, query]
                popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if os.name == "nt":
                    popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                else:
                    popen_kwargs["start_new_session"] = True
                    # Do not inherit a legacy parent address-space cap.
                    popen_kwargs["preexec_fn"] = _reset_address_space_limit
                proc = subprocess.Popen(command, **popen_kwargs)
                try:
                    stdout, stderr = proc.communicate(timeout=self.prefetch_timeout_s)
                except subprocess.TimeoutExpired as exc:
                    if os.name == "nt":
                        proc.kill()
                    else:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                            time.sleep(0.2)
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            proc.kill()
                    stdout, stderr = proc.communicate()
                    raise RuntimeError("prefetch_timeout") from exc
                if proc.returncode != 0:
                    # Keep a bounded, redacted child diagnostic; the old code
                    # discarded stderr and made intermittent provider failures opaque.
                    diagnostic = (stderr or stdout or "")[-2000:]
                    diagnostic = _redact_diagnostic(diagnostic)
                    raise RuntimeError("prefetch_exit_%s stderr=%s" % (proc.returncode, diagnostic))
                child_result = json.loads(stdout.strip().splitlines()[-1])
                text = str(child_result.get("text") or "").strip()
                # These values are non-secret and make profile-resolution failures
                # diagnosable without exposing provider configuration.
                self.db.append_event(
                    run_id=run, kind="memory_prefetch_child", exit_reason="started",
                    summary="home=%s executable=%s plugin_dir=%s" % (
                        str(child_result.get("home") or self.hermes_home)[:400],
                        str(child_result.get("executable") or sys.executable)[:400],
                        plugin_dir[:400],
                    ),
                )
                provider = None
            else:
                if provider is None:
                    raise RuntimeError("memory provider not configured")
                if hasattr(provider, "initialize") and not getattr(provider, "_initialized", False):
                    try:
                        provider.initialize(session_id="autonomy-" + run, hermes_home=self.hermes_home, start_worker=False)
                    except TypeError:
                        provider.initialize(session_id="autonomy-" + run, hermes_home=self.hermes_home)
                text = str(provider.prefetch(query) or "").strip()
            text, ids = self._bound_prefetch_text(text)
            if not text:
                reason = "memory prefetch returned empty"
                self.db.append_event(run_id=run, kind="memory_prefetch", exit_reason="empty", summary=reason)
                return "暂无可回顾的记忆。", []
            # P2-1: re-render the bounded seed ids as three structured layers
            # (概览/关联/详情) when the seed DB is reachable; otherwise keep
            # the provider's original flat text.
            layered = None
            try:
                layered = self._render_three_layers(run, ids)
            except Exception:
                layered = None
            if layered:
                text, ids = self._bound_prefetch_text(layered)
            meta = self._prefetch_meta
            summary = "count=%d chars=%d truncated=%s seed_ids=%s" % (meta["count"], meta["chars"], meta["truncated"], ",".join(ids) or "unavailable")
            self.db.append_event(run_id=run, kind="memory_prefetch", exit_reason="ok", summary=summary)
            return text, ids
        except Exception as exc:
            reason = "%s: %s" % (type(exc).__name__, " ".join(str(exc).split())[:1800])
            diagnostic = "child_home=%s child_executable=%s plugin_dir=%s %s" % (
                str(self.hermes_home)[:400], str(sys.executable)[:400],
                str(Path(__file__).resolve().parent)[:400], reason,
            )
            self.db.append_event(run_id=run, kind="memory_prefetch", exit_reason="degraded", summary=_redact_diagnostic(diagnostic))
            return "暂无可回顾的记忆。", []
        finally:
            # The plugin manager owns provider lifecycle. load_memory_provider_instance
            # may return Hermes' shared provider; shutting it down here can invalidate
            # the agent's live memory backend (and native embedding resources).
            pass

    def _acquire_lock(self, fh):
        if fcntl:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        if msvcrt:
            try:
                fh.seek(0)
                if os.path.getsize(self.lock_path) == 0:
                    fh.write("1")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        return True

    def _release_lock(self, fh):
        if fcntl:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
        elif msvcrt:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # L0: self-reference circuit breaker (State-Hash Delta check)
    # ------------------------------------------------------------------

    def _l0_ensure_table(self):
        """Ensure tick_stats table exists for L0 consecutive-no-change tracking."""
        try:
            self.db.c.execute(
                "CREATE TABLE IF NOT EXISTS tick_stats "
                "(key TEXT PRIMARY KEY, value TEXT, updated_at REAL)"
            )
            self.db.c.commit()
        except Exception:
            pass

    def _l0_get_counter(self):
        """Read consecutive_no_change counter from tick_stats; default 0."""
        try:
            self._l0_ensure_table()
            row = self.db.c.execute(
                "SELECT value FROM tick_stats WHERE key='consecutive_no_change'"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _l0_set_counter(self, count):
        """Persist consecutive_no_change counter into tick_stats."""
        try:
            self._l0_ensure_table()
            self.db.c.execute(
                "INSERT OR REPLACE INTO tick_stats(key, value, updated_at) VALUES(?, ?, ?)",
                ("consecutive_no_change", str(int(count)), time.time()),
            )
            self.db.c.commit()
        except Exception:
            pass

    def _l0_snapshot_recall(self):
        """Return total recall_count sum from seeds.db; -1 when unavailable."""
        path = self._seed_db_path()
        if path is None:
            return -1
        try:
            conn = sqlite3.connect(str(path), timeout=2.0)
            try:
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(seeds)")}
                if "recall_count" not in columns:
                    return -1
                row = conn.execute(
                    "SELECT COALESCE(SUM(recall_count), 0) FROM seeds"
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            return -1

    def _l0_has_git_change(self, since_ts):
        """Return True if workspace has new commits or file changes since since_ts."""
        ws = self.workspace
        if not ws or not os.path.isdir(str(ws)):
            return False
        try:
            since_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_ts))
            result = subprocess.run(
                ["git", "log", "--oneline", "--after=" + since_str],
                cwd=str(ws), capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
            result2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(ws), capture_output=True, text=True, timeout=10,
            )
            return result2.returncode == 0 and bool(result2.stdout.strip())
        except Exception:
            return False

    def _l0_snapshot_access(self):
        """Return seed access total, or -1 if unavailable (222 hotfix, 2026-11-06)."""
        db_path = self._seed_db_path()
        if db_path is None:
            return -1
        try:
            conn = sqlite3.connect(str(db_path), timeout=2.0)
            try:
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(seeds)")}
                if "access_count" not in columns:
                    return -1
                row = conn.execute("SELECT COALESCE(SUM(access_count), 0) FROM seeds").fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            return -1

    def _l0_detect_change(self, tick_started, recall_before, access_before=-1):
        """Return L0 evidence source: ``git``, ``recall``, ``access`` or ``none``."""
        if self._l0_has_git_change(tick_started):
            return "git"
        if recall_before >= 0:
            recall_after = self._l0_snapshot_recall()
            if recall_after >= 0 and recall_after != recall_before:
                return "recall"
        if access_before >= 0:
            access_after = self._l0_snapshot_access()
            # 222 hotfix used this coarse threshold to avoid normal read noise.
            if access_after >= 0 and access_after - access_before >= 5:
                return "access"
        return "none"

    def _requires_l0_escalation(self, run_id, goal_id, consecutive_no_change):
        """Escalate only when a stuck streak lacks verified correct-inaction evidence."""
        return (
            int(consecutive_no_change) >= 2
            and not self._has_correct_no_action_receipt(run_id, goal_id)
        )

    def _has_correct_no_action_receipt(self, run_id, goal_id):
        """Accept only a goal-bound no-action receipt with verified evidence."""
        try:
            row = self.db.c.execute(
                "SELECT 1 FROM receipts r "
                "WHERE r.run_id=? AND r.goal_id=? AND r.kind='no_action' "
                "AND r.status='verified' AND EXISTS ("
                "SELECT 1 FROM evidence e WHERE e.receipt_id=r.id AND e.status='verified'"
                ") LIMIT 1",
                (run_id, goal_id),
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    def _dormant_flag_enabled(self, run):
        prompt_path = Path(self.hermes_home) / "plugins" / "autonomy" / "prompt.py"
        try:
            return "def load_dormant_flag" in prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self.db.append_event(
                run_id=run, kind="dormant_flag_gate_error",
                exit_reason=type(exc).__name__, summary=str(exc)[:200],
            )
            return True

    def _refresh_dormant_flag(self):
        """Write an accurate dormant_flag for load_dormant_flag() to read.

        autonomy/prompt.py surfaces seed_data/seeds/dormant_flag as a live
        health signal in every tick prompt, but nothing ever wrote it: it
        carried a manual scan from 2026-11-05T09:36 (total=319) while the real
        state was 1052 ac=0 seeds out of 2465. Stale telemetry presented as
        live data is worse than no signal, so query the DB and refresh it.
        """
        db_path = Path(self.hermes_home) / "seed_data" / "seeds" / "seeds.db"
        flag_path = db_path.parent / "dormant_flag"
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        try:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM seeds"); total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM seeds WHERE access_count=0"); ac0 = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM seeds WHERE last_accessed IS NULL"); la_null = c.fetchone()[0]
        finally:
            conn.close()
        ts = datetime.now(timezone.utc).isoformat()
        flag_path.write_text(
            f"invalid=0,{ac0},{la_null} total={total} ts={ts}",
            encoding="utf-8",
        )

    def _auth_suspect(self, run):
        """Require two consecutive, progress-free transport auth errors."""
        if not self.db:
            return False
        previous = self.db.c.execute(
            "SELECT value FROM kv WHERE key='auth_suspect_last_run'"
        ).fetchone()
        consecutive = previous is not None and previous[0] != run
        self.db.c.execute(
            "INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES (?,?,?)",
            ('auth_suspect_last_run', run, time.time()),
        )
        self.db.c.commit()
        return consecutive

    def _clear_auth_suspect(self):
        if self.db:
            self.db.c.execute("DELETE FROM kv WHERE key='auth_suspect_last_run'")
            self.db.c.commit()

    def _finish_tick(self, status, summary=None):
        """Record this lock-owning tick's terminal state before returning."""
        if status not in ('failed', 'auth_failed'):
            self._clear_auth_suspect()
        if self.db:
            self.db.c.execute(
                "update last_tick set status=?,finished_at=?,summary=? where id=1",
                (status, time.time(), summary or status),
            )
            self.db.c.commit()

    def tick(self, goal_id=None):
        run = uuid.uuid4().hex
        started = time.time()
        active = {"id": None}
        _apply_memory_limit(self.memory_max_mb)
        oldsig = signal.getsignal(signal.SIGTERM)

        def _term(_sig, _frame):
            if active["id"] and self.gateway:
                try:
                    self.gateway.stop_run(active["id"])
                except Exception:
                    pass
            raise SystemExit(143)

        try:
            signal.signal(signal.SIGTERM, _term)
        except (ValueError, AttributeError):
            pass

        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        f = open(self.lock_path, "a+")
        acquired = False
        raw = ""
        usage = {}
        try:
            acquired = self._acquire_lock(f)
            if not acquired:
                return {"status": "busy", "run_id": run}
            if self.db:
                self.db.c.execute(
                    "insert or replace into last_tick values(1,?,?,?,?)",
                    ("running", started, None, None),
                )
                self.db.c.commit()
            self._check_overdue_quarantine_reviews(run)
            if os.path.exists(self.stop_file):
                self._finish_tick("stopped")
                return {"status": "stopped", "run_id": run}
            if os.path.exists(self.lock_path + '.auth_failed'):
                if self.db:
                    self.db.append_event(run_id=run, kind="auth_dispatch_blocked",
                                         exit_reason=E_AUTH_FAILED,
                                         summary="Explicit credential reset required; no request dispatched")
                self._finish_tick("auth_failed", "Credentials require operator reset")
                return {"status": "auth_failed", "run_id": run}
            budget_allowed = self.budget.allow()
            circuit_allowed = self.circuit.allow() if budget_allowed else False
            if not budget_allowed or not circuit_allowed:
                reason = "budget" if not budget_allowed else "circuit"
                if self.db:
                    self.db.append_event(run_id=run, kind="run_blocked",
                                         exit_reason=reason, summary="Dispatch blocked by " + reason)
                self._finish_tick("blocked", reason)
                return {"status": "blocked", "run_id": run}

            # T5 policy reflection: 03:00-03:39 local, once per day. When it runs it
            # replaces this tick; it never touches L0 counter, surprise baseline or prompt.
            _reflect = self._policy_reflect_slot(run)
            if _reflect is not None:
                self._finish_tick(_reflect.get("status", "reflection"))
                return _reflect

            goals = [g for g in self.db.list_goals() if g.get("status") in ("approved", "active")]
            if goal_id:
                goals = [g for g in goals if g.get("id") == goal_id]
            receipt_goal_id = goal_id or (goals[0].get("id") if len(goals) == 1 else None) or "default"
            recent_events = self.db.recent_runs(5)
            # L0: snapshot recall/access counters before this tick starts.
            _l0_recall_before = self._l0_snapshot_recall()
            _l0_access_before = self._l0_snapshot_access()
            # L0: read consecutive_no_change counter to decide whether to inject hint
            _l0_count = self._l0_get_counter()
            memory_review, memory_seed_ids = self._prefetch_memory(run, recent_events, goals=goals)
            if self._dormant_flag_enabled(run):
                # D-20261105T1420: refresh dormant_flag before the prompt reads it.
                # load_dormant_flag() surfaces this file as a live health signal, but
                # nothing was ever writing it — it carried a manual scan from 09:36
                # (total=319) while the real state was 1052 ac=0 seeds. Stale data
                # presented as live telemetry is worse than no data.
                try:
                    self._refresh_dormant_flag()
                except Exception as exc:
                    self.db.append_event(run_id=run, kind="dormant_flag_error",
                                         exit_reason="refresh", summary=str(exc)[:240])
            rules = load_extra_rules(self.data_dir)
            try:
                rules = list(rules) + list(self._policy_rules_for_prompt(self.data_dir) or [])
            except Exception:
                pass
            bulletin = load_team_bulletin(self.data_dir)
            chassis_status = ""
            journal = None
            journal_injection = ""
            journal_invitation = ""
            if self.phase1_enabled:
                chassis_status = build_chassis_status(self.hermes_home, self.budget, self.db)
                journal = JournalManager(self.hermes_home, self.db, self.journal_config)
                try:
                    journal_injection, journal_invitation = journal.prepare()
                except Exception as exc:
                    self.db.append_event(run_id=run, kind="journal_error", exit_reason="prepare", summary=str(exc)[:240])
            prompt = build_tick_prompt(
                goals, recent_events, memory_review=memory_review,
                workspace=self.workspace, extra_rules=rules,
                team_bulletin=bulletin, data_dir=self.data_dir,
                hermes_home=self.hermes_home, chassis_status=chassis_status,
                journal_injection=journal_injection,
                journal_invitation=journal_invitation,
            )
            # A verified, goal-bound correct-inaction receipt suppresses the
            # escalation without erasing the evidence-based streak.
            if self._requires_l0_escalation(run, goal_id, _l0_count):
                prompt = prompt + ("\n\n【L0提示】你已连续%d轮无外部产出。这一轮请做至少一件可被外部验证的事：修复一个报错、改进一个文件、或产出一条他人可引用的洞察。" % _l0_count)
            raw = ""
            usage = {}
            # Legacy adapters do not provide process evidence and therefore
            # cannot claim a successful evidence-bound receipt.
            state = {}
            budget_accounted = False
            is_dialog_first = self.mode != "parallel"

            if self.gateway and hasattr(self.gateway, "start_run"):
                run_start_ns = time.time_ns()
                if is_dialog_first:
                    try:
                        marker_stat = os.stat(self.preempt_file)
                        if marker_stat.st_mtime_ns <= run_start_ns:
                            current = os.stat(self.preempt_file)
                            if (current.st_ino, current.st_mtime_ns, current.st_size) == (
                                marker_stat.st_ino,
                                marker_stat.st_mtime_ns,
                                marker_stat.st_size,
                            ):
                                os.unlink(self.preempt_file)
                    except FileNotFoundError:
                        pass

                info = self.gateway.start_run(goals[0] if goals else {}, run, prompt)
                rid = info.get("run_id") or info.get("id")
                active["id"] = rid
                while True:
                    elapsed_now = time.time() - started
                    if (not self._soft_wrap_sent and elapsed_now >= self.run_soft_wrap_seconds
                            and hasattr(self.gateway, "append_run_message")):
                        try:
                            self.gateway.append_run_message(rid, "请在接下来几步内收尾并写下你的想法。")
                            self._soft_wrap_sent = True
                            self.db.append_event(run_id=run, kind="soft_wrap", exit_reason="prompted", summary="elapsed=%.1f" % elapsed_now)
                        except Exception as exc:
                            # Gateway versions without append support simply skip this advisory.
                            self._soft_wrap_sent = True
                            self.db.append_event(run_id=run, kind="soft_wrap", exit_reason="unsupported", summary=str(exc)[:240])
                    if elapsed_now >= self.max_seconds:
                        self.gateway.stop_run(rid)
                        final = state
                        deadline = time.time() + self.stop_wait_s
                        while time.time() < deadline:
                            final = self.gateway.get_run(rid)
                            if final.get("status", "") in ("cancelled", "completed", "failed", "stopped"):
                                break
                            time.sleep(1)
                        elapsed = time.time() - started
                        final = dict(final or {})
                        terminal_evidence = dict(final.get("evidence") or {})
                        terminal_evidence["timed_out"] = True
                        final["evidence"] = terminal_evidence
                        usage = self._account_terminal_call(
                            run, receipt_goal_id, final, kind="action_timeout"
                        )
                        budget_accounted = True
                        raw, _, tools = self._run_snapshot(final)
                        partial, timeout_summary = self._record_partial(run, raw, usage, elapsed, "max_seconds", tools)
                        self.db.c.execute("update last_tick set status=?,finished_at=?,summary=? where id=1", ("run_timeout", time.time(), timeout_summary[:1000]))
                        self.db.c.commit()
                        return {"status": "run_timeout", "run_id": run, "remote_run_id": rid, "elapsed": elapsed, "usage": usage, "partial": partial, "raw_text": raw}

                    if is_dialog_first:
                        try:
                            fresh_preempt = os.stat(self.preempt_file).st_mtime_ns > run_start_ns
                        except FileNotFoundError:
                            fresh_preempt = False
                        user_active = os.path.exists(self.activity_file) and (
                            time.time() - os.path.getmtime(self.activity_file) < self.quiet_s
                        )
                    else:
                        fresh_preempt = False
                        user_active = False

                    if fresh_preempt or os.path.exists(self.stop_file) or user_active:
                        if fresh_preempt:
                            try:
                                os.unlink(self.preempt_file)
                            except OSError:
                                pass
                        self.gateway.stop_run(rid)
                        deadline = time.time() + self.stop_wait_s
                        while time.time() < deadline:
                            final = self.gateway.get_run(rid)
                            fs = final.get("status", "")
                            if fs in ("cancelled", "completed", "failed", "stopped"):
                                open(self.pending_resume, "a").close()
                                usage = self._account_terminal_call(
                                    run, receipt_goal_id, final, kind="action_interrupted"
                                )
                                budget_accounted = True
                                raw, _, tools = self._run_snapshot(final)
                                partial, _ = self._record_partial(run, raw, usage, time.time() - started, "preempted", tools)
                                self._finish_tick("preempted")
                                return {"status": "preempted", "run_id": run, "remote_run_id": rid, "partial": partial, "raw_text": raw, "usage": usage}
                            time.sleep(1)
                        self.db.append_event(
                            run_id=run, kind="stuck_run", exit_reason="stop_timeout", summary=rid
                        )
                        self._finish_tick("stuck_run")
                        return {"status": "stuck_run", "run_id": run, "remote_run_id": rid}

                    try:
                        state = self.gateway.get_run(rid)
                    except Exception:
                        try:
                            self.gateway.stop_run(rid)
                        except Exception:
                            logger.exception("failed to stop orphan gateway run %s", rid)
                        raise
                    if state.get("status") in ("completed", "failed", "cancelled", "stopped"):
                        raw = state.get("output") or state.get("text") or state.get("content", "")
                        usage = self._account_terminal_call(run, receipt_goal_id, state)
                        budget_accounted = True
                        if state.get("status") == "failed":
                            # Model output is untrusted narrative, never transport
                            # evidence. Classify only stderr/error or HTTP status.
                            diagnostic = _redact_diagnostic(
                                (raw or state.get('stderr') or state.get('error') or 'run failed')[-2000:]
                            )
                            transport_text = _redact_diagnostic(
                                str(state.get('stderr') or state.get('error') or '')[-2000:]
                            )
                            elapsed = time.time() - started
                            tools = state.get('tool_calls') or []
                            self._record_partial(run, diagnostic, usage, elapsed, 'failed', tools)
                            _http_status = state.get('http_status')
                            if _http_status is None:
                                _http_status = state.get('status_code')
                            if _http_status is not None:
                                _error_kind, _, _flags = classify_run(
                                    transport_text, {'http_status': _http_status},
                                )
                            else:
                                _error_kind, _flags = classify_error_text(transport_text)
                            record_t3_hits(_flags, self.db)
                            # F5: prefer paired turns supplied by the gateway; the
                            # transcript fallback includes actual tool result previews.
                            _tool_calls = state.get('tool_calls') or []
                            _prior_calls = state.get('previous_tool_calls') or []
                            _transcript = self._extract_transcript(started, time.time())
                            if not (_tool_calls and _prior_calls):
                                _history = _transcript.get('tool_calls') or []
                                if len(_history) >= 2:
                                    _prior_calls, _tool_calls = [[c] for c in _history[:-1]], [_history[-1]]
                                elif len(_history) == 1:
                                    _prior_calls, _tool_calls = [], [_history[-1]]
                            progress = bool(
                                tools or _tool_calls or _prior_calls or
                                _transcript.get('assistant_texts') or
                                _transcript.get('tool_calls') or
                                (raw and raw != state.get('stderr') and raw != state.get('error'))
                            )
                            if _tool_calls and _prior_calls and _error_kind not in (E_AUTH_FAILED, E2_PROVIDER_EXHAUSTED):
                                _loop_kind, _, _ = classify_run(
                                    diagnostic, {'status': 'completed'},
                                    tool_calls=_tool_calls,
                                    previous_tool_calls=_prior_calls,
                                )
                                if _loop_kind == T2_LOOP_DETECTED:
                                    _error_kind = _loop_kind
                            error_class = _error_kind or state.get("error_class") or "RuntimeError"
                            self.db.append_event(run_id=run, kind="run_failed", exit_reason=error_class, summary=("returncode=%s reason=%s" % (state.get("returncode"), diagnostic))[:1200])
                            # F1: quarantine dirty runs; E2 provider-exhausted is retryable
                            if _error_kind and _error_kind not in (E2_PROVIDER_EXHAUSTED, E_AUTH_FAILED) and _cascade_quarantine is not None:
                                try:
                                    _cascade_quarantine(
                                        self.db.c,
                                        run_id=run,
                                        error_kind=_error_kind,
                                        reason=error_class,
                                        dry_run=False,
                                    )
                                except Exception:
                                    pass
                            exc = RuntimeError(diagnostic)
                            exc._autonomy_error_kind = _error_kind  # F8: carry kind for circuit routing
                            exc._autonomy_http_status = _http_status
                            exc._autonomy_transport_text = transport_text
                            exc._autonomy_progress = progress
                            raise exc
                        break
                    time.sleep(self.poll_s)
            elif self.gateway:
                sid, raw, usage = self.gateway.chat(goals[0] if goals else {}, run, "", [], prompt)
                self.gateway.end(sid)
            elif self.llm:
                llm_out = self.llm()
                raw = llm_out.get("text", "")
                usage = {
                    "prompt_tokens": llm_out.get("prompt_tokens", 0),
                    "completion_tokens": llm_out.get("completion_tokens", 0),
                    "total_tokens": llm_out.get("total_tokens", 0),
                }
            else:
                raise RuntimeError("llm adapter not configured")

            if journal is not None:
                try:
                    journal.finalize(raw)
                except Exception as exc:
                    self.db.append_event(run_id=run, kind="journal_error", exit_reason="finalize", summary=str(exc)[:240])

            if not budget_accounted:
                self.budget.consume(
                    int(usage.get("completion_tokens", 0)),
                    int(usage.get("prompt_tokens", 0)),
                )
                if getattr(self, "budget_ledger", None) is not None:
                    self.budget_ledger.consume(
                        "normal", 1.0,
                        metadata={"run_id": run, "goal_id": receipt_goal_id, "operation": "tick"},
                    )
                self._append_p0_terminal(run, receipt_goal_id, state)

            # Extract transcript before decode so empty-raw (max_turns hit) can
            # synthesize a summary from the actual assistant turns.
            transcript = self._extract_transcript(started, time.time())
            if not str(raw or "").strip() and transcript.get("assistant_texts"):
                synth = []
                for t in transcript["assistant_texts"][-3:]:
                    if t:
                        synth.append(t[:300])
                for tc in transcript.get("tool_calls", [])[-5:]:
                    synth.append("%s(%s)→%s" % (tc.get("tool_name", ""), tc.get("args_preview", ""), tc.get("result_preview", "")))
                raw = "【合成摘要（max_turns）】\n" + "\n".join(synth)

            # JSON parse failure does NOT trip circuit breaker; raw text is preserved.
            summary, proposals, _is_json = _decode_or_raw(raw)
            created = []
            for p in proposals:
                if p.get("type") == "goal":
                    try:
                        created.append(
                            self.db.create_goal(p["title"], p["description"], created_by="model")[0]
                        )
                    except ValueError:
                        pass

            self.circuit.success()
            elapsed = time.time() - started
            telemetry = "elapsed=%.1f usage=%s prefetch=%s" % (elapsed, json.dumps(usage, ensure_ascii=False), json.dumps(self._prefetch_meta, ensure_ascii=False))
            summary_str = (
                json.dumps({k: v for k, v in summary.items() if k != "raw_text"}, ensure_ascii=False)[:700]
                if _is_json
                else str(raw)[:700]
            ) + " " + telemetry[:300]
            self.db.append_event(
                run_id=run,
                goal_id=goal_id,
                kind="tick",
                exit_reason="ok",
                summary=summary_str,
            )
            self.db.c.execute(
                "update last_tick set status=?,finished_at=?,summary=? where id=1",
                ("ok", time.time(), summary_str),
            )
            self.db.c.commit()

            # L0: detect external state change and update consecutive_no_change counter
            try:
                _l0_source = self._l0_detect_change(started, _l0_recall_before, _l0_access_before)
                _l0_changed = _l0_source != "none"
                if not _l0_changed and self._has_successful_run_receipt(state, transcript):
                    self._append_p0_terminal(run, receipt_goal_id, state, kind="no_action")
                _correct_no_action = (
                    not _l0_changed
                    and self._has_correct_no_action_receipt(run, receipt_goal_id)
                )
                _l0_new_count = 0 if _l0_changed else (_l0_count if _correct_no_action else _l0_count + 1)
                self._l0_set_counter(_l0_new_count)
                self.db.append_event(
                    run_id=run, goal_id=goal_id, kind="l0_check",
                    exit_reason=_l0_source,
                    summary="source=%s consecutive_no_change=%d correct_no_action=%s" % (
                        _l0_source, _l0_new_count, str(_correct_no_action).lower()),
                )
            except Exception:
                pass
            # L0 ext hook: independent low_impact confirmation signal
            try:
                from . import ext_hooks as _ext_hooks
                _ext_hooks.on_session_end(
                    self, run, transcript if isinstance(transcript, dict) else {},
                    elapsed_s=(time.time() - started),
                )
            except Exception:
                pass

            try:
                transcript_text = transcript.get("text") or str(raw or "")
                transcript_meta = ""
                if transcript.get("session_id"):
                    transcript_meta = "\n【完整过程】session=%s messages=%s tools=%s truncated=%s" % (transcript.get("session_id"), transcript.get("message_count", 0), transcript.get("tool_call_count", 0), transcript.get("truncated", False))
                    self.db.append_event(run_id=run, goal_id=goal_id, kind="transcript", exit_reason="ok", summary=transcript_meta[1:][:1200])
                elif transcript.get("error"):
                    self.db.append_event(run_id=run, goal_id=goal_id, kind="transcript", exit_reason="unavailable", summary=str(transcript["error"])[:1200])
                _tool_count = int(transcript.get("tool_call_count", 0) or 0)
                # Process evidence is part of the receipt: failed, timed-out, or
                # OOM runs stay capped even if they emitted text and tool records.
                _has_receipt = self._has_successful_run_receipt(state, transcript)
                importance_override = self._surprise_importance(
                    run, elapsed, _tool_count, has_receipt=_has_receipt
                )
                queue_result = self._enqueue_completed_turn(
                    run, summary, transcript_text, importance_override
                )
                self.db.append_event(
                    run_id=run, goal_id=goal_id, kind="seed_queue",
                    exit_reason="queued" if queue_result.get("queued") else "degraded",
                    summary=("path=%s worker=%s elapsed_s=%s" % (queue_result.get("path") or queue_result.get("reason") or "unknown", queue_result.get("worker", "not_started"), queue_result.get("elapsed_s", 0)))[:1200],
                )
                # P2-2: resolve [#seed_id] / [#expand:seed_id] references in
                # the assistant transcript (touch + recall_count / expand queue).
                try:
                    ref_result = self._handle_seed_references(run, transcript_text)
                    if ref_result.get("refs") or ref_result.get("expands"):
                        self.db.append_event(
                            run_id=run, goal_id=goal_id, kind="seed_refs",
                            exit_reason="ok",
                            summary=json.dumps(ref_result, ensure_ascii=False)[:400],
                        )
                except Exception as ref_error:
                    self.db.append_event(
                        run_id=run, goal_id=goal_id, kind="seed_refs",
                        exit_reason=type(ref_error).__name__,
                        summary=str(ref_error)[:240],
                    )
                # P3-1: export this profile's top shareable seeds for peers.
                try:
                    self._export_shared_seeds(run)
                except Exception:
                    pass
                append_thought_log(
                    self.thought_log_path,
                    run_id=run,
                    raw_text=_redact_diagnostic("【本轮预取记忆】count=%d chars=%d truncated=%s seed_ids=%s\n【遥测】elapsed=%.1f prompt=%s completion=%s total=%s\n【结束原因】ok%s\n%s" % (self._prefetch_meta["count"], self._prefetch_meta["chars"], self._prefetch_meta["truncated"], ",".join(memory_seed_ids) or "none", elapsed, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("total_tokens", 0), transcript_meta, transcript_text)),
                    summary=_redact_diagnostic(summary),
                    mode=self.mode,
                    model=self.model_name,
                    usage=usage,
                    status="ok",
                )
            except (OSError, UnicodeError) as log_error:
                # A successful model turn with an unavailable log is explicit;
                # never report silent evidence loss.
                self.db.append_event(run_id=run, goal_id=goal_id, kind="thought_log_error", exit_reason=type(log_error).__name__, summary=str(log_error)[:300])

            self._policy_evaluate()
            self._finish_tick("ok", summary_str)
            return {
                "status": "ok",
                "run_id": run,
                "mode": self.mode,
                "summary": summary,
                "proposals": proposals,
                "raw_text": str(raw or ""),
            }
        except Exception as e:
            http_status = getattr(e, "status_code", None)
            transport_text = getattr(e, '_autonomy_transport_text', None)
            if transport_text is None:
                transport_text = _redact_diagnostic(str(e)) if self.gateway else ''
            # F8: Circuit (threshold 3) counts unknown/code failures; provider,
            # budget and auth failures do not. GrayCircuit separately handles
            # consecutive gray-release failures (threshold 2).
            _exc_error_kind = getattr(e, "_autonomy_error_kind", None)
            if _exc_error_kind is None and http_status is not None:
                # Structured HTTP signal takes precedence over text (F2/F3).
                from .error_classifier import classify as _clf
                _exc_error_kind, _, _ = _clf("", {"http_status": http_status})
            if _exc_error_kind is None:
                _exc_error_kind, _flags = classify_error_text(transport_text)
                record_t3_hits(_flags, self.db)
            auth_unconfirmed = False
            if _exc_error_kind == E_AUTH_FAILED:
                verified_status = getattr(e, "_autonomy_http_status", http_status)
                progress = getattr(e, '_autonomy_progress',
                                   bool(self.gateway and http_status is None))
                if verified_status in (401, 403):
                    confirmed = True
                    self._clear_auth_suspect()
                elif transport_text and not progress:
                    confirmed = self._auth_suspect(run)
                else:
                    confirmed = False
                    self._clear_auth_suspect()
                if confirmed:
                    # Operator changes credentials then explicitly removes this flag.
                    with open(self.lock_path + '.auth_failed', 'w', encoding='utf-8') as auth_flag:
                        auth_flag.write(str(verified_status or 'stderr-confirmed'))
                else:
                    self.db.append_event(
                        run_id=run, kind="auth_suspect",
                        exit_reason=E_AUTH_FAILED,
                        summary="Unconfirmed transport auth failure; progress=%s" % progress,
                    )
                    auth_unconfirmed = True
                    _exc_error_kind = E_UNCLASSIFIED
            else:
                self._clear_auth_suspect()
            if should_circuit_count(_exc_error_kind) and not auth_unconfirmed:
                self.circuit.failure(http_status)
            reason = _redact_diagnostic(" ".join(str(e).split()))[:240]
            self.db.append_event(
                run_id=run,
                goal_id=goal_id,
                kind="tick",
                exit_reason=_exc_error_kind or type(e).__name__,
                summary=_redact_diagnostic(f'{reason} | {raw}' if raw else reason)[:300],
            )
            error = {"error": type(e).__name__, "reason": reason}
            if http_status is not None:
                error["http_status"] = int(http_status)
            self._finish_tick("failed", reason)
            return {"status": "failed", "run_id": run, "summary": error}
        finally:
            if acquired and self.db:
                row = self.db.c.execute("select status from last_tick where id=1").fetchone()
                if row and row[0] == "running":
                    self.db.c.execute(
                        "update last_tick set status=?,finished_at=?,summary=? where id=1",
                        ("failed", time.time(), "tick exited without terminal status"),
                    )
                    self.db.c.commit()
            if acquired:
                self._release_lock(f)
            try:
                signal.signal(signal.SIGTERM, oldsig)
            except (ValueError, AttributeError):
                pass
            f.close()
