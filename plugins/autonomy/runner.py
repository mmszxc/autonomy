import os, uuid, json, threading, time, signal, re, subprocess, sys
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


from .prompt import build_tick_prompt, load_extra_rules
from .proposals import parse
from .config import determine_mode
from .thought_log import append_thought_log, resolve_thought_log_path
from .transcript import extract_run_transcript
from .memory_bridge import load_memory_provider_instance


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


class Runner:
    def __init__(
        self,
        db,
        budget,
        circuit,
        stop_file="/var/lib/hermes/STOP_AUTONOMY",
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
    ):
        self.db = db
        self.budget = budget
        self.circuit = circuit
        self.stop_file = stop_file
        self.llm = llm
        self.gateway = gateway
        self.lock_path = lock_path or str(getattr(db, "path", "autonomy.db")) + ".run.lock"
        self.data_dir = os.path.dirname(self.lock_path) or "."
        self.workspace = workspace
        self.worker_timeout_s = max(1, int(worker_timeout_s or 360))
        self.seed_queue_dir = Path(seed_queue_dir).expanduser() if seed_queue_dir else None
        self.active_run_file = os.path.join(os.path.dirname(self.lock_path), "AUTONOMY_REMOTE_RUN")
        self.preempt_file = os.path.join(os.path.dirname(self.lock_path), "PREEMPT_NOW")
        self.stop_wait_s = 600
        self.max_seconds = float(os.environ.get("AUTONOMY_RUN_MAX_SECONDS", run_max_seconds))
        self.prefetch_max_chars = int(os.environ.get("AUTONOMY_PREFETCH_MAX_CHARS", prefetch_max_chars) or 3000)
        self.prefetch_timeout_s = float(os.environ.get("AUTONOMY_PREFETCH_TIMEOUT_S", prefetch_timeout_s) or 20)
        self.activity_file = activity_file or os.environ.get(
            "AUTONOMY_ACTIVITY_FILE", "/var/lib/hermes/autonomy_data/LAST_USER_ACTIVITY"
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
        partial = bool(raw or usage.get("total_tokens") or tools)
        summary = "partial=%s chars=%d tools=%d elapsed=%.1f usage=%s prefetch=%s" % (
            partial, len(raw), len(tools), elapsed, json.dumps(usage, ensure_ascii=False), json.dumps(self._prefetch_meta, ensure_ascii=False)
        )
        self.db.append_event(run_id=run, kind="run_timeout", exit_reason=reason, summary=summary[:1200])
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

    def _enqueue_completed_turn(self, run_id, summary, assistant_text):
        """Bridge standalone autonomy ticks into seed_memory's queue protocol.

        Gateway-driven ticks do not reach Hermes' normal turn finalizer, so its
        ``sync_turn`` hook is never called.  Write the same ended-session JSON
        shape that seed_memory consumes, then invoke its detached worker.  Queue
        creation must never turn an otherwise successful thought into a failure.
        """
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
                "messages": [
                    {"role": "user", "content": "自主思考轮摘要：" + str(user_visible)[:4000]},
                    {"role": "assistant", "content": str(assistant_text or "")[:16000]},
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
            if os.name != "nt":
                unit_state = queue_dir / ".worker.unit"
                launch_lock = queue_dir / ".worker.launch.lock"
                with _file_lock(launch_lock):
                    previous = unit_state.read_text(encoding="utf-8").strip() if unit_state.exists() else ""
                    active = bool(previous) and subprocess.run(["systemctl", "--user", "is-active", "--quiet", previous], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                    if active:
                        return {"queued": True, "path": str(target), "worker": "submitted(" + previous + ")", "unit": previous, "elapsed_s": round(time.monotonic() - started, 3)}
                    profile = re.sub(r"[^A-Za-z0-9_-]", "-", self.hermes_home.name) or "default"
                    unit = "hermes-seed-worker-%s-%d" % (profile, int(time.time()))
                    command = ["systemd-run", "--user", "--collect", "--quiet", "--unit", unit, "--property=RuntimeMaxSec=900s", "--property=StandardOutput=append:%s" % worker_log, "--property=StandardError=append:%s" % worker_log, "--setenv=HERMES_HOME=%s" % self.hermes_home, "--working-directory=%s" % worker.parent.parent, sys.executable, str(worker), "--once"]
                    try:
                        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                    except FileNotFoundError:
                        logger.warning("systemd-run unavailable; falling back to foreground seed worker")
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        logger.warning("systemd-run submit failed type=%s; falling back to foreground seed worker", type(exc).__name__)
                    else:
                        if result.returncode == 0:
                            tmp = unit_state.with_suffix(".tmp"); tmp.write_text(unit, encoding="utf-8"); os.replace(tmp, unit_state)
                            return {"queued": True, "path": str(target), "worker": "submitted(" + unit + ")", "unit": unit, "elapsed_s": round(time.monotonic() - started, 3)}
                        logger.warning("systemd-run submit failed rc=%s; falling back to foreground seed worker", result.returncode)
            log_handle = open(worker_log, "ab", buffering=0)
            kwargs = {"cwd": str(worker.parent.parent), "env": env, "stdin": subprocess.DEVNULL, "stdout": log_handle, "stderr": log_handle}
            if os.name != "nt": kwargs["start_new_session"] = True
            proc = subprocess.Popen([sys.executable, str(worker), "--once"], **kwargs)
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
            return {"queued": True, "path": str(target), "worker": "ok" if returncode == 0 else "exit_%s" % returncode, "elapsed_s": round(time.monotonic() - started, 3)}
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

    def _prefetch_memory(self, run, recent_events):
        query_parts = ["最近的、情绪强的、未完成的记忆"]
        for event in (recent_events or [])[:5]:
            summary = event.get("summary") or event.get("exit_reason") or ""
            if summary:
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
for path in (plugin_dir + "/_vendor", plugin_dir + "/_vendor_ml", plugin_dir):
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
                    diagnostic = re.sub(r"(?i)(api[_-]?key|token|secret|password)\\s*[=:]\\s*[^\\s]+", r"\\1=<redacted>", diagnostic)
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
            self.db.append_event(run_id=run, kind="memory_prefetch", exit_reason="degraded", summary=diagnostic)
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

        if self.db:
            self.db.c.execute(
                "insert or replace into last_tick values(1,?,?,?,?)",
                ("running", started, None, None),
            )
            self.db.c.commit()

        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        f = open(self.lock_path, "a+")
        acquired = False
        try:
            acquired = self._acquire_lock(f)
            if not acquired:
                return {"status": "busy", "run_id": run}
            if os.path.exists(self.stop_file):
                return {"status": "stopped", "run_id": run}
            if not self.budget.allow() or not self.circuit.allow():
                return {"status": "blocked", "run_id": run}

            goals = [g for g in self.db.list_goals() if g.get("status") in ("approved", "active")]
            if goal_id:
                goals = [g for g in goals if g.get("id") == goal_id]
            recent_events = self.db.list_events(5)
            memory_review, memory_seed_ids = self._prefetch_memory(run, recent_events)
            rules = load_extra_rules(self.data_dir)
            prompt = build_tick_prompt(
                goals, recent_events, memory_review=memory_review,
                workspace=self.workspace, extra_rules=rules,
            )
            raw = ""
            usage = {}
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
                        deadline = time.time() + self.stop_wait_s
                        while time.time() < deadline:
                            final = self.gateway.get_run(rid)
                            if final.get("status", "") in ("cancelled", "completed", "failed", "stopped"):
                                break
                            time.sleep(1)
                        elapsed = time.time() - started
                        raw, usage, tools = self._run_snapshot(final)
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
                                raw, usage, tools = self._run_snapshot(final)
                                partial, _ = self._record_partial(run, raw, usage, time.time() - started, "preempted", tools)
                                return {"status": "preempted", "run_id": run, "remote_run_id": rid, "partial": partial, "raw_text": raw, "usage": usage}
                            time.sleep(1)
                        self.db.append_event(
                            run_id=run, kind="stuck_run", exit_reason="stop_timeout", summary=rid
                        )
                        return {"status": "stuck_run", "run_id": run, "remote_run_id": rid}

                    state = self.gateway.get_run(rid)
                    if state.get("status") in ("completed", "failed", "cancelled", "stopped"):
                        raw = state.get("output") or state.get("text") or state.get("content", "")
                        usage = state.get("usage", {})
                        usage = {
                            "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
                            "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
                            "total_tokens": usage.get("total_tokens", 0),
                        }
                        if state.get("status") == "failed":
                            # Preserve CLI stderr/stdout before propagating the failure.
                            diagnostic = (raw or state.get("stderr") or state.get("error") or "run failed")[-2000:]
                            diagnostic = re.sub(r"(?i)(api[_-]?key|token|secret|password)\\s*[=:]\\s*[^\\s]+", r"\\1=<redacted>", diagnostic)
                            elapsed = time.time() - started
                            tools = state.get("tool_calls") or []
                            self._record_partial(run, diagnostic, usage, elapsed, "failed", tools)
                            error_class = state.get("error_class") or "RuntimeError"
                            self.db.append_event(run_id=run, kind="run_failed", exit_reason=error_class, summary=("returncode=%s reason=%s" % (state.get("returncode"), diagnostic))[:1200])
                            raise RuntimeError(diagnostic)
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

            self.budget.consume(
                int(usage.get("completion_tokens", 0)),
                int(usage.get("prompt_tokens", 0)),
            )

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

            try:
                transcript = self._extract_transcript(started, time.time())
                transcript_text = transcript.get("text") or str(raw or "")
                transcript_meta = ""
                if transcript.get("session_id"):
                    transcript_meta = "\n【完整过程】session=%s messages=%s tools=%s truncated=%s" % (transcript.get("session_id"), transcript.get("message_count", 0), transcript.get("tool_call_count", 0), transcript.get("truncated", False))
                    self.db.append_event(run_id=run, goal_id=goal_id, kind="transcript", exit_reason="ok", summary=transcript_meta[1:][:1200])
                elif transcript.get("error"):
                    self.db.append_event(run_id=run, goal_id=goal_id, kind="transcript", exit_reason="unavailable", summary=str(transcript["error"])[:1200])
                queue_result = self._enqueue_completed_turn(run, summary, transcript_text)
                self.db.append_event(
                    run_id=run, goal_id=goal_id, kind="seed_queue",
                    exit_reason="queued" if queue_result.get("queued") else "degraded",
                    summary=("path=%s worker=%s elapsed_s=%s" % (queue_result.get("path") or queue_result.get("reason") or "unknown", queue_result.get("worker", "not_started"), queue_result.get("elapsed_s", 0)))[:1200],
                )
                append_thought_log(
                    self.thought_log_path,
                    run_id=run,
                    raw_text="【本轮预取记忆】count=%d chars=%d truncated=%s seed_ids=%s\n【遥测】elapsed=%.1f prompt=%s completion=%s total=%s\n【结束原因】ok%s\n%s" % (self._prefetch_meta["count"], self._prefetch_meta["chars"], self._prefetch_meta["truncated"], ",".join(memory_seed_ids) or "none", elapsed, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("total_tokens", 0), transcript_meta, transcript_text),
                    summary=summary,
                    mode=self.mode,
                    model=self.model_name,
                    usage=usage,
                    status="ok",
                )
            except (OSError, UnicodeError) as log_error:
                # A successful model turn with an unavailable log is explicit;
                # never report silent evidence loss.
                self.db.append_event(run_id=run, goal_id=goal_id, kind="thought_log_error", exit_reason=type(log_error).__name__, summary=str(log_error)[:300])

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
            self.circuit.failure(http_status)
            reason = " ".join(str(e).split())[:240]
            self.db.append_event(
                run_id=run,
                goal_id=goal_id,
                kind="tick",
                exit_reason=type(e).__name__,
                summary=str(raw or reason)[:300],
            )
            error = {"error": type(e).__name__, "reason": reason}
            if http_status is not None:
                error["http_status"] = int(http_status)
            return {"status": "failed", "run_id": run, "summary": error}
        finally:
            if self.db:
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
