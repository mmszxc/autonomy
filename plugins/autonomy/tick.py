from __future__ import annotations
import argparse, os, sys, time, datetime
from pathlib import Path
from .db import AutonomyDB
from .budget import Budget, BudgetLedger
from .event_log import EventLog
from .circuit import Circuit
from .runner import Runner
from .gateway_client import GatewayClient
from .config import (
    load_raw_config,
    resolve_p0_mode,
    get_autonomy_section,
    resolve_autonomy_model,
    determine_mode,
    resolve_paths,
    validate_reflection_profile,
    P0Config,
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
    "policy_reflect",
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
    p0_mode, p0_mode_reason = resolve_p0_mode(raw_cfg)
    print(
        f"autonomy startup: p0_mode={p0_mode} reason={p0_mode_reason}",
        file=sys.stderr,
        flush=True,
    )
    auto_cfg = get_autonomy_section(raw_cfg)
    paths = resolve_paths(raw_cfg, hermes_home=a.hermes_home)
    mode = determine_mode(raw_cfg, hermes_home=a.hermes_home)
    auto_model = resolve_autonomy_model(raw_cfg)
    reflect_cfg = auto_cfg.get("policy_reflect") if isinstance(auto_cfg.get("policy_reflect"), dict) else {}
    reflection_profile = str(reflect_cfg.get("profile") or "").strip()
    reflection_safe, reflection_reason, _ = validate_reflection_profile(
        a.hermes_home, Path(a.hermes_home).name, reflection_profile
    )

    base = os.environ.get("AUTONOMY_DB_PATH") or paths["db_path"]
    stop = os.environ.get("AUTONOMY_STOP_FILE") or paths["stop_file"]
    db = AutonomyDB(base)
    if a.dry_run:
        print("dry_run db=%s stop=%s mode=%s reflection_profile=%s reflection_safe=%s reason=%s" % (
            base, stop, mode, reflection_profile or "-", reflection_safe, reflection_reason or "-"
        ))
        return 0

    activity = os.environ.get("AUTONOMY_ACTIVITY_FILE") or paths["activity_file"]
    res_cfg = auto_cfg.get("resources") if isinstance(auto_cfg.get("resources"), dict) else {}
    limits_cfg = auto_cfg.get("limits") if isinstance(auto_cfg.get("limits"), dict) else {}
    # New limits schema wins; retain old resources keys for read-only compatibility.
    prefetch_max_chars = int(os.environ.get("AUTONOMY_PREFETCH_MAX_CHARS") or limits_cfg.get("prefetch_max_chars") or 3000)
    prefetch_timeout_s = float(os.environ.get("AUTONOMY_PREFETCH_TIMEOUT_S") or limits_cfg.get("prefetch_timeout_s") or 20)
    run_max_seconds = float(
        os.environ.get('AUTONOMY_RUN_MAX_SECONDS')
        or limits_cfg.get('run_max_seconds')
        or limits_cfg.get('max_seconds')
        or auto_cfg.get('run_max_seconds')
        or auto_cfg.get('max_seconds')
        or 2400
    )
    effective_max_turns = int(limits_cfg.get('max_turns') or auto_cfg.get('max_turns') or 30)
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
    p0_cfg = P0Config.from_mapping(auto_cfg.get("p0") if isinstance(auto_cfg.get("p0"), dict) else {})
    event_log = EventLog(db, challenge_sla_s=p0_cfg.evidence_deadline_s)
    budget_ledger = BudgetLedger(weekly_units=max(1.0, float(budget.calls) * 7.0), db=db)
    circuit = Circuit(
        db,
        threshold=int(
            os.environ.get("AUTONOMY_CIRCUIT_THRESHOLD")
            or circuit_cfg.get("failure_threshold")
            or 5
        ),
        cooldown_s=os.environ.get("AUTONOMY_CIRCUIT_COOLDOWN_S") or circuit_cfg.get("cooldown_seconds"),
    )

    inherit_model = auto_model.get("inherit", True)
    gw_model = None if inherit_model else auto_model.get("model")
    configured_auto_model = auto_cfg.get("model")
    provider_explicit = isinstance(configured_auto_model, dict) and bool(configured_auto_model.get("provider"))
    gw_provider = auto_model.get("provider") if not inherit_model and provider_explicit else None
    transport = str(auto_cfg.get("transport") or "auto").lower()
    gw = GatewayClient(model=gw_model, provider=gw_provider)
    if transport == "cli":
        from .cli_transport import CliGateway
        gw = CliGateway(
            max_turns=effective_max_turns, run_budget=int(run_max_seconds),
            profile=Path(hermes_home).name, model=gw_model, provider=gw_provider,
            raw_buffer_dir=str(Path(paths["data_dir"]) / "evidence_raw"),
            raw_buffer_min_free_bytes=p0_cfg.t2_free_bytes,
        )
    elif transport == "auto":
        try:
            gw._req("GET", "/health")
        except Exception:
            from .cli_transport import CliGateway
            gw = CliGateway(
            max_turns=effective_max_turns, run_budget=int(run_max_seconds),
            profile=Path(hermes_home).name, model=gw_model, provider=gw_provider,
            raw_buffer_dir=str(Path(paths["data_dir"]) / "evidence_raw"),
            raw_buffer_min_free_bytes=p0_cfg.t2_free_bytes,
        )

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
        journal_config=auto_cfg.get("journal") if isinstance(auto_cfg.get("journal"), dict) else {"enabled": True, "window_local": "21:00-23:59", "tz": "Asia/Shanghai", "reflection_max_chars": 800, "inject_max_chars": 400},
        reflection_profile=reflection_profile,
        reflection_profile_safe=reflection_safe,
        reflection_profile_reason=reflection_reason,
        p0_mode=p0_mode,
        event_log=event_log,
        budget_ledger=budget_ledger,
        rotation_extra_keywords=auto_cfg.get("rotation_extra_keywords", []),
        peer_share_enabled=(auto_cfg.get("peer_share") or {}).get("enabled", False),
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
