"""Hermes standalone plugin for autonomous thinking / solitude loop (autonomy).

Provides:
- CLI commands: `hermes autonomy init | stop | resume | status | tick`
- Slash command: `/autonomy status | stop | resume`
- Conversation hooks: `pre_gateway_dispatch`, `pre_llm_call`, `on_session_start`
  to automatically record user conversation activity (LAST_USER_ACTIVITY and PREEMPT_NOW
  in single-model dialog_first mode) without relying on an external web UI.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict

_plugins_dir = Path(__file__).resolve().parent.parent
for _vendor_dir in (_plugins_dir / "_vendor_ml", Path(__file__).resolve().parent / "_vendor"):
    if _vendor_dir.is_dir() and str(_vendor_dir) not in sys.path:
        sys.path.insert(0, str(_vendor_dir))

# Preserve the loader-provided package name.  Some portable runtimes load
# __init__.py as a package with an arbitrary name; hard-coding hermes_home
# breaks those runtimes.  Only flat file loaders need a synthetic package.
_is_flat_loader = not __package__ or not getattr(globals().get("__spec__"), "submodule_search_locations", None)
if _is_flat_loader:
    _plugin_dir = Path(__file__).resolve().parent
    # The repository's test harness uses a deliberately flat spec; retain its
    # historical package alias. Real portable loaders provide a package name.
    if str(__name__).startswith("autonomy_plugin_stage"):
        _root = _plugin_dir.parents[3]
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        __package__ = "hermes_home.plugins.autonomy"
        __spec__ = None
    else:
        _package_name = str(__name__) or "autonomy_plugin_pkg"
        _pkg = sys.modules.get(_package_name) or types.ModuleType(_package_name)
        _pkg.__path__ = [str(_plugin_dir)]
        _pkg.__file__ = str(_plugin_dir / "__init__.py")
        sys.modules[_package_name] = _pkg
        __package__ = _package_name

if str(__name__).startswith("autonomy_plugin_stage"):
    from hermes_home.plugins.autonomy.cli import cmd_resume, cmd_status, cmd_stop, handle_cli, setup_cli_parser
    from hermes_home.plugins.autonomy.config import (
        determine_mode, get_autonomy_section, load_raw_config, record_user_activity,
    )
else:
    from .cli import cmd_resume, cmd_status, cmd_stop, handle_cli, setup_cli_parser
    from .config import (
        determine_mode,
        get_autonomy_section,
        load_raw_config,
        record_user_activity,
    )


def _extract_session_id(kwargs: Dict[str, Any]) -> str:
    for key in ("session_id", "task_id", "session_key", "chat_id"):
        val = kwargs.get(key)
        if val:
            return str(val)
    return ""


def _on_conversation_activity(**kwargs: Any) -> None:
    """Hook callback fired when a user message or LLM turn starts in Hermes."""
    sid = _extract_session_id(kwargs)
    try:
        record_user_activity(session_id=sid)
    except Exception:
        pass


def _handle_slash_command(args_str: str = "", **kwargs: Any) -> str:
    sub = (args_str or "").strip().split()[0] if (args_str or "").strip() else "status"
    if sub == "stop":
        res = cmd_stop()
        return f"已停止自主思考 (STOP_AUTONOMY: {res['stop_file']})"
    if sub == "resume":
        res = cmd_resume()
        return f"已恢复自主思考 (已删除: {res['stop_file']})"
    res = cmd_status()
    return (
        f"状态: {res['state']} | 模式: {res['mode']} | "
        f"模型: {res['autonomy_model']} (主模型: {res['main_model']}) | "
        f"预算: {res['budget']['tokens']}/{res['budget']['tokens_limit']} tokens | "
        f"熔断: {res['circuit']['state']} ({res['circuit']['failures']}/{res['circuit']['threshold']})"
    )


def register(ctx: Any) -> None:
    """Standard Hermes plugin entry point."""
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(
            name="autonomy",
            help="Manage autonomous thinking loop (init / stop / resume / status / tick)",
            setup_fn=setup_cli_parser,
            handler_fn=handle_cli,
            description="Standalone autonomous thinking plugin for Hermes Agent",
        )

    if hasattr(ctx, "register_command"):
        ctx.register_command(
            name="autonomy",
            handler=_handle_slash_command,
            description="Check or toggle autonomous thinking (/autonomy status|stop|resume)",
        )

    if hasattr(ctx, "register_hook"):
        for hook_name in ("pre_gateway_dispatch", "pre_llm_call", "on_session_start"):
            try:
                ctx.register_hook(hook_name, _on_conversation_activity)
            except Exception:
                pass

    # Optional in-process scheduler daemon thread if explicitly enabled in config
    try:
        raw_cfg = load_raw_config()
        auto_cfg = get_autonomy_section(raw_cfg)
        if bool(auto_cfg.get("in_process_loop", False)):
            interval_s = float(auto_cfg.get("interval_seconds", 600))

            def _daemon_loop() -> None:
                from .tick import main as tick_main

                while True:
                    time.sleep(interval_s)
                    try:
                        tick_main([])
                    except Exception:
                        pass

            t = threading.Thread(target=_daemon_loop, name="hermes-autonomy-loop", daemon=True)
            t.start()
    except Exception:
        pass


__all__ = [
    "register",
    "determine_mode",
    "record_user_activity",
    "cmd_stop",
    "cmd_resume",
    "cmd_status",
]
