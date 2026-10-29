"""Configuration loader, mode detector, and user-activity recorder for the autonomy plugin."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml


DEFAULT_BUDGET = {
    "daily_calls": 200,
    "daily_tokens": 5_000_000,
    "daily_prompt_tokens": 4_000_000,
}

DEFAULT_CIRCUIT = {
    "failure_threshold": 5,
    "cooldown_seconds": 600,
}

DEFAULT_RESOURCES = {
    "memory_max_mb": 1024,
    "max_run_seconds": 1800,
    "quiet_seconds": 300,
    "min_quiet_start_seconds": 300,
}


def resolve_hermes_home(hermes_home: Optional[str | Path] = None) -> Path:
    if hermes_home:
        return Path(hermes_home).expanduser().resolve()
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    # If running inside hermes_home/plugins/autonomy/
    here = Path(__file__).resolve()
    if here.parent.parent.name == "plugins" and (here.parent.parent.parent / "config.yaml").exists():
        return here.parent.parent.parent
    # If running inside <repo>/autonomy/
    repo_home = here.parent.parent / "hermes_home"
    if repo_home.exists():
        return repo_home.resolve()
    opt_home = Path("/var/lib/hermes/hermes_home")
    if opt_home.exists():
        return opt_home
    return Path.home() / ".hermes"


def resolve_base_dir(hermes_home: Optional[str | Path] = None) -> Path:
    home = resolve_hermes_home(hermes_home)
    if home.name == "hermes_home":
        return home.parent
    return home


def load_raw_config(hermes_home: Optional[str | Path] = None) -> Dict[str, Any]:
    home = resolve_hermes_home(hermes_home)
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_raw_config(raw_cfg: Dict[str, Any], hermes_home: Optional[str | Path] = None) -> Path:
    home = resolve_hermes_home(hermes_home)
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(raw_cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return cfg_path


def _memory_limit_mb(value: Any) -> int:
    if isinstance(value, str) and value.strip().lower().endswith("g"):
        try:
            return int(float(value.strip()[:-1]) * 1024)
        except ValueError:
            return 1024
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1024


def get_autonomy_section(raw_cfg: Dict[str, Any]) -> Dict[str, Any]:
    direct = raw_cfg.get("autonomy")
    if isinstance(direct, dict):
        result = dict(direct)
        limits = direct.get("limits") if isinstance(direct.get("limits"), dict) else {}
        # Normalize canonical flat schema into the legacy read view consumed by
        # status/Runner while preserving direct values for new callers.
        result.setdefault("memory", {"provider": direct.get("memory_provider")})
        result.setdefault("budget", {"daily_tokens": limits.get("daily_tokens", 0), "daily_calls": 0, "daily_prompt_tokens": 0})
        result.setdefault("circuit", {"failure_threshold": limits.get("circuit_threshold", 3), "cooldown_seconds": limits.get("circuit_cooldown_s", 1800)})
        result.setdefault("resources", {"memory_max_mb": _memory_limit_mb(limits.get("memory_max", 1024)), "quiet_seconds": direct.get("quiet_s", 300), "max_run_seconds": 0})
        return result
    plugins = raw_cfg.get("plugins")
    if isinstance(plugins, dict):
        entries = plugins.get("entries")
        if isinstance(entries, dict):
            entry = entries.get("autonomy")
            if isinstance(entry, dict) and isinstance(entry.get("settings"), dict):
                return dict(entry["settings"])
    return {}


def _resolve_provider_details(
    raw_cfg: Dict[str, Any], provider_str: str
) -> Tuple[str, str, str]:
    """Return (provider_key, api_base_url, api_key_env) for a provider string."""
    prov = (provider_str or "").strip()
    short_key = prov.split(":", 1)[1] if prov.startswith("custom:") else prov
    providers = raw_cfg.get("providers") or {}
    if isinstance(providers, dict):
        entry = providers.get(short_key) or providers.get(prov)
        if isinstance(entry, dict):
            return (
                prov or short_key,
                str(entry.get("api") or entry.get("base_url") or "").strip(),
                str(entry.get("key_env") or entry.get("api_key_env") or "").strip(),
            )
    custom_providers = raw_cfg.get("custom_providers") or []
    if isinstance(custom_providers, list):
        for entry in custom_providers:
            if not isinstance(entry, dict) or str(entry.get("name") or "").strip() not in {prov, short_key}:
                continue
            return (
                str(entry.get("name") or prov or short_key).strip(),
                str(entry.get("api") or entry.get("base_url") or "").strip(),
                str(entry.get("key_env") or entry.get("api_key_env") or "").strip(),
            )
    model_sec = raw_cfg.get("model") or {}
    if isinstance(model_sec, dict):
        return (
            prov,
            str(model_sec.get("base_url") or "").strip(),
            str(model_sec.get("api_key_env") or "").strip(),
        )
    return prov, "", ""


def resolve_main_model(raw_cfg: Dict[str, Any]) -> Dict[str, Any]:
    model_sec = raw_cfg.get("model")
    if isinstance(model_sec, str):
        model_id = model_sec.strip()
        provider = ""
        base_url = ""
        api_key_env = ""
    elif isinstance(model_sec, dict):
        model_id = str(model_sec.get("default") or model_sec.get("id") or model_sec.get("name") or "").strip()
        provider = str(model_sec.get("provider") or "").strip()
        _, base_url, api_key_env = _resolve_provider_details(raw_cfg, provider)
        if model_sec.get("base_url"):
            base_url = str(model_sec["base_url"]).strip()
        if model_sec.get("api_key_env"):
            api_key_env = str(model_sec["api_key_env"]).strip()
    else:
        model_id, provider, base_url, api_key_env = "", "", "", ""
    return {
        "model": model_id,
        "provider": provider,
        "base_url": base_url,
        "api_key_env": api_key_env,
    }


def resolve_autonomy_model(raw_cfg: Dict[str, Any]) -> Dict[str, Any]:
    main = resolve_main_model(raw_cfg)
    auto_sec = get_autonomy_section(raw_cfg)
    m = auto_sec.get("model")
    if not m:
        return {**main, "inherit": True}
    if isinstance(m, str):
        m_id = m.strip()
        if not m_id or m_id == main["model"]:
            return {**main, "inherit": True}
        return {
            "model": m_id,
            "provider": main["provider"],
            "base_url": main["base_url"],
            "api_key_env": main["api_key_env"],
            "inherit": False,
        }
    if isinstance(m, dict):
        inherit = bool(m.get("inherit", False))
        m_id = str(m.get("default") or m.get("model") or m.get("id") or m.get("name") or "").strip()
        if inherit or not m_id:
            return {**main, "inherit": True}
        prov = str(m.get("provider") or main["provider"] or "").strip()
        _, prov_url, prov_key_env = _resolve_provider_details(raw_cfg, prov)
        base_url = str(m.get("base_url") or m.get("api") or prov_url or main["base_url"] or "").strip()
        api_key_env = str(m.get("api_key_env") or m.get("key_env") or prov_key_env or main["api_key_env"] or "").strip()
        return {
            "model": m_id,
            "provider": prov,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "inherit": False,
        }
    return {**main, "inherit": True}


def determine_mode(raw_cfg: Optional[Dict[str, Any]] = None, hermes_home: Optional[str | Path] = None) -> str:
    """Auto-detect conversation/thinking mode:
    - Two different models (autonomy model != Hermes main model) -> 'parallel' (conversation does not interrupt thinking)
    - Single shared model (inherit or identical model) -> 'dialog_first' (user chat pauses thinking via PREEMPT_NOW/LAST_USER_ACTIVITY)
    """
    from .mode import MODE_DIALOG_FIRST, MODE_PARALLEL, decide_mode
    env_mode = os.environ.get("AUTONOMY_MODE", "").strip().lower()
    if env_mode in (MODE_PARALLEL, MODE_DIALOG_FIRST):
        return env_mode
    cfg = raw_cfg if raw_cfg is not None else load_raw_config(hermes_home)
    main = resolve_main_model(cfg)
    auto = resolve_autonomy_model(cfg)
    return decide_mode(main.get("model"), None if auto.get("inherit", True) else auto.get("model"))


def resolve_paths(
    raw_cfg: Optional[Dict[str, Any]] = None,
    hermes_home: Optional[str | Path] = None,
) -> Dict[str, str]:
    home = resolve_hermes_home(hermes_home)
    base = resolve_base_dir(home)
    cfg = raw_cfg if raw_cfg is not None else load_raw_config(home)
    auto = get_autonomy_section(cfg)
    paths_cfg = auto.get("paths") if isinstance(auto.get("paths"), dict) else {}

    data_dir = Path(
        os.environ.get("AUTONOMY_DATA_DIR")
        or auto.get("data_dir")
        or paths_cfg.get("data_dir")
        or (base / "autonomy_data")
    )
    default_db = (
        data_dir / "autonomy.db"
        if (data_dir / "autonomy.db").exists()
        else (base / "autonomy" / "autonomy.db")
    )
    db_path = Path(
        os.environ.get("AUTONOMY_DB_PATH")
        or paths_cfg.get("db_path")
        or default_db
    )
    default_stop = (
        data_dir / "STOP_AUTONOMY"
        if ((data_dir / "STOP_AUTONOMY").exists() or (data_dir / "autonomy.db").exists())
        else (base / "STOP_AUTONOMY")
    )
    stop_file = Path(
        os.environ.get("AUTONOMY_STOP_FILE")
        or paths_cfg.get("stop_file")
        or default_stop
    )
    activity_file = Path(
        os.environ.get("AUTONOMY_ACTIVITY_FILE")
        or paths_cfg.get("activity_file")
        or (data_dir / "LAST_USER_ACTIVITY")
    )
    lock_path = Path(
        os.environ.get("AUTONOMY_LOCK_PATH")
        or paths_cfg.get("lock_path")
        or (str(db_path) + ".run.lock")
    )
    preempt_file = Path(
        os.environ.get("AUTONOMY_PREEMPT_FILE")
        or paths_cfg.get("preempt_file")
        or (lock_path.parent / "PREEMPT_NOW")
    )
    pending_resume = Path(
        os.environ.get("AUTONOMY_PENDING_RESUME")
        or paths_cfg.get("pending_resume")
        or (stop_file.parent / "PENDING_RESUME")
    )
    thought_log = Path(
        os.environ.get("AUTONOMY_THOUGHT_LOG")
        or auto.get("thought_log")
        or paths_cfg.get("thought_log")
        or (data_dir / "thoughts.log")
    )
    return {
        "hermes_home": str(home),
        "base_dir": str(base),
        "data_dir": str(data_dir),
        "db_path": str(db_path),
        "stop_file": str(stop_file),
        "activity_file": str(activity_file),
        "lock_path": str(lock_path),
        "preempt_file": str(preempt_file),
        "pending_resume": str(pending_resume),
        "thought_log": str(thought_log),
    }


def record_user_activity(
    session_id: str = "",
    *,
    raw_cfg: Optional[Dict[str, Any]] = None,
    hermes_home: Optional[str | Path] = None,
    activity_file: Optional[str | Path] = None,
    preempt_file: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Called by plugin hooks when a conversation event occurs.
    Ignores autonomy_* sessions.
    Updates LAST_USER_ACTIVITY, and in dialog_first (single-model) mode also writes PREEMPT_NOW.
    """
    sid = str(session_id or "").strip()
    if sid.startswith("autonomy_"):
        return {"recorded": False, "reason": "autonomy_session"}

    cfg = raw_cfg if raw_cfg is not None else load_raw_config(hermes_home)
    mode = determine_mode(cfg, hermes_home=hermes_home)
    paths = resolve_paths(cfg, hermes_home=hermes_home)

    act_path = Path(activity_file or paths["activity_file"])
    pre_path = Path(preempt_file or paths["preempt_file"])
    now = time.time()
    now_ns = time.time_ns()

    act_path.parent.mkdir(parents=True, exist_ok=True)
    act_path.write_text(f"{now:.6f}\n", encoding="utf-8")
    os.utime(act_path, ns=(now_ns, now_ns))

    preempted = False
    if mode == "dialog_first":
        pre_path.parent.mkdir(parents=True, exist_ok=True)
        pre_path.write_text(f"{now_ns}\n", encoding="utf-8")
        os.utime(pre_path, ns=(now_ns, now_ns))
        preempted = True

    return {
        "recorded": True,
        "mode": mode,
        "preempted": preempted,
        "activity_file": str(act_path),
        "preempt_file": str(pre_path) if preempted else None,
    }
