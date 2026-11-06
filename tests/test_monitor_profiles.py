import importlib.util
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("monitor_profiles", ROOT / "tools" / "ops" / "monitor_profiles.py")
MONITOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MONITOR)


def healthy_profile(now):
    return {
        "timer": "active",
        "service": "inactive",
        "service_active_age_s": 0,
        "last_tick_age_s": 300,
        "logs": {"traceback": 0, "nameerror": 0, "failed": 0, "timeout": 0, "exit143": 0, "invalid_json": 0},
        "queue": {"pending": 0, "oldest_age_s": 0, "failed_count": 15},
        "ticks": [
            {"ts": now - 300, "run_id": "ok-1", "status": "ok", "elapsed": 120, "tools": 8, "prefetch": 4},
            {"ts": now - 60, "run_id": "ok-2", "status": "ok", "elapsed": 90, "tools": 5, "prefetch": 3},
        ],
        "policy": {"supported": True, "db_error": False, "active_json_valid": True},
    }


def snapshot():
    now = time.time()
    return {"collected_at": now, "profiles": {p: healthy_profile(now) for p in MONITOR.PROFILES}}


def test_healthy_snapshot_is_ok_and_static_failed_baseline_is_not_an_alert():
    current = snapshot()
    previous = snapshot()
    assert MONITOR.evaluate(current, previous) == []
    assert MONITOR.render(current, [])["severity"] == "OK"


def test_new_nameerror_and_failed_queue_growth_are_critical():
    current = snapshot()
    previous = snapshot()
    current["profiles"]["xiaoai"]["logs"]["nameerror"] = 1
    current["profiles"]["xiaoai"]["queue"]["failed_count"] = 16
    issues = MONITOR.evaluate(current, previous)
    codes = {(x["level"], x["profile"], x["code"]) for x in issues}
    assert ("CRIT", "xiaoai", "NEW_EXCEPTION") in codes
    assert ("CRIT", "xiaoai", "FAILED_QUEUE_GREW") in codes


def test_zero_tool_threshold_warns_then_escalates():
    current = snapshot()
    ticks = current["profiles"]["xiaozhi"]["ticks"]
    ticks.extend({"ts": time.time(), "run_id": f"z-{i}", "status": "ok", "elapsed": 20, "tools": 0, "prefetch": 2} for i in range(2))
    issues = MONITOR.evaluate(current, current)
    assert any(x["level"] == "WARN" and x["code"] == "ZERO_TOOL_LOOP" for x in issues)
    ticks.extend({"ts": time.time(), "run_id": f"z2-{i}", "status": "ok", "elapsed": 20, "tools": 0, "prefetch": 2} for i in range(2))
    issues = MONITOR.evaluate(current, current)
    assert any(x["level"] == "CRIT" and x["code"] == "ZERO_TOOL_LOOP" for x in issues)


def test_queue_and_invalid_json_thresholds():
    current = snapshot()
    profile = current["profiles"]["xiaoai"]
    profile["queue"].update({"pending": 21, "oldest_age_s": 8000})
    profile["logs"]["invalid_json"] = 3
    issues = MONITOR.evaluate(current, current)
    assert any(x["level"] == "CRIT" and x["code"] == "QUEUE_BACKLOG" for x in issues)
    assert any(x["level"] == "CRIT" and x["code"] == "INVALID_JSON" for x in issues)


def test_missing_profile_and_stale_tick_are_critical():
    current = snapshot()
    del current["profiles"]["xiaoai"]
    current["profiles"]["xiaozhi"]["last_tick_age_s"] = 3000
    issues = MONITOR.evaluate(current, current)
    assert any(x["code"] == "PROFILE_MISSING" for x in issues)
    assert any(x["code"] == "TICK_STALE" for x in issues)


def test_service_active_too_long_is_critical():
    current = snapshot()
    current["profiles"]["xiaozhi"].update({"service": "active", "service_active_age_s": 601})
    issues = MONITOR.evaluate(current, current)
    assert any(x["code"] == "SERVICE_STUCK" for x in issues)
