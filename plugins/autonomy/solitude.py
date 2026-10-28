"""Unattended solitude/action loop; proposals are persisted, and free-text output is saved directly."""
import json, time, uuid
from .prompt import build_tick_prompt
from .proposals import parse
from .runner import _decode_or_raw


def run_solitude_tick(db, gateway, *, now=None):
    goals = [g for g in db.list_goals() if g.get("status") in ("approved", "active")]
    events = db.list_events(5)
    prompt = build_tick_prompt(goals, events, now=now)
    run_id = uuid.uuid4().hex
    sid, raw, usage = gateway.chat(goals[0] if goals else {}, run_id, "", [], prompt)
    summary, parsed, is_json = _decode_or_raw(raw)
    created = []
    for p in parsed:
        try:
            created.append(db.create_goal(p["title"], p["description"], "model")[0])
        except ValueError:
            pass
    did_text = str(summary.get("did") or summary.get("thoughts") or raw or "")
    db.append_event(
        run_id=run_id,
        kind="solitude_tick",
        exit_reason="completed",
        summary=did_text[:1000],
    )
    gateway.end(sid)
    return {
        "status": "ok",
        "run_id": run_id,
        "goal_ids": created,
        "did": summary.get("did", ""),
        "raw_text": str(raw or ""),
    }
