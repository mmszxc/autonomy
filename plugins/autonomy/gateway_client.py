import json, os
from urllib.request import Request, urlopen
import subprocess, threading, signal


class GatewayError(Exception):
    def __init__(self, status, kind="error"):
        self.status_code = status
        self.kind = kind


class GatewayClient:
    def __init__(self, base_url=None, key=None, timeout=None, model=None, provider=None):
        self.base = (base_url or os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:18787")).rstrip("/")
        self.key = key if key is not None else os.environ.get("API_SERVER_KEY", "")
        self.timeout = float(timeout or os.environ.get("AUTONOMY_GATEWAY_TIMEOUT", "1500"))
        self.model = model if model is not None else os.environ.get("AUTONOMY_MODEL", "")
        self.provider = provider if provider is not None else os.environ.get("AUTONOMY_PROVIDER", "")

    def _req(self, method, path, body=None, headers=None, timeout=None):
        h = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        h.update(headers or {})
        raw = None if body is None else json.dumps(body).encode()
        try:
            with urlopen(
                Request(self.base + path, data=raw, headers=h, method=method),
                timeout=self.timeout if timeout is None else timeout,
            ) as r:
                return r.status, json.loads(r.read())
        except Exception as e:
            status = getattr(e, "code", 0)
            if status in (401, 403, 404):
                raise GatewayError(status, "auth_blocked")
            raise

    def chat(self, goal, run, thoughts, seeds, instruction):
        goal_id = goal.get("id", "none") if isinstance(goal, dict) else str(goal or "none")
        sid = "autonomy_" + goal_id + "_" + run
        body = {
            "model": self.model or "qwen-5060",
            "messages": [{"role": "user", "content": instruction}],
            "max_tokens": int(os.environ.get("AUTONOMY_MAX_OUTPUT_TOKENS", "384")),
        }
        if self.provider:
            body["provider"] = self.provider
        status, data = self._req("POST", "/v1/chat/completions", body, {"X-Hermes-Session-Id": sid})
        choice = data.get("choices", [{}])[0]
        return sid, choice.get("message", {}).get("content", ""), data.get("usage", {})

    def start_run(self, goal, run, instruction):
        body = {"input": instruction, "session_id": "autonomy_" + run}
        if self.model:
            body["model"] = self.model
        if self.provider:
            body["provider"] = self.provider
        return self._req("POST", "/v1/runs", body, timeout=min(self.timeout, 30))[1]

    def get_run(self, run_id):
        return self._req("GET", "/v1/runs/" + str(run_id))[1]

    def stop_run(self, run_id):
        return self._req("POST", "/v1/runs/" + str(run_id) + "/stop", {})[1]

    def append_run_message(self, run_id, content):
        """Best-effort soft-wrap prompt; older gateways may return 404."""
        return self._req("POST", "/v1/runs/" + str(run_id) + "/messages", {"role": "user", "content": content})[1]

    def end(self, sid):
        return self._req("PATCH", "/api/sessions/" + sid, {"end_reason": "autonomy_tick"}, {"X-Hermes-Session-Id": sid})
