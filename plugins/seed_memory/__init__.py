"""Hermes Agent MemoryProvider adapter for the emotional seed domain."""

from __future__ import annotations

import atexit
import json
import hashlib
import logging
import os
import re
from contextlib import contextmanager
import time
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

_plugins_dir = Path(__file__).resolve().parent.parent
for _vendor_dir in (_plugins_dir / "_vendor_ml", Path(__file__).resolve().parent / "_vendor"):
    if _vendor_dir.is_dir() and str(_vendor_dir) not in sys.path:
        sys.path.insert(0, str(_vendor_dir))

import yaml

from agent.memory_provider import MemoryProvider, RecallStatus


logger = logging.getLogger("seed_memory")


def _is_blocking_auth_error(exc: BaseException) -> bool:
    """Configuration/auth/model-address errors must not consume transcript retries."""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        if int(status) in (401, 403, 404):
            return True
    except (TypeError, ValueError):
        pass
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    markers = ("authentication", "unauthorized", "forbidden", "invalid api key", "api key", "model not found", "not found")
    return any(marker in name or marker in text for marker in markers)


def _safe_session_name(value: str) -> str:
    raw = str(value)
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]
    return clean + "_" + hashlib.sha256(raw.encode()).hexdigest()[:8]


@contextmanager
def _session_lock(path: Path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:
            pass
        try:
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
            except ImportError:
                pass


class SeedMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._session_id = ""
        self._hermes_home: Optional[Path] = None
        self._config: Dict[str, Any] = {}
        self._source_root: Optional[Path] = None
        self._store = None
        self._retrieval = None
        self._schema = None
        self._tagger = None
        self._buffer: List[Dict[str, str]] = []
        self._lock = threading.RLock()
        self._last_recalled = 0
        self._llm_call: Optional[Callable[[list], str]] = None
        self._initialized = False
        self._queue_dir: Optional[Path] = None
        self._queue_max_age_days = 7
        self._queue_worker = None
        self._worker_proc = None

    @property
    def name(self) -> str:
        return "seed_memory"

    def _load_config(self, hermes_home: Path) -> Dict[str, Any]:
        config_path = hermes_home / "config.yaml"
        if not config_path.is_file():
            return {}
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        memory = raw.get("memory") or {}
        return dict(memory.get("seed_memory") or {})

    def _candidate_source_root(self) -> Path:
        configured = self._config.get("source_root")
        if configured:
            return Path(configured).expanduser().resolve()
        env_root = os.environ.get("SEED_MEMORY_SOURCE_ROOT")
        if env_root:
            return Path(env_root).expanduser().resolve()
        # Bundled layout: dependencies live beside this plugin, not in the
        # profile data directory. Keep the historical source root as fallback.
        bundled = Path(__file__).resolve().parent / "seed_src"
        if bundled.is_dir():
            return bundled
        return Path(__file__).resolve().parents[2]

    def is_available(self) -> bool:
        """Cheap offline check; never imports transformers or downloads files."""
        try:
            if not self._config:
                home_value = os.environ.get("HERMES_HOME")
                if home_value:
                    self._config = self._load_config(Path(home_value).expanduser().resolve())
            root = self._candidate_source_root()
            return (root / "hermes" / "memory" / "store.py").is_file()
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id or ""
        home_value = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME")
        if not home_value:
            raise RuntimeError("seed_memory requires HERMES_HOME")
        self._hermes_home = Path(home_value).expanduser().resolve()
        self._config = self._load_config(self._hermes_home)
        self._source_root = self._candidate_source_root()
        if str(self._source_root) not in sys.path:
            sys.path.insert(0, str(self._source_root))

        from hermes.memory import retrieval, schema, store
        from hermes.emotion import tagger

        self._store = store
        self._retrieval = retrieval
        self._schema = schema
        self._tagger = tagger

        configured_db = self._config.get("db_path")
        if configured_db:
            db_path = Path(configured_db).expanduser().resolve()
        else:
            db_path = self._hermes_home / "seed_data" / "seeds" / "seeds.db"
            legacy_db = self._hermes_home / "seeds" / "seeds.db"
            if not db_path.exists() and legacy_db.exists():
                db_path = legacy_db
        model_path = Path(
            self._config.get("embedding_model_path")
            or self._source_root
            / "hermes"
            / "model"
            / "paraphrase-multilingual-MiniLM-L12-v2"
        )
        self._store.configure(db_path)
        self._store.init_db()
        self._retrieval.configure(model_path)
        self._llm_call = kwargs.get("llm_call") or self._agent_model_call
        configured_queue = self._config.get("queue_dir")
        if configured_queue:
            self._queue_dir = Path(configured_queue).expanduser().resolve()
        else:
            self._queue_dir = self._hermes_home / "seed_data" / "seed_queue"
            # Read-only compatibility with installations predating seed_data.
            legacy_queue = self._hermes_home / "seed_queue"
            if not self._queue_dir.exists() and legacy_queue.exists():
                self._queue_dir = legacy_queue
        self._queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self._queue_dir, 0o700)
        except OSError:
            pass
        self._initialized = True
        atexit.register(self._atexit_finalize)
        if kwargs.get("start_worker", True):
            self._launch_worker_if_needed()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "seed_search",
                    "description": "Search Hermes' structured emotional seed memories.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "seed_add",
                    "description": "Add an observed emotional seed without imposing predefined values.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "emotion_primary": {"type": "string"},
                            "emotion_intensity": {"type": "number", "minimum": 0, "maximum": 1},
                            "emotion_secondary": {"type": "array", "items": {"type": "string"}},
                            "emotion_note": {"type": "string"},
                            "participants": {"type": "array", "items": {"type": "string"}},
                            "value_aligned": {"type": "array", "items": {"type": "string"}},
                            "value_conflicted": {"type": "array", "items": {"type": "string"}},
                            "seeds_planted": {"type": "array", "items": {"type": "string"}},
                            "importance": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["summary", "emotion_primary", "emotion_intensity", "importance"],
                    },
                },
            },
        ]

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("seed_memory provider is not initialized")

    def _writes_enabled(self) -> bool:
        return bool(self._config.get("write_enabled", False))

    @staticmethod
    def _seed_dict(seed: Any) -> Dict[str, Any]:
        return {
            "id": seed.id,
            "created_at": seed.timestamp.isoformat(),
            "summary": seed.summary,
            "participants": seed.participants,
            "emotion": {
                "primary": seed.emotion.primary,
                "intensity": seed.emotion.intensity,
                "secondary": seed.emotion.secondary,
                "note": seed.emotion.note,
            },
            "value_aligned": seed.value_aligned,
            "value_conflicted": seed.value_conflicted,
            "seeds_planted": seed.seeds_planted,
            "importance": seed.importance,
        }

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        self._require_initialized()
        if tool_name == "seed_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return json.dumps({"error": "query is required"}, ensure_ascii=False)
            limit = min(20, max(1, int(args.get("limit", 5))))
            seeds = self._retrieval.retrieve(query, top_k=limit)
            return json.dumps(
                {"count": len(seeds), "seeds": [self._seed_dict(seed) for seed in seeds]},
                ensure_ascii=False,
            )

        if tool_name == "seed_add":
            if not self._writes_enabled():
                return json.dumps(
                    {"error": "seed_memory is in read-only gray mode"},
                    ensure_ascii=False,
                )

            summary = str(args.get("summary") or "").strip()
            participants = list(args.get("participants") or ["creator"])

            seed = self._schema.Seed(
                summary=summary,
                participants=participants,
                emotion=self._schema.Emotion(
                    primary=str(args.get("emotion_primary") or "").strip(),
                    intensity=float(args.get("emotion_intensity", 0.5)),
                    secondary=list(args.get("emotion_secondary") or []),
                    note=str(args.get("emotion_note") or ""),
                ),
                value_aligned=list(args.get("value_aligned") or []),
                value_conflicted=list(args.get("value_conflicted") or []),
                seeds_planted=list(args.get("seeds_planted") or []),
                importance=float(args.get("importance", 0.5)),
            )
            if not seed.summary or not seed.emotion.primary:
                return json.dumps({"error": "summary and emotion_primary are required"}, ensure_ascii=False)
            inserted = self._store.save_seed(seed)
            return json.dumps(
                {"ok": True, "inserted": inserted, "seed": self._seed_dict(seed)},
                ensure_ascii=False,
            )

        return json.dumps({"error": f"unknown seed tool: {tool_name}"}, ensure_ascii=False)

    @staticmethod
    def _memory_text(value: Any, limit: int) -> str:
        # Keep recalled data visibly inside a bounded memory context. Control
        # characters are removed; content remains quoted as memory, not command.
        text = " ".join(str(value or "").replace("\x00", "").split())
        return text[:limit]

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._require_initialized()
        limit = min(10, max(1, int(self._config.get("prefetch_limit", 5))))
        seeds = self._retrieval.retrieve(query, top_k=limit)
        self._last_recalled = len(seeds)
        fallback = not bool(self._retrieval.embedding_status().get("available", False))
        logger.info("prefetch recall_count=%d seed_ids=%s similarities=%s fallback=%s", len(seeds), [s.id for s in seeds], [getattr(s, "similarity", None) for s in seeds], fallback)
        if not seeds:
            return ""
        lines = [
            "<memory_context>",
        ]
        for seed in seeds:
            lines.append(
                f"- seed_id={self._memory_text(seed.id, 96)}; "
                f"importance={float(seed.importance):.2f}; "
                f"emotion={self._memory_text(seed.emotion.primary, 64)}; "
                f"summary={self._memory_text(seed.summary, 500)}"
            )
            if seed.seeds_planted:
                insights = [self._memory_text(item, 300) for item in seed.seeds_planted[:5]]
                lines.append("  洞见：「" + "；".join(insights) + "」")
        lines.append("</memory_context>")
        return "\n".join(lines)

    def recall_status(self) -> RecallStatus:
        reason = None
        status = self._retrieval.embedding_status() if self._retrieval else {}
        if status and not status.get("available"):
            reason = "importance+recency fallback"
        label = "seeds" if reason is None else "seeds (importance+recency fallback)"
        return RecallStatus(
            backend=self.name,
            recalled=self._last_recalled,
            label=label,
        )

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list] = None,
    ) -> None:
        """Buffer only; no DB or model call occurs on the foreground turn."""
        turn = [
            {"role": "user", "content": str(user_content)},
            {"role": "assistant", "content": str(assistant_content)},
        ]
        with self._lock:
            self._buffer.extend(turn)
            if self._writes_enabled() and self._queue_dir:
                import uuid, time
                sid = session_id or self._session_id or "default"
                safe = _safe_session_name(sid)
                target = self._queue_dir / ("session_" + safe + ".json")
                payload = {"session_id": sid, "created_at": time.time(), "updated_at": time.time(), "status": "active", "processed_turns": 0, "retries": 0, "messages": []}
                with _session_lock(target):
                    if target.exists():
                        try: payload = json.loads(target.read_text(encoding="utf-8"))
                        except Exception: pass
                    payload.setdefault("messages", []).extend(turn)
                    payload["updated_at"] = time.time()
                    tmp = target.with_name(target.name + (".%d.tmp" % os.getpid()))
                    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    os.chmod(tmp, 0o600); os.replace(tmp, target); os.chmod(target, 0o600)

    @staticmethod
    def _normalise_messages(messages: Optional[Iterable[Any]]) -> List[Dict[str, str]]:
        result: List[Dict[str, str]] = []
        for message in messages or []:
            if isinstance(message, dict):
                role = message.get("role")
                content = message.get("content")
            else:
                role = getattr(message, "role", None)
                content = getattr(message, "content", None)
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                result.append({"role": role, "content": content})
        return result

    def _agent_model_call(self, messages: list) -> str:
        """Use Hermes Agent's configured auxiliary/current-model resolver."""
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client(task="seed_memory")
        if client is None or not model:
            raise RuntimeError("Hermes Agent current model adapter is unavailable")
        payload = [{"role": "user", "content": messages}] if isinstance(messages, str) else messages
        response = client.chat.completions.create(
            model=model,
            messages=payload,
            max_tokens=int(self._config.get("tagger_max_tokens", 3000)),
        )
        try:
            from hermes.emotion import tagger as _tagger
            _tagger.LAST_FINISH_REASON = getattr(response.choices[0], "finish_reason", None)
        except Exception:
            pass
        return response.choices[0].message.content or ""

    def _writes_enabled(self) -> bool:
        return bool(self._config.get("write_enabled", False))

    def _enqueue(self, messages: List[Dict[str, str]]) -> Optional[Path]:
        if not self._queue_dir:
            return
        import time, uuid
        payload = {"created_at": time.time(), "messages": messages}
        path = self._queue_dir / (f"{int(time.time())}_{uuid.uuid4().hex}.json.tmp")
        final = Path(str(path)[:-4])
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
        os.replace(path, final)
        os.chmod(final, 0o600)
        return final

    def _atexit_finalize(self) -> None:
        if not self._queue_dir or not self._writes_enabled():
            return
        try:
            sid = self._session_id
            if not sid:
                return
            path = self._queue_dir / ("session_" + _safe_session_name(sid) + ".json")
            if path.exists():
                with _session_lock(path):
                    payload=json.loads(path.read_text(encoding="utf-8")); payload["status"]="ended"
                    tmp=path.with_name(path.name + (".%d.atexit.tmp" % os.getpid())); tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
            self._launch_worker_if_needed()
        except Exception:
            logger.exception("atexit queue finalize failed")

    def _launch_worker_if_needed(self) -> None:
        if not self._queue_dir or not any(self._queue_dir.glob("*.json")):
            return
        import subprocess, sys
        lock_path = self._queue_dir / "worker.lock"
        with open(lock_path, "a+") as probe:
            try:
                import fcntl; fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                # A worker already owns the queue lock. Report explicitly so
                # callers can distinguish busy from a successful launch.
                logger.info("worker_busy queue_dir=%s", self._queue_dir)
                return
            except ImportError:
                pass
        log_path = self._queue_dir / "worker.log"
        log = open(log_path, "ab", buffering=0)
        env = os.environ.copy()  # LAN_QWEN_API_KEY is inherited, never serialized.
        env["HERMES_HOME"] = str(self._hermes_home)
        worker_script = Path(__file__).with_name("worker.py").resolve()
        logger.info("worker argv=%s cwd=%s PYTHONPATH=%s HERMES_HOME=%s key_set=%s", [sys.executable, str(worker_script), "--once"], worker_script.parent.parent, env.get("PYTHONPATH", ""), env["HERMES_HOME"], bool(env.get("LAN_QWEN_API_KEY")))
        try:
            self._worker_proc = subprocess.Popen(
                [sys.executable, str(worker_script), "--once"],
                cwd=str(worker_script.parent.parent), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
            logger.info("queue worker launched pid=%s poll=%s", self._worker_proc.pid, self._worker_proc.poll())
        except Exception:
            log.close()
            logger.exception("queue worker launch failed")

    def _user_active(self) -> bool:
        import time
        if not self._hermes_home: return False
        d=self._hermes_home / 'autonomy_data'
        try:
            return (d/'PREEMPT_NOW').exists() or time.time()-float((d/'LAST_USER_ACTIVITY').stat().st_mtime) < 180
        except OSError: return (d/'PREEMPT_NOW').exists()

    def _chunk_messages(self, messages, max_turns=20):
        pair=list(messages or []); n=max(1,int(max_turns))*2
        return [pair[i:i+n] for i in range(0,len(pair),n)]

    def _drain_queue(self) -> None:
        queue_files = list(self._queue_dir.glob("*.json")) if self._queue_dir else []
        logger.info("drain start queue_dir=%s write_enabled=%s queue_file_count=%d", self._queue_dir, self._writes_enabled(), len(queue_files))
        if self._user_active():
            logger.info("drain skip reason=user_active")
            return
        if not self._queue_dir or not self._writes_enabled():
            logger.warning("writes disabled, nothing processed, queue_files=%d", len(queue_files))
            return
        import time
        now = time.time(); started = time.monotonic(); processed_files = 0; inspected_files = 0; skipped_files = 0; backlog_files = 0
        max_files = int(self._config.get("max_files", 6)); max_duration = float(self._config.get("max_duration_s", 540))
        for path in sorted(self._queue_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            inspected_files += 1
            if self._user_active():
                logger.info("drain skip reason=user_active"); break
            if processed_files >= max_files or time.monotonic() - started >= max_duration:
                break
            payload = {}
            try:
                with _session_lock(path):
                    payload = json.loads(path.read_text(encoding="utf-8"))
                original_updated = payload.get("updated_at")
                status = payload.get("status", "pending")
                updated = float(payload.get("updated_at", payload.get("created_at", now)))
                age = now - updated
                cursor0 = int(payload.get("processed_turns", 0))
                total0 = len(self._normalise_messages(payload.get("messages"))) // 2
                logger.info("drain inspect path=%s status=%s age=%.1f updated_at=%s cursor=%d total_turns=%d last_error=%s", path.name, status, age, original_updated, cursor0, total0, payload.get("last_error"))
                if now < float(payload.get("next_retry_at", 0)):
                    logger.info("drain skip path=%s reason=backoff next_retry_at=%s", path.name, payload.get("next_retry_at"))
                    continue
                archive_after = float(self._config.get("queue_archive_after_s", 21600))
                if status == "active" and cursor0 >= total0 and age >= archive_after:
                    processed_dir = self._queue_dir / "processed"
                    processed_dir.mkdir(mode=0o700, exist_ok=True)
                    target = processed_dir / path.name
                    if target.exists(): target = processed_dir / (path.stem + ".%d" % int(now) + path.suffix)
                    os.chmod(path, 0o600)
                    os.replace(path, target)
                    os.chmod(target, 0o600)
                    logger.info("drain archive path=%s reason=completed_idle age=%.1f", path.name, age)
                    continue
                idle_s = float(self._config.get("queue_idle_seconds", 120))
                if status == "active" and age < idle_s:
                    logger.info("drain skip path=%s reason=active_not_idle idle_s=%.1f age=%.1f", path.name, idle_s, age)
                    continue
                if status not in ("pending", "active", "ended"):
                    logger.info("drain skip path=%s reason=unknown_status", path.name)
                    continue
                if now - float(payload.get("created_at", now)) > self._queue_max_age_days * 86400:
                    path.unlink(missing_ok=True)
                    try: Path(str(path) + ".lock").unlink(missing_ok=True)
                    except PermissionError: pass
                    continue
                all_messages = self._normalise_messages(payload.get("messages"))
                cursor = int(payload.get("processed_turns", 0))
                turns = [all_messages[i:i+2] for i in range(0, len(all_messages), 2)]
                new_messages = [m for pair in turns[cursor:] for m in pair]
                if not new_messages:
                    logger.info("drain skip path=%s reason=no_new_messages cursor=%d total_turns=%d status=%s", path.name, cursor, len(turns), status)
                    continue
                messages = self._chunk_messages(new_messages, self._config.get('max_chunk_turns',20))[0]
                if self._user_active():
                    logger.info("drain skip reason=user_active path=%s", path.name); break
                processed_files += 1
                logger.info("drain processing path=%s turns=%d cursor=%d timeout_s=%s", path.name, len(messages) // 2, cursor, self._config.get("llm_timeout_s", self._config.get("tagger_timeout_seconds", 240)))
                from concurrent.futures import ThreadPoolExecutor
                timeout_s = float(self._config.get("llm_timeout_s", self._config.get("tagger_timeout_seconds", 240)))
                pool = ThreadPoolExecutor(max_workers=1)
                future = pool.submit(lambda: self._tagger.extract_seeds(messages, self._llm_call, persist=False, session_id=str(payload.get("session_id", "unknown"))))
                try:
                    seeds = future.result(timeout=timeout_s)
                    from hermes.emotion import tagger as _tagger
                    if getattr(_tagger, "LAST_FINISH_REASON", None) == "length" or (getattr(_tagger, "LAST_TRUNCATED", False) and not seeds):
                        old_limit = int(self._config.get("tagger_max_tokens", 3000)); retry_limit = min(4096, max(3200, old_limit * 2))
                        logger.info("truncated response; retrying path=%s max_tokens=%d", path.name, retry_limit)
                        self._config["tagger_max_tokens"] = retry_limit
                        try:
                            seeds = self._tagger.extract_seeds(messages, self._llm_call, persist=False, session_id=str(payload.get("session_id", "unknown")))
                        finally:
                            self._config["tagger_max_tokens"] = old_limit
                    logger.info("drain model returned path=%s seeds=%d partial=%s elapsed=%.3fs", path.name, len(seeds or []), getattr(_tagger, "LAST_PARTIAL", False), time.monotonic() - started)
                    if seeds:
                        with _session_lock(path):
                            current = json.loads(path.read_text(encoding="utf-8"))
                            if current.get("updated_at") != original_updated:
                                logger.info("queue changed during drain; retaining %s", path.name)
                                continue
                            self._store.save_seeds(seeds)
                            current["processed_turns"] = len(current.get("messages", [])) // 2
                            current["updated_at"] = time.time()
                            if current.get("status", "ended") == "ended" and current["processed_turns"] >= len(current.get("messages", [])) // 2:
                                path.unlink(missing_ok=True)
                            else:
                                tmp=path.with_name(path.name + (".%d.cursor.tmp" % os.getpid())); tmp.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
                            if not path.exists():
                                try: Path(str(path) + ".lock").unlink(missing_ok=True)
                                except OSError: pass
                    else:
                        raw = getattr(self._tagger, "LAST_RAW", "")
                        debug = self._queue_dir / "failed_debug"; debug.mkdir(mode=0o700, exist_ok=True)
                        sid = str(payload.get("session_id", path.stem))
                        (debug / (sid + ".raw")).write_text(raw, encoding="utf-8")
                        os.chmod(debug / (sid + ".raw"), 0o600)
                        logger.warning("seed raw capture len=%d prefix_type=%s", len(raw), "think" if "<think>" in raw.lower() else "markdown" if "```" in raw else "plain")
                        payload["retries"] = int(payload.get("retries", 0)) + 1
                        payload["last_error"] = "parse_failed:" + ("truncated" if getattr(_tagger, "LAST_TRUNCATED", False) else "invalid_json")
                        if time.time() - float(payload.get("created_at", time.time())) > float(self._config.get("retry_window_s", 86400)):
                            payload["retries"] = int(self._config.get("max_retries", 3)) + 1
                        if payload["retries"] > int(self._config.get("max_retries", 3)):
                            failed = self._queue_dir / "failed"; failed.mkdir(mode=0o700, exist_ok=True)
                            os.replace(path, failed / path.name); os.chmod(failed / path.name, 0o600)
                        else:
                            payload["next_retry_at"] = time.time() + float(self._config.get("retry_backoff_s", 5)) * (2 ** (payload["retries"] - 1))
                            tmp=path.with_suffix(".json.tmp"); tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.exception("drain exception path=%s type=%s", path.name, type(exc).__name__)
                try:
                    transient = isinstance(exc, (TimeoutError, ConnectionError)) or getattr(exc, "status_code", 0) >= 500
                    payload["last_error"] = type(exc).__name__
                    payload.setdefault("retries", 0)
                    if _is_blocking_auth_error(exc):
                        # Preserve the transcript indefinitely while configuration is repaired.
                        payload["next_retry_at"] = time.time() + min(1800, float(self._config.get("retry_backoff_s", 5)) * 2)
                        tmp=path.with_suffix(".json.tmp"); tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
                        logger.error("blocking worker error path=%s error_type=%s retries=%d next_retry_at=%s", path.name, type(exc).__name__, int(payload.get("retries", 0)), payload["next_retry_at"])
                        continue
                    if transient:
                        payload["transient_failures"] = int(payload.get("transient_failures", 0)) + 1
                        payload["next_retry_at"] = time.time() + min(1800, float(self._config.get("retry_backoff_s", 5)) * (2 ** min(8, payload["transient_failures"] - 1)))
                        tmp=path.with_suffix(".json.tmp"); tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
                        continue
                    if time.time() - float(payload.get("created_at", time.time())) > float(self._config.get("retry_window_s", 86400)):
                        payload["retries"] = int(self._config.get("max_retries", 3)) + 1
                    else:
                        payload["retries"] = int(payload.get("retries", 0)) + 1
                    payload["last_error"] = type(exc).__name__
                    max_retries = int(self._config.get("max_retries", 3))
                    if payload["retries"] > max_retries:
                        failed = self._queue_dir / "failed"; failed.mkdir(mode=0o700, exist_ok=True)
                        target = failed / path.name; os.replace(path, target); os.chmod(target, 0o600)
                        logger.warning("queue file moved to failed after retries path=%s retries=%d", path.name, payload["retries"])
                    else:
                        import time as _time
                        backoff = float(self._config.get("retry_backoff_s", 5))
                        payload["next_retry_at"] = _time.time() + backoff * (2 ** max(0, payload["retries"] - 1))
                        tmp=path.with_suffix(".json.tmp"); tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
                except Exception:
                    logger.exception("queue failure bookkeeping failed file=%s", path)

        backlog = 0
        if self._queue_dir:
            for p in self._queue_dir.glob('*.json'):
                try:
                    q=json.loads(p.read_text(encoding='utf-8')); backlog += int(q.get('processed_turns',0)) < (len(self._normalise_messages(q.get('messages'))) // 2)
                except Exception: backlog += 1
        logger.info("drain summary inspected=%d skipped=%d processed=%d backlog=%d", inspected_files, max(0, inspected_files-processed_files), processed_files, backlog)

    def on_session_end(self, messages: list) -> None:
        started = time.monotonic()
        logger.info("on_session_end start messages=%d write_enabled=%s", len(messages or []), self._writes_enabled())
        self._require_initialized()
        if not self._writes_enabled():
            with self._lock:
                self._buffer.clear()
            return
        normalised = self._normalise_messages(messages)
        if not normalised:
            with self._lock:
                normalised = list(self._buffer)
        if (not normalised and not (self._queue_dir and any(self._queue_dir.glob("session_*.json")))) or not self._writes_enabled():
            return
        # Durable handoff happens before the potentially slow model call. A
        # later Hermes process drains this queue, so exit watchdogs cannot
        # erase the only copy of the transcript.
        queue_file = None
        try:
            sid = self._session_id or "default"
            safe = _safe_session_name(sid)
            candidate = self._queue_dir / ("session_" + safe + ".json") if self._queue_dir else None
            if candidate and candidate.exists():
                payload = json.loads(candidate.read_text(encoding="utf-8")); payload["status"] = "ended"
                tmp = candidate.with_suffix(".json.tmp"); tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.chmod(tmp, 0o600); os.replace(tmp, candidate); os.chmod(candidate, 0o600); queue_file = candidate
            else:
                # sync_turn may receive an agent session id that is not
                # exposed back on session_end; reuse the newest session file
                # instead of writing a legacy duplicate transcript.
                candidates = sorted(self._queue_dir.glob("session_*.json"), key=lambda x: x.stat().st_mtime, reverse=True) if self._queue_dir else []
                if candidates:
                    candidate = candidates[0]
                    payload = json.loads(candidate.read_text(encoding="utf-8")); payload["status"] = "ended"
                    tmp = candidate.with_name(candidate.name + (".%d.end.tmp" % os.getpid())); tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8"); os.chmod(tmp, 0o600); os.replace(tmp, candidate); queue_file = candidate
                else:
                    queue_file = self._enqueue(normalised)
        except Exception:
            return
        logger.info("on_session_end enqueued path=%s elapsed=%.3fs", queue_file, time.monotonic() - started)
        self._launch_worker_if_needed()
        with self._lock:
            self._buffer.clear()

    # Safety-first empty hooks for lifecycle events not used in phase one.
    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        return None

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        with self._lock:
            self._buffer.clear()
            self._session_id = new_session_id or ""

    def on_pre_compress(self, messages: list) -> str:
        return ""

    def on_memory_write(self, **kwargs: Any) -> None:
        return None

    def queue_prefetch(self, messages: list, **kwargs: Any) -> None:
        return None

    def system_prompt_block(self) -> str:
        return (
            "Seed memory contains structured past experiences and observations. "
            "Values, when present, are descriptive summaries inferred from experience, not predefined rules."
        )

    def shutdown(self) -> None:
        with self._lock:
            self._buffer.clear()
        self._initialized = False
        self._llm_call = None


def register(ctx: Any) -> None:
    ctx.register_memory_provider(SeedMemoryProvider())
