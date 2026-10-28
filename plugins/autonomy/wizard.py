"""Interactive `hermes autonomy init` wizard with live model test and unified memory self-test."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import (
    DEFAULT_BUDGET,
    DEFAULT_CIRCUIT,
    DEFAULT_RESOURCES,
    determine_mode,
    get_autonomy_section,
    load_raw_config,
    resolve_autonomy_model,
    resolve_hermes_home,
    resolve_main_model,
    resolve_paths,
    save_raw_config,
)
from .memory_bridge import (
    get_configured_memory_provider,
    list_available_memory_plugins,
    memory_self_test,
)


def _lookup_secret_env(var_name: str, hermes_home: Optional[str | Path] = None) -> str:
    """Resolve an API key env var from os.environ or local env files without ever printing it."""
    if not var_name:
        return ""
    val = os.environ.get(var_name, "").strip()
    if val:
        return val
    home = resolve_hermes_home(hermes_home)
    env_files = [
        home / ".env",
        home.parent / ".env",
        home.parent / "secrets.env",
        home.parent / "web.env",
        Path("/etc/hermes/secrets.env"),
        Path("/etc/hermes/web.env"),
        Path("/etc/hermes/hermes.env"),
    ]
    for ef in env_files:
        if not ef.is_file():
            continue
        try:
            for line in ef.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                if s.startswith("export "):
                    s = s[7:].strip()
                k, v = s.split("=", 1)
                if k.strip() == var_name:
                    return v.strip().strip("'").strip('"')
        except OSError:
            continue
    return ""


def test_model_connection(
    model_id: str,
    *,
    base_url: str = "",
    api_key_env: str = "",
    hermes_home: Optional[str | Path] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Send a real test chat completion request to verify the selected model is reachable and working."""
    model_name = (model_id or "").strip()
    if not model_name:
        return {"ok": False, "model": model_name, "error": "model name is empty"}

    raw_cfg = load_raw_config(hermes_home)
    main_info = resolve_main_model(raw_cfg)
    endpoint_base = (
        base_url
        or main_info.get("base_url")
        or os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:18787/v1")
    ).rstrip("/")
    if not endpoint_base.endswith("/v1") and "/v1/" not in endpoint_base:
        url = endpoint_base + "/v1/chat/completions"
    else:
        url = endpoint_base + "/chat/completions"

    key_var = api_key_env or main_info.get("api_key_env") or "LAN_QWEN_API_KEY"
    token = _lookup_secret_env(key_var, hermes_home=hermes_home)
    if not token:
        token = _lookup_secret_env("API_SERVER_KEY", hermes_home=hermes_home)

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    started = time.time()
    try:
        req = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=timeout) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        elapsed_ms = round((time.time() - started) * 1000, 1)
        choices = data.get("choices") if isinstance(data, dict) else None
        if status == 200 and isinstance(choices, list) and len(choices) > 0:
            reply = str((choices[0].get("message") or {}).get("content") or "").strip()
            return {
                "ok": True,
                "model": model_name,
                "status_code": status,
                "latency_ms": elapsed_ms,
                "reply_preview": reply[:80],
            }
        return {
            "ok": False,
            "model": model_name,
            "status_code": status,
            "latency_ms": elapsed_ms,
            "error": f"unexpected response structure from {url}",
        }
    except HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return {
            "ok": False,
            "model": model_name,
            "status_code": exc.code,
            "error": f"HTTP {exc.code}: {err_body or exc.reason}",
        }
    except URLError as exc:
        return {
            "ok": False,
            "model": model_name,
            "error": f"connection failed: {exc.reason}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": model_name,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_init_wizard(
    *,
    hermes_home: Optional[str | Path] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    model_tester: Optional[Callable[..., Dict[str, Any]]] = None,
    memory_tester: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the 3-step `hermes autonomy init` wizard:
    Step (a): Select model (default inherits Hermes main model, or specify separate model).
              Must send a live test request and pass before proceeding; on failure report error & re-prompt.
    Step (b): Select external memory plugin (lists available, reads current memory.provider,
              and performs a write + read-back self-test via Hermes unified MemoryProvider interface).
    Step (c): Configure relaxed circuit breaker and daily token budget quotas.
    """
    home = resolve_hermes_home(hermes_home)
    raw_cfg = load_raw_config(home)
    main_info = resolve_main_model(raw_cfg)
    auto_sec = get_autonomy_section(raw_cfg)
    tester = model_tester or test_model_connection
    mem_tester = memory_tester or memory_self_test

    output_fn("=== Hermes Autonomy 插件初始化向导 (hermes autonomy init) ===")
    default_main = main_info.get("model") or "qwen-5060"
    default_prov = main_info.get("provider") or "custom:lan-qwen"
    default_url = main_info.get("base_url") or ""
    default_key_env = main_info.get("api_key_env") or "LAN_QWEN_API_KEY"

    # --- Step (a): Model Selection + Live Test Request ---
    output_fn(f"\n[步骤 1/3] 选择模型 (当前 Hermes 主模型: {default_main}, provider: {default_prov})")
    selected_model_cfg: Optional[Dict[str, Any]] = None
    effective_main_model = default_main

    while selected_model_cfg is None:
        output_fn("  1) 默认继承 Hermes 主模型（单模型 → 对话优先模式，用户说话暂停思考）")
        output_fn("  2) 单独指定自主思考模型（双模型 → 并行模式，对话不打断思考）")
        try:
            choice = (input_fn("请选择模型模式 [1/2, 默认 1]: ") or "1").strip()
        except EOFError:
            return {
                "ok": False,
                "step": "model_select",
                "error": "model selection aborted after test failure or EOF",
            }

        if choice == "2":
            try:
                main_override = input_fn(f"确认或修改 Hermes 主模型名称 [默认 {effective_main_model}]: ").strip()
                if main_override:
                    effective_main_model = main_override
                custom_model = input_fn("请输入自主思考模型名称 (例如 qwen38-flash): ").strip()
                custom_url = input_fn(f"请输入模型 API 地址 [默认复用 {default_url or '主模型地址'}]: ").strip() or default_url
                custom_key_env = input_fn(f"请输入 API Key 环境变量名 [默认 {default_key_env}]: ").strip() or default_key_env
            except EOFError:
                return {"ok": False, "step": "model_select", "error": "EOF while reading custom model"}

            if not custom_model:
                output_fn("[错误] 自主思考模型名称不能为空，请重新选择。")
                continue

            output_fn(f"正在向主模型 '{effective_main_model}' 发送测试请求...")
            res_main = tester(
                effective_main_model,
                base_url=custom_url,
                api_key_env=custom_key_env,
                hermes_home=home,
            )
            if not res_main.get("ok"):
                output_fn(f"[失败] 主模型 '{effective_main_model}' 测试请求未通过: {res_main.get('error')}。请重新选择！")
                continue
            output_fn(f"[通过] 主模型 '{effective_main_model}' 测试成功 (延迟 {res_main.get('latency_ms', 0)} ms)")

            output_fn(f"正在向自主思考模型 '{custom_model}' 发送测试请求...")
            res_auto = tester(
                custom_model,
                base_url=custom_url,
                api_key_env=custom_key_env,
                hermes_home=home,
            )
            if not res_auto.get("ok"):
                output_fn(f"[失败] 自主思考模型 '{custom_model}' 测试请求未通过: {res_auto.get('error')}。请重新选择！")
                continue
            output_fn(f"[通过] 自主思考模型 '{custom_model}' 测试成功 (延迟 {res_auto.get('latency_ms', 0)} ms)")

            selected_model_cfg = {
                "inherit": False,
                "default": custom_model,
                "provider": default_prov,
                "base_url": custom_url,
                "api_key_env": custom_key_env,
                "main_model": effective_main_model,
                "test_results": {"main": res_main, "autonomy": res_auto},
            }
        else:
            try:
                main_override = input_fn(f"确认或修改继承的 Hermes 主模型名称 [默认 {effective_main_model}]: ").strip()
                if main_override:
                    effective_main_model = main_override
            except EOFError:
                pass

            output_fn(f"正在向模型 '{effective_main_model}' 发送测试请求...")
            res = tester(
                effective_main_model,
                base_url=default_url,
                api_key_env=default_key_env,
                hermes_home=home,
            )
            if not res.get("ok"):
                output_fn(f"[失败] 模型 '{effective_main_model}' 测试请求未通过: {res.get('error')}。请重新选择！")
                continue
            output_fn(f"[通过] 模型 '{effective_main_model}' 测试成功 (延迟 {res.get('latency_ms', 0)} ms)")
            selected_model_cfg = {
                "inherit": True,
                "default": effective_main_model,
                "provider": default_prov,
                "base_url": default_url,
                "api_key_env": default_key_env,
                "main_model": effective_main_model,
                "test_results": {"main": res},
            }

    # --- Step (b): External Memory Plugin Selection + Write/Read-back Self-Test ---
    plugins_list = list_available_memory_plugins(home)
    current_mem = get_configured_memory_provider(home) or "seed_memory"
    output_fn(f"\n[步骤 2/3] 选择外置记忆插件 (当前 memory.provider: {current_mem})")
    for idx, p in enumerate(plugins_list, 1):
        mark = " (当前配置)" if p.get("name") == current_mem else ""
        output_fn(f"  {idx}) {p.get('name')}{mark} - {p.get('description', '')}")

    selected_memory: Optional[str] = None
    mem_test_result: Dict[str, Any] = {}
    while selected_memory is None:
        try:
            raw_mem_in = input_fn(f"请输入外置记忆插件名称或序号 [默认 {current_mem}]: ").strip()
        except EOFError:
            raw_mem_in = ""
        if not raw_mem_in:
            candidate_mem = current_mem
        elif raw_mem_in.isdigit() and 1 <= int(raw_mem_in) <= len(plugins_list):
            candidate_mem = plugins_list[int(raw_mem_in) - 1]["name"]
        else:
            candidate_mem = raw_mem_in

        output_fn(f"正在通过 Hermes 统一记忆接口对 '{candidate_mem}' 进行写入+读回自检...")
        mem_res = mem_tester(candidate_mem, hermes_home=home)
        if not mem_res.get("ok"):
            output_fn(f"[失败] 记忆插件 '{candidate_mem}' 读写自检未通过: {mem_res.get('error')}")
            try:
                retry_q = input_fn("请重新选择记忆插件（按回车重试或输入其他插件名）: ").strip()
                if retry_q:
                    current_mem = retry_q
                continue
            except EOFError:
                return {
                    "ok": False,
                    "step": "memory_select",
                    "error": mem_res.get("error") or "memory self-test failed",
                }
        output_fn(f"[通过] 记忆插件 '{candidate_mem}' 写入与读回自检成功 ({mem_res.get('detail', 'ok')})")
        selected_memory = candidate_mem
        mem_test_result = mem_res

    # --- Step (c): Relaxed Circuit Breaker & Budget Quotas ---
    output_fn("\n[步骤 3/3] 设置宽松熔断与预算额度")
    cur_budget = auto_sec.get("budget") if isinstance(auto_sec.get("budget"), dict) else {}
    cur_circuit = auto_sec.get("circuit") if isinstance(auto_sec.get("circuit"), dict) else {}
    cur_res = auto_sec.get("resources") if isinstance(auto_sec.get("resources"), dict) else {}

    def _ask_int(prompt_text: str, default_val: int) -> int:
        try:
            val_str = input_fn(f"{prompt_text} [默认 {default_val}]: ").strip()
            return int(val_str) if val_str else default_val
        except (EOFError, ValueError):
            return default_val

    daily_tokens = _ask_int("每日 Token 预算上限 (daily_tokens)", int(cur_budget.get("daily_tokens", DEFAULT_BUDGET["daily_tokens"])))
    daily_calls = _ask_int("每日思考轮次上限 (daily_calls)", int(cur_budget.get("daily_calls", DEFAULT_BUDGET["daily_calls"])))
    failure_threshold = _ask_int("连续失败熔断阈值 (failure_threshold)", int(cur_circuit.get("failure_threshold", DEFAULT_CIRCUIT["failure_threshold"])))
    cooldown_seconds = _ask_int("熔断冷却恢复秒数 (cooldown_seconds)", int(cur_circuit.get("cooldown_seconds", DEFAULT_CIRCUIT["cooldown_seconds"])))
    memory_max_mb = _ask_int("单实例内存上限 MB (memory_max_mb)", int(cur_res.get("memory_max_mb", DEFAULT_RESOURCES["memory_max_mb"])))

    # Update config.yaml
    if not isinstance(raw_cfg.get("model"), dict):
        raw_cfg["model"] = {"default": effective_main_model, "provider": default_prov}
    else:
        raw_cfg["model"]["default"] = effective_main_model

    if not isinstance(raw_cfg.get("memory"), dict):
        raw_cfg["memory"] = {}
    raw_cfg["memory"]["provider"] = selected_memory

    model_block: Dict[str, Any] = {
        "inherit": bool(selected_model_cfg["inherit"]),
    }
    if not selected_model_cfg["inherit"]:
        model_block["default"] = selected_model_cfg["default"]
        model_block["provider"] = selected_model_cfg["provider"]
        if selected_model_cfg.get("base_url"):
            model_block["base_url"] = selected_model_cfg["base_url"]
        if selected_model_cfg.get("api_key_env"):
            model_block["api_key_env"] = selected_model_cfg["api_key_env"]

    paths = resolve_paths(raw_cfg, hermes_home=home)
    raw_cfg["autonomy"] = {
        "enabled": True,
        "model": model_block,
        "memory": {
            "provider": selected_memory,
        },
        "budget": {
            "daily_tokens": daily_tokens,
            "daily_prompt_tokens": max(daily_tokens, int(daily_tokens * 0.8)),
            "daily_calls": daily_calls,
        },
        "circuit": {
            "failure_threshold": failure_threshold,
            "cooldown_seconds": cooldown_seconds,
        },
        "resources": {
            "memory_max_mb": memory_max_mb,
            "max_run_seconds": int(cur_res.get("max_run_seconds", DEFAULT_RESOURCES["max_run_seconds"])),
            "quiet_seconds": int(cur_res.get("quiet_seconds", DEFAULT_RESOURCES["quiet_seconds"])),
            "min_quiet_start_seconds": int(cur_res.get("min_quiet_start_seconds", DEFAULT_RESOURCES["min_quiet_start_seconds"])),
        },
        "paths": {
            "db_path": paths["db_path"],
            "stop_file": paths["stop_file"],
            "activity_file": paths["activity_file"],
            "thought_log": paths["thought_log"],
        },
    }

    # Ensure plugin is enabled in plugins.enabled
    if not isinstance(raw_cfg.get("plugins"), dict):
        raw_cfg["plugins"] = {}
    enabled_list = list(raw_cfg["plugins"].get("enabled") or [])
    if "autonomy" not in enabled_list:
        enabled_list.append("autonomy")
    raw_cfg["plugins"]["enabled"] = enabled_list

    mode = determine_mode(raw_cfg, hermes_home=home)
    # In parallel mode, ensure gateway.api_server.max_concurrent_runs >= 4 so chat and autonomy do not block each other
    if mode == "parallel":
        gw_sec = raw_cfg.setdefault("gateway", {})
        if isinstance(gw_sec, dict):
            api_sec = gw_sec.setdefault("api_server", {})
            if isinstance(api_sec, dict):
                cur_cap = int(api_sec.get("max_concurrent_runs", 10) or 10)
                if 0 < cur_cap < 4:
                    api_sec["max_concurrent_runs"] = 4

    cfg_path = save_raw_config(raw_cfg, hermes_home=home)
    output_fn(f"\n[完成] 配置已写入 {cfg_path}")
    output_fn(
        f"当前模式: {mode} ({'双模型并行：对话不打断思考' if mode == 'parallel' else '单模型对话优先：用户说话暂停思考，静默后续上'})"
    )
    return {
        "ok": True,
        "config_path": str(cfg_path),
        "mode": mode,
        "main_model": effective_main_model,
        "autonomy_model": selected_model_cfg["default"],
        "inherit_model": bool(selected_model_cfg["inherit"]),
        "memory_provider": selected_memory,
        "memory_self_test": mem_test_result,
        "model_test": selected_model_cfg.get("test_results", {}),
        "budget": raw_cfg["autonomy"]["budget"],
        "circuit": raw_cfg["autonomy"]["circuit"],
        "resources": raw_cfg["autonomy"]["resources"],
    }
