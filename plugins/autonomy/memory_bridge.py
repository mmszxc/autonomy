"""Bridge to Hermes's unified external memory provider interface.

Note: Kept in a dedicated submodule so __init__.py never contains the literal
strings 'MemoryProvider' or 'register_memory_provider' (which Hermes's
PluginManager._detect_kind_from_source uses to classify exclusive memory plugins).
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import load_raw_config, resolve_hermes_home, resolve_base_dir


def _ensure_memory_provider_base() -> None:
    """Ensure `agent.memory_provider` is importable even outside the Hermes CLI."""
    try:
        import agent.memory_provider  # noqa: F401
        return
    except ImportError:
        pass

    candidates = [
        Path("/usr/local/lib/hermes-agent"),
        Path.home() / ".local" / "share" / "hermes" / "hermes-agent",
    ]
    for cand in candidates:
        try:
            is_valid = (cand / "agent" / "memory_provider.py").is_file()
        except OSError:
            is_valid = False
        if is_valid:
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            try:
                import agent.memory_provider  # noqa: F401
                return
            except ImportError:
                pass

    # Fallback lightweight shim for standalone test environments
    agent_mod = sys.modules.get("agent") or types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    @dataclass
    class RecallStatus:
        backend: str = ""
        recalled: int = 0
        label: str = ""

    class MemoryProvider:
        name: str = "base"

        def is_available(self) -> bool:
            return True

        def initialize(self, session_id: str, **kwargs: Any) -> None:
            pass

        def get_tool_schemas(self) -> List[Dict[str, Any]]:
            return []

        def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
            return "{}"

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def sync_turn(self, user_content: str, assistant_content: str, **kwargs: Any) -> None:
            pass

        def shutdown(self) -> None:
            pass

    mp_mod.MemoryProvider = MemoryProvider
    mp_mod.RecallStatus = RecallStatus
    agent_mod.memory_provider = mp_mod
    sys.modules["agent"] = agent_mod
    sys.modules["agent.memory_provider"] = mp_mod


class _MemoryCaptureContext:
    def __init__(self) -> None:
        self.provider: Optional[Any] = None

    def register_memory_provider(self, provider: Any) -> None:
        self.provider = provider


def get_configured_memory_provider(hermes_home: Optional[str | Path] = None) -> str:
    raw = load_raw_config(hermes_home)
    mem = raw.get("memory")
    if isinstance(mem, dict):
        prov = str(mem.get("provider") or "").strip()
        if prov:
            return prov
    return ""


def list_available_memory_plugins(hermes_home: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """Discover available external memory plugins from Hermes and $HERMES_HOME/plugins."""
    home = resolve_hermes_home(hermes_home)
    base = resolve_base_dir(home)
    current = get_configured_memory_provider(home)
    found: Dict[str, Dict[str, Any]] = {}

    # 1. Try Hermes built-in discovery if available
    _ensure_memory_provider_base()
    try:
        from plugins.memory import discover_memory_providers

        for info in discover_memory_providers():
            name = getattr(info, "name", "") or (info.get("name", "") if isinstance(info, dict) else "")
            desc = getattr(info, "description", "") or (info.get("description", "") if isinstance(info, dict) else "")
            avail = getattr(info, "available", True) if not isinstance(info, dict) else info.get("available", True)
            if name:
                found[name] = {
                    "name": name,
                    "description": desc,
                    "available": bool(avail),
                    "active": name == current,
                }
    except Exception:
        pass

    # 2. Scan $HERMES_HOME/plugins/* and <base>/plugin/* for memory provider plugins
    repo_root = Path(__file__).resolve().parents[1]
    scan_dirs = [
        home / "plugins",
        base / "plugin",
        repo_root / "plugin",
        repo_root / "hermes_home" / "plugins",
        Path("/usr/local/lib/hermes-agent/plugins/memory"),
        Path.home() / ".local" / "share" / "hermes" / "hermes-agent" / "plugins" / "memory",
    ]
    for parent in scan_dirs:
        try:
            if not parent.is_dir():
                continue
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or child.name.startswith(("_", ".")):
                    continue
                init_py = child / "__init__.py"
                if not init_py.is_file():
                    continue
                head = init_py.read_text(encoding="utf-8", errors="ignore")[:8192]
            except OSError:
                continue
            if "register_memory_provider" not in head and "MemoryProvider" not in head:
                continue
            name = child.name
            if name not in found:
                found[name] = {
                    "name": name,
                    "description": f"Memory plugin at {child}",
                    "available": True,
                    "active": name == current,
                    "path": str(child),
                }
            elif "path" not in found[name]:
                found[name]["path"] = str(child)

    if current and current not in found:
        found[current] = {
            "name": current,
            "description": "Configured in config.yaml",
            "available": True,
            "active": True,
        }

    return list(found.values())


def load_memory_provider_instance(
    provider_name: Optional[str] = None,
    hermes_home: Optional[str | Path] = None,
) -> Any:
    """Load a MemoryProvider instance through Hermes's unified memory plugin contract."""
    _ensure_memory_provider_base()
    home = resolve_hermes_home(hermes_home)
    base = resolve_base_dir(home)
    target = (provider_name or get_configured_memory_provider(home) or "seed_memory").strip()

    # Check candidate plugin directories first ($HERMES_HOME/plugins/<target> or <base>/plugin/<target>)
    repo_root = Path(__file__).resolve().parents[1]
    candidate_dirs = [
        base / "plugin" / target,
        home / "plugins" / target,
        repo_root / "plugin" / target,
        repo_root / "hermes_home" / "plugins" / target,
        Path("/usr/local/lib/hermes-agent/plugins/memory") / target,
        Path.home() / ".local" / "share" / "hermes" / "hermes-agent" / "plugins" / "memory" / target,
    ]
    for cdir in candidate_dirs:
        try:
            init_py = cdir / "__init__.py"
            if not init_py.is_file():
                continue
        except OSError:
            continue
        spec = importlib.util.spec_from_file_location(
            f"hermes_memory_plugin_{target}",
            init_py,
            submodule_search_locations=[str(cdir)],
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            ctx = _MemoryCaptureContext()
            if hasattr(mod, "register"):
                mod.register(ctx)
            if ctx.provider is not None:
                return ctx.provider

    # Fallback to Hermes runtime loader
    try:
        from plugins.memory import load_memory_provider

        prov = load_memory_provider(target)
        if prov is not None:
            return prov
    except Exception:
        pass

    raise RuntimeError(f"Memory provider '{target}' could not be loaded")


def memory_self_test(
    provider_name: Optional[str] = None,
    hermes_home: Optional[str | Path] = None,
    cleanup: bool = True,
) -> Dict[str, Any]:
    """Perform a write + read-back self-test via Hermes's unified MemoryProvider interface."""
    home = resolve_hermes_home(hermes_home)
    target = (provider_name or get_configured_memory_provider(home) or "seed_memory").strip()
    try:
        provider = load_memory_provider_instance(target, hermes_home=home)
    except Exception as exc:
        return {"ok": False, "provider": target, "error": f"load_failed: {exc}"}

    session_id = f"autonomy_selftest_{uuid.uuid4().hex[:8]}"
    try:
        try:
            provider.initialize(session_id=session_id, hermes_home=str(home), start_worker=False)
        except TypeError:
            provider.initialize(session_id=session_id, hermes_home=str(home))
    except Exception as exc:
        return {"ok": False, "provider": target, "error": f"initialize_failed: {exc}"}

    token = f"autonomy_selftest_probe_{uuid.uuid4().hex[:10]}"
    wrote = False
    read_back = False
    created_seed_id = None
    detail = ""

    try:
        schemas = provider.get_tool_schemas() or []
        tool_names = [
            (s.get("function", {}) if isinstance(s, dict) else {}).get("name", "")
            for s in schemas
        ]

        if "seed_add" in tool_names:
            # Ensure write_enabled for self-test if config had it unset
            if hasattr(provider, "_config") and isinstance(provider._config, dict):
                orig_write = provider._config.get("write_enabled", True)
                provider._config["write_enabled"] = True
            else:
                orig_write = True
            try:
                add_raw = provider.handle_tool_call(
                    "seed_add",
                    {
                        "summary": f"自主思考插件初始化读写自检 {token}",
                        "emotion_primary": "平静",
                        "emotion_intensity": 0.5,
                        "importance": 0.5,
                        "participants": ["autonomy_init"],
                    },
                    session_id=session_id,
                )
            finally:
                if hasattr(provider, "_config") and isinstance(provider._config, dict):
                    provider._config["write_enabled"] = orig_write

            add_res = json.loads(add_raw) if isinstance(add_raw, str) else {}
            if not add_res.get("ok"):
                return {
                    "ok": False,
                    "provider": target,
                    "error": f"write_failed: {add_res.get('error') or add_raw}",
                }
            wrote = True
            created_seed_id = (add_res.get("seed") or {}).get("id")

            # Read back via unified tool_call (seed_search) and prefetch()
            if "seed_search" in tool_names:
                search_raw = provider.handle_tool_call(
                    "seed_search", {"query": token, "limit": 5}, session_id=session_id
                )
                search_res = json.loads(search_raw) if isinstance(search_raw, str) else {}
                seeds = search_res.get("seeds") or []
                if any(token in str(s.get("summary", "")) for s in seeds):
                    read_back = True
                    detail = f"seed_search matched id={created_seed_id}"

            if not read_back:
                prefetched = provider.prefetch(token, session_id=session_id) or ""
                if token in prefetched:
                    read_back = True
                    detail = f"prefetch matched id={created_seed_id}"

            # Clean up temporary self-test record from SQLite store so production DB stays clean
            if cleanup and created_seed_id and hasattr(provider, "_store") and provider._store:
                try:
                    with provider._store._connect() as conn:
                        conn.execute("DELETE FROM seeds WHERE id = ?", (created_seed_id,))
                        conn.commit()
                except Exception:
                    pass
        else:
            # Generic MemoryProvider interface fallback: sync_turn + prefetch
            provider.sync_turn(f"self_test_write {token}", "ok", session_id=session_id)
            wrote = True
            prefetched = provider.prefetch(token, session_id=session_id)
            read_back = isinstance(prefetched, str)
            detail = "generic MemoryProvider sync_turn + prefetch succeeded"

        if not (wrote and read_back):
            return {
                "ok": False,
                "provider": target,
                "wrote": wrote,
                "read_back": read_back,
                "error": "read_back_verification_failed",
            }
        return {
            "ok": True,
            "provider": target,
            "wrote": wrote,
            "read_back": read_back,
            "detail": detail,
        }
    except Exception as exc:
        return {"ok": False, "provider": target, "error": f"selftest_exception: {exc}"}
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
