from __future__ import annotations
import argparse, os, sys, time, datetime
from pathlib import Path
from .db import AutonomyDB
from .budget import Budget
from .circuit import Circuit
from .runner import Runner
from .gateway_client import GatewayClient
from .config import (
    load_raw_config,
    get_autonomy_section,
    resolve_autonomy_model,
    determine_mode,
    resolve_paths,
)

# These outcomes are expected scheduler decisions, not service failures.
NORMAL_STATUSES = {
    "ok",
    "stopped",
    "blocked",
    "busy",
    "preempted",
    "run_timeout",
    "yielded_for_memory",
}


def is_normal_status(status):
    return status in NORMAL_STATUSES or str(status).startswith("yielded_")


def main(argv=None, *, hermes_home=None):
    hermes_home = hermes_home or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume-if-idle", action="store_true")
    p.add_argument("--hermes-home", default=hermes_home)
    a = p.parse_args(argv)

    raw_cfg = load_raw_config(a.hermes_home)
    auto_cfg = get_autonomy_section(raw_cfg)
    paths = resolve_paths(raw_cfg, hermes_home=a.hermes_home)
    mode = determine_mode(raw_cfg, hermes_home=a.hermes_home)
    auto_model = resolve_autonomy_model(raw_cfg)

    base = os.environ.get("AUTONOMY_DB_PATH") or paths["db_path"]
    stop = os.environ.get("AUTONOMY_STOP_FILE") or paths["stop_file"]
    db = AutonomyDB(base)
    if a.dry_run:
        print("dry_run db=%s stop=%s mode=%s" % (base, stop, mode))
        return 0

    activity = os.environ.get("AUTONOMY_ACTIVITY_FILE") or paths["activity_file"]
    res_cfg = auto_cfg.get("resources") if isinstance(auto_cfg.get("resources"), dict) else {}
    limits_cfg = auto_cfg.get("limits") if isinstance(auto_cfg.get("limits"), dict) else {}
    # New limits schema wins; retain old resources keys for read-only compatibility.
    prefetch_max_chars = int(os.environ.get("AUTONOMY_PREFETCH_MAX_CHARS") or limits_cfg.get("prefetch_max_chars") or 3000)
    prefetch_timeout_s = float(os.environ.get("AUTONOMY_PREFETCH_TIMEOUT_S") or limits_cfg.get("prefetch_timeout_s") or 20)
    run_max_seconds = float(os.environ.get("AUTONOMY_RUN_MAX_SECONDS") or limits_cfg.get("run_max_seconds") or 2400)
    run_soft_wrap_seconds = float(os.environ.get("AUTONOMY_RUN_SOFT_WRAP_SECONDS") or limits_cfg.get("run_soft_wrap_seconds") or 1080)
    min_quiet = float(
        os.environ.get(
            "AUTONOMY_MIN_QUIET_START",
            res_cfg.get("min_quiet_start_seconds", 1800),
        )
    )
    quiet_s = float(res_cfg.get("quiet_seconds", 300))

    # In chat_priority (single shared model) mode, yield if user was recently active.
    # In parallel (two separate models) mode, user chat does NOT block autonomous thinking.
    if mode != "parallel":
        if os.path.exists(activity) and time.time() - os.path.getmtime(activity) < min_quiet:
            print({"status": "yielded_user_recent", "mode": mode}, flush=True)
            return 0

    hours = os.environ.get("AUTONOMY_ACTIVE_HOURS", "")
    if hours:
        h = datetime.datetime.now().hour
        if not any(int(x_a) <= h < int(x_b) for x in hours.split(",") for x_a, x_b in [x.split("-", 1)]):
            print({"status": "yielded_outside_hours"}, flush=True)
            return 0

    limits_cfg = auto_cfg.get("limits") if isinstance(auto_cfg.get("limits"), dict) else {}
    budget_cfg = dict(auto_cfg.get("budget") if isinstance(auto_cfg.get("budget"), dict) else {})
    budget_cfg.update({k: v for k, v in limits_cfg.items() if k in ("daily_tokens", "daily_calls", "daily_prompt_tokens")})
    circuit_cfg = auto_cfg.get("circuit") if isinstance(auto_cfg.get("circuit"), dict) else {}

    budget = Budget(
        db,
        calls=os.environ.get("AUTONOMY_DAILY_CALLS") or budget_cfg.get("daily_calls"),
        tokens=os.environ.get("AUTONOMY_DAILY_TOKENS") or budget_cfg.get("daily_tokens"),
        prompt=os.environ.get("AUTONOMY_DAILY_PROMPT_TOKENS") or budget_cfg.get("daily_prompt_tokens"),
    )
    circuit = Circuit(
        db,
        threshold=int(
            os.environ.get("AUTONOMY_CIRCUIT_THRESHOLD")
            or circuit_cfg.get("failure_threshold")
            or 5
        ),
        cooldown_s=os.environ.get("AUTONOMY_CIRCUIT_COOLDOWN_S") or circuit_cfg.get("cooldown_seconds"),
    )

    gw_model = None if auto_model.get("inherit", True) else auto_model.get("model")
    gw_provider = None if auto_model.get("inherit", True) else auto_model.get("provider")
    transport = str(auto_cfg.get("transport") or "auto").lower()
    gw = GatewayClient(model=gw_model, provider=gw_provider)
    if transport == "cli":
        from .cli_transport import CliGateway
        gw = CliGateway(max_turns=int(limits_cfg.get("max_turns") or 30), run_budget=int(run_max_seconds), profile=Path(hermes_home).name)
    elif transport == "auto":
        try:
            gw._req("GET", "/health")
        except Exception:
            from .cli_transport import CliGateway
            gw = CliGateway(max_turns=int(limits_cfg.get("max_turns") or 30), run_budget=int(run_max_seconds), profile=Path(hermes_home).name)

    r = Runner(
        db,
        budget,
        circuit,
        stop_file=stop,
        gateway=gw,
        lock_path=paths["lock_path"],
        activity_file=activity,
        quiet_s=quiet_s,
        mode=mode,
        thought_log_path=paths["thought_log"],
        memory_max_mb=res_cfg.get("memory_max_mb"),
        model_name=auto_model.get("model", ""),
        memory_provider=(auto_cfg.get("memory", {}).get("provider") if isinstance(auto_cfg.get("memory"), dict) else None) or auto_cfg.get("memory_provider"),
        hermes_home=hermes_home,
        prefetch_max_chars=prefetch_max_chars,
        prefetch_timeout_s=prefetch_timeout_s,
        run_max_seconds=run_max_seconds,
        run_soft_wrap_seconds=run_soft_wrap_seconds,
        workspace=auto_cfg.get("workspace") if isinstance(auto_cfg.get("workspace"), str) else None,
        worker_timeout_s=int(limits_cfg.get("worker_timeout_s", 360) or 360),
        seed_queue_dir=((raw_cfg.get("memory") or {}).get("seed_memory") or {}).get("queue_dir"),
    )

    if a.resume_if_idle:
        if not os.path.exists(r.pending_resume):
            print({"status": "nothing_to_resume"}, flush=True)
            return 0
        if mode != "parallel" and os.path.exists(activity) and time.time() - os.path.getmtime(activity) < min_quiet:
            print({"status": "user_still_active"}, flush=True)
            return 0
        try:
            os.unlink(r.pending_resume)
        except FileNotFoundError:
            pass

    out = r.tick()
    print(out, flush=True)
    return 0 if is_normal_status(out.get("status")) else 1


if __name__ == "__main__":
    sys.exit(main())
