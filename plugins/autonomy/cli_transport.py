"""Hermes CLI transport for runtimes without the HTTP runs API."""
import os, signal, subprocess, threading, uuid
try:
    import resource
except ImportError:
    resource = None


def _reset_address_space_limit():
    """Avoid inheriting legacy RLIMIT_AS into model-loading Hermes CLI child."""
    if not resource:
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    except (OSError, ValueError):
        pass


class CliGateway:
    def __init__(self, hermes_bin="hermes", max_turns=30, run_budget=2400, profile=None):
        self.hermes_bin = os.environ.get("AUTONOMY_HERMES_BIN", hermes_bin)
        self.profile = profile
        self.max_turns = int(max_turns)
        self.run_budget = int(run_budget)
        self.runs = {}

    def start_run(self, goal, run_id, prompt):
        rid = "cli-" + uuid.uuid4().hex
        cmd = [self.hermes_bin]
        if self.profile:
            cmd.extend(["-p", self.profile])
        cmd.extend(["chat", "--query-file", "-", "--oneshot", "--quiet", "--yolo", "--accept-hooks", "--max-turns", str(self.max_turns), "--run-budget", str(self.run_budget)])
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
        # Preserve diagnostics for Runner's partial-output recorder on non-zero exits.
        return {"status":status,"output":text or errors,"stderr":errors,"usage":{},"returncode":proc.returncode}

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

    def end(self, sid): pass
