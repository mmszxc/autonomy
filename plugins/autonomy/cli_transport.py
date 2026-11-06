"""Hermes CLI transport for runtimes without the HTTP runs API."""
import hashlib, os, re, signal, subprocess, threading, time, uuid
from pathlib import Path
try:
    import resource
except ImportError:
    resource = None


_SECRET = re.compile(rb"(?i)(authorization:\s*bearer\s+|password\s*[=:]\s*|api[_-]?key\s*[=:]\s*|token\s*[=:]\s*)[^\s,;]+")


def _redact(data: bytes) -> bytes:
    return _SECRET.sub(lambda m: m.group(1) + b"[REDACTED]", data)


def capture_process_evidence(stdout, stderr, exit_code, *, signal_number=None,
                             timed_out=False, oom=False, buffer_dir=None,
                             max_buffer=5 * 1024**2):
    """Capture bounded evidence before any JSON/text parser consumes process output."""
    stdout = stdout if isinstance(stdout, bytes) else str(stdout or "").encode("utf-8", "replace")
    stderr = stderr if isinstance(stderr, bytes) else str(stderr or "").encode("utf-8", "replace")
    out_digest, err_digest = hashlib.sha256(stdout).hexdigest(), hashlib.sha256(stderr).hexdigest()
    out_head, out_tail = _redact(stdout[:256]), _redact(stdout[-256:])
    err_head, err_tail = _redact(stderr[:128]), _redact(stderr[-128:])
    framed = b"STDOUT\n" + stdout + b"\nSTDERR\n" + stderr
    truncated = len(framed) > max_buffer
    path = None
    if buffer_dir is not None:
        root = Path(buffer_dir); root.mkdir(parents=True, exist_ok=True)
        path = root / ("evidence-%d-%s.bin" % (time.time_ns(), uuid.uuid4().hex[:8]))
        # The short-lived buffer may contain secrets; restrict permissions before writing.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, framed[:max_buffer])
        finally:
            os.close(fd)
    return {
        "stdout_sha256": out_digest, "stderr_sha256": err_digest,
        "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
        "stdout_head": out_head, "stdout_tail": out_tail,
        "stderr_head": err_head, "stderr_tail": err_tail,
        "exit_code": int(exit_code), "signal": signal_number,
        "timed_out": bool(timed_out), "oom": bool(oom), "truncated": truncated,
        "buffer_path": str(path) if path else None,
        "buffer_expires_at": time.time() + 24 * 3600,
        "permanent_bytes": len(out_head) + len(out_tail) + len(err_head) + len(err_tail),
    }


def _reset_address_space_limit():
    """Avoid inheriting legacy RLIMIT_AS into model-loading Hermes CLI child."""
    if not resource:
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    except (OSError, ValueError):
        pass


def build_reflection_command(gateway, run_id, reflection_profile, prompt):
    """Build against the deployed `hermes chat --help` contract."""
    cmd = [gateway.hermes_bin, "-p", reflection_profile, "chat"]
    if gateway.model:
        cmd.extend(["--model", gateway.model])
    if gateway.provider:
        cmd.extend(["--provider", gateway.provider])
    session = "autonomy-reflect-%s-%s" % (run_id, uuid.uuid4().hex[:12])
    cmd.extend([
        "--session-id", session, "--timeout", str(gateway.run_budget),
        "--no-save", "--oneshot", "--quiet", str(prompt),
    ])
    return cmd


class ReflectionCallError(RuntimeError):
    def __init__(self, reason, detail="", *, attempted=True):
        super().__init__(detail or reason)
        self.reason = reason
        self.attempted = attempted


class CliGateway:
    def __init__(self, hermes_bin="hermes", max_turns=30, run_budget=2400, profile=None, model=None, provider=None):
        self.hermes_bin = os.environ.get("AUTONOMY_HERMES_BIN", hermes_bin)
        self.profile = profile
        self.model = str(model or "").strip()
        self.provider = str(provider or "").strip()
        self.max_turns = int(max_turns)
        self.run_budget = int(run_budget)
        self.evidence_dir = os.environ.get("AUTONOMY_EVIDENCE_BUFFER")
        self.runs = {}
        self.evidence = {}

    def start_run(self, goal, run_id, prompt):
        rid = "cli-" + uuid.uuid4().hex
        cmd = [self.hermes_bin]
        if self.profile:
            cmd.extend(["-p", self.profile])
        cmd.append("chat")
        if self.model:
            cmd.extend(["--model", self.model])
        if self.provider:
            cmd.extend(["--provider", self.provider])
        cmd.extend(["--query-file", "-", "--oneshot", "--quiet", "--yolo", "--accept-hooks", "--max-turns", str(self.max_turns), "--run-budget", str(self.run_budget)])
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "errors": "replace"}
        if os.name == "nt": kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = _reset_address_space_limit
        proc = subprocess.Popen(cmd, **kwargs)
        out, err = [], []
        def drain(stream, target):
            for line in iter(stream.readline, ""): target.append(line)
        threading.Thread(target=drain, args=(proc.stdout,out), daemon=True).start()
        threading.Thread(target=drain, args=(proc.stderr,err), daemon=True).start()
        proc.stdin.write(prompt); proc.stdin.close()
        self.runs[rid] = (proc,out,err)
        return {"run_id": rid}

    def get_run(self, rid):
        proc,out,err=self.runs[rid]
        text, errors = "".join(out), "".join(err)
        # Hermes chat may return non-zero after producing a complete answer
        # (for example, a post-run plugin warning). Preserve useful thought as ok.
        status="running" if proc.poll() is None else ("completed" if proc.returncode == 0 or text.strip() else "failed")
        evidence = None
        if proc.returncode is not None:
            evidence = self.evidence.get(rid)
            if evidence is None:
                evidence = capture_process_evidence(text, errors, proc.returncode, buffer_dir=self.evidence_dir)
                self.evidence[rid] = evidence
        # Preserve diagnostics for Runner's partial-output recorder on non-zero exits.
        return {"status":status,"output":text or errors,"stderr":errors,"usage":{},"returncode":proc.returncode,"evidence":evidence}

    def stop_run(self, rid):
        proc,_,_=self.runs[rid]
        if proc.poll() is None:
            try:
                if os.name == "nt": proc.terminate()
                else: os.killpg(proc.pid, signal.SIGTERM)
            except OSError: pass
        return self.get_run(rid)

    def append_run_message(self, rid, content):
        # One-shot CLI has no mid-run message endpoint; runner records advisory skip.
        raise RuntimeError("cli_transport_has_no_append_message")

    def run_reflection(self, prompt, run_id, *, reflection_profile):
        # The deployed CLI has no --prompt-file/stdin option for chat. Passing
        # one argv element preserves newlines and quotes without shell parsing.
        cmd = build_reflection_command(self, run_id, reflection_profile, prompt)
        kwargs = {
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": True, "errors": "replace",
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = _reset_address_space_limit
        proc = subprocess.Popen(cmd, **kwargs)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=self.run_budget + 30)
            except subprocess.TimeoutExpired as exc:
                self._kill_process_group(proc)
                proc.communicate()
                raise ReflectionCallError("reflection_timeout", str(exc)) from exc
            except BaseException:
                self._kill_process_group(proc)
                proc.communicate()
                raise
            evidence = capture_process_evidence(stdout or "", stderr or "", proc.returncode, buffer_dir=self.evidence_dir)
            self.evidence["reflection:" + str(run_id)] = evidence
            text = (stdout or "").strip()
            if proc.returncode != 0:
                raise ReflectionCallError("reflection_transport_failed", (stderr or "")[-500:])
            if not text:
                raise ReflectionCallError("reflection_empty_output")
            return {"text": text, "usage": {"calls": 1}}
        finally:
            if proc.poll() is None:
                self._kill_process_group(proc)

    @staticmethod
    def _kill_process_group(proc):
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def end(self, sid): pass
