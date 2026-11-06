"""Configuration loader, mode detector, and user-activity recorder for the autonomy plugin."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

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


@dataclass(frozen=True)
class P0Config:
    """Restart-scoped P0 thresholds. Mutations require a proposal and restart."""
    validator_hashes: Mapping[str, str] = field(default_factory=dict)
    buffer_ttl_s: int = 24 * 3600
    evidence_deadline_s: int = 6 * 3600
    per_call_buffer_bytes: int = 5 * 1024**2
    permanent_evidence_bytes: int = 1536
    pool_cap_bytes: int = 10 * 1024**3
    pool_warn_ratio: float = 0.80
    pool_freeze_ratio: float = 0.95
    t2_free_bytes: int = 1024**3
    evaluation_window_s: int = 7 * 86400
    normal_share: float = 0.80
    memory_share: float = 0.10
    evolution_share: float = 0.10

    def __post_init__(self) -> None:
        object.__setattr__(self, "validator_hashes", MappingProxyType(dict(self.validator_hashes)))
        if abs(self.normal_share + self.memory_share + self.evolution_share - 1.0) > 1e-9:
            raise ValueError("budget shares must sum to 1")
        for value in self.validator_hashes.values():
            if not re.fullmatch(r"[0-9a-f]{64}", str(value)):
                raise ValueError("validator hashes must be lowercase sha256")

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]] = None) -> "P0Config":
        from dataclasses import fields
        values = dict(values or {})
        allowed = {f.name for f in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown P0 config keys: {sorted(unknown)}")
        return cls(**values)

    @staticmethod
    def t1_free_bytes(filesystem_size: int) -> int:
        return max(2 * 1024**3, min(int(filesystem_size * 0.05), 10 * 1024**3))

    @staticmethod
    def pool_cap_for_startup_free_space(free_bytes: int) -> int:
        return min(10 * 1024**3, int(free_bytes * 0.10))


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


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")


def _reflection_profile_paths(hermes_home, source_profile, name):
    """Mirror `hermes -p NAME`: named profiles live below ROOT/profiles."""
    home = Path(hermes_home).expanduser().absolute()
    if home.parent.name.lower() == "profiles":
        return home, home.parent / name
    profiles_root = home / "profiles"
    candidate = profiles_root / str(source_profile or "")
    return (candidate if candidate.exists() else home), profiles_root / name


def validate_reflection_profile(
    hermes_home: str | Path, source_profile: str, reflection_profile: str | None
) -> Tuple[bool, str, Optional[Path]]:
    """Fail closed unless a separate profile explicitly proves it has no tools.

    Live Hermes resolver measurements show that an explicit empty CLI toolset
    suppresses built-ins, plugins, and portable MCP.  XAI credentials are the
    exception: they can auto-inject ``x_search``, so it must also be explicitly
    disabled under ``agent.disabled_toolsets``.
    """
    name = str(reflection_profile or "").strip()
    source_name = str(source_profile or "").strip()
    if not name:
        return False, "profile_not_configured", None
    if not _PROFILE_NAME_RE.fullmatch(name) or name.startswith("-"):
        return False, "profile_name_invalid", None
    if name.casefold() == source_name.casefold():
        return False, "profile_not_isolated", None
    source_home, profile_home = _reflection_profile_paths(hermes_home, source_profile, name)
    cfg_path, plugins_dir = profile_home / "config.yaml", profile_home / "plugins"
    if profile_home.is_symlink():
        return False, "profile_symlink", profile_home
    if cfg_path.is_symlink():
        return False, "config_symlink", profile_home
    if plugins_dir.is_symlink():
        return False, "plugins_symlink", profile_home
    if not profile_home.is_dir() or not cfg_path.is_file():
        return False, "profile_unreadable", profile_home
    try:
        resolved_profile, resolved_source = profile_home.resolve(strict=True), source_home.resolve(strict=True)
        if resolved_profile == resolved_source or os.path.samefile(resolved_profile, resolved_source):
            return False, "profile_not_isolated", profile_home
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return False, "profile_unreadable", profile_home
    if not isinstance(raw, dict):
        return False, "config_not_mapping", profile_home
    toolsets = raw.get("platform_toolsets")
    if not isinstance(toolsets, dict) or "cli" not in toolsets or toolsets.get("cli") != []:
        return False, "toolsets_not_explicitly_empty", profile_home
    agent = raw.get("agent")
    if not isinstance(agent, dict):
        return False, "agent_not_mapping", profile_home
    disabled_toolsets = agent.get("disabled_toolsets")
    if not isinstance(disabled_toolsets, list) or "x_search" not in disabled_toolsets:
        return False, "x_search_not_disabled", profile_home
    if raw.get("mcp_servers") not in (None, False, {}, []):
        return False, "mcp_servers_configured", profile_home
    if raw.get("memory") not in (None, False, {}, []):
        return False, "memory_configured", profile_home
    if raw.get("context_engine") not in (None, False, {}, []):
        return False, "context_engine_enabled", profile_home
    context = raw.get("context")
    if context not in (None, False, {}, []):
        if not isinstance(context, dict) or context.get("engine") not in (None, False, {}, []):
            return False, "context_engine_enabled", profile_home
    if raw.get("plugins") not in (None, False, {}, []) or raw.get("enabled_plugins") not in (None, False, {}, []):
        return False, "plugins_configured", profile_home
    try:
        if plugins_dir.exists() and (not plugins_dir.is_dir() or any(plugins_dir.iterdir())):
            return False, "plugin_directory_not_empty", profile_home
    except OSError:
        return False, "plugin_directory_unreadable", profile_home
    return True, "", profile_home


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
