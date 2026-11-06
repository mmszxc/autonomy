"""Policy v1 T4: rule evaluator (veto, expiry classification, promotion eligibility).

Bookkeeping only, called once per tick. Never raises.

Public API:
  veto_check(rule, now_ctx) -> {"vetoed": bool, "reason": str, "metrics": {...}}
  reuse_sessions(state_db, rule, now) -> sorted [session_id]
  decide_rules(rules, now_ctx) -> [(rule, decision)]          (pure, no writes)
  evaluate_rules(profile_home, data_dir, db, now=None, outcome_fn=None) -> summary dict
  dry_run(profile_home, data_dir=None, now=None) -> report dict  (read-only)

Writes (evaluate_rules only):
  active.json   remove vetoed/expired rules; update rule.stats (opportunities, fails, reuse_sessions)
  policy_events kind in {vetoed, expired_no_opportunity, expired_neutral, promoted_eligible}
  seed store    record_program_memory_outcome(pm_id, failed=True, counter_seed_id=rule_id) on veto only
Rules never change status here; promotion needs the acceptance reviewer (not in v1).
"""
import logging
import sys
import time
from pathlib import Path

from . import policy
from .db import POLICY_EVENT_KINDS

MIN_OPPORTUNITIES = 8
FAIL_RATE_DELTA = 0.15
FAIL_COUNT_DELTA = 2
AVOID_RATIO = 0.5
L0_CHANGED_DROP = 0.20
REUSE_MIN_SESSIONS = 3
# Families whose calls count as "non-read" follow-up (a real action after using the rule).
NON_READ_FAMILIES = frozenset({"terminal", "execute_code", "write_file", "browser", "delegate"})

KIND_VETOED = "vetoed"
KIND_EXPIRED_NO_OPP = "expired_no_opportunity"
KIND_EXPIRED_NEUTRAL = "expired_neutral"
KIND_PROMOTED_ELIGIBLE = "promoted_eligible"
for _k in (KIND_VETOED, KIND_EXPIRED_NO_OPP, KIND_EXPIRED_NEUTRAL, KIND_PROMOTED_ELIGIBLE):
    assert _k in POLICY_EVENT_KINDS

_SEED_SRC = Path(__file__).resolve().parents[1] / "seed_memory" / "seed_src"
_log = logging.getLogger(__name__)


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- veto

def _post_pre(autonomy_db, state_db, rule, now):
    """Metrics over [created, now] (post) and the equal-length window before creation (pre)."""
    fam = rule.get("tool_family") or ""
    created = _f(rule.get("created_at"), now)
    days = max((now - created) / 86400.0, 1e-6)
    post = policy.baseline(autonomy_db, state_db, fam, now, days=days)
    pre = policy.baseline(autonomy_db, state_db, fam, created, days=days)
    return post, pre


def veto_check(rule, now_ctx):
    """Veto only when opportunities >= MIN_OPPORTUNITIES. Any single signal vetoes.

    now_ctx: {"now": float, "autonomy_db": conn|None, "state_db": conn|None}
    """
    now = _f(now_ctx.get("now"), time.time())
    adb, sdb = now_ctx.get("autonomy_db"), now_ctx.get("state_db")
    fam = rule.get("tool_family") or ""
    created = _f(rule.get("created_at"), now)
    opp = policy.opportunities(sdb, fam, since_ts=created)
    metrics = {"opportunities": opp["sessions"], "fail_rate_now": opp["fail_rate"]}
    if opp["sessions"] < MIN_OPPORTUNITIES:
        return {"vetoed": False, "reason": "opportunities<%d" % MIN_OPPORTUNITIES, "metrics": metrics}

    post, pre = _post_pre(adb, sdb, rule, now)
    base = rule.get("baseline") or {}
    b_fr = _f(base.get("fail_rate"))
    b_apt = _f(base.get("attempts_per_tick"))
    b_l0 = _f(base.get("l0_changed_rate"))
    metrics.update({
        "base_fail_rate": b_fr, "post_fail_rate": post["fail_rate"],
        "post_fails": post["fails"], "pre_fails": pre["fails"],
        "base_attempts_per_tick": b_apt, "post_attempts_per_tick": post["attempts_per_tick"],
        "post_ticks": post["ticks"],
        "base_l0_changed_rate": b_l0, "post_l0_changed_rate": post["l0_changed_rate"],
        "post_l0_checks": post["l0_checks"],
    })
    reasons = []
    if post["fail_rate"] - b_fr >= FAIL_RATE_DELTA and post["fails"] - pre["fails"] >= FAIL_COUNT_DELTA:
        reasons.append("fail_rate_up")
    if b_apt > 0 and post["ticks"] > 0 and post["attempts_per_tick"] < AVOID_RATIO * b_apt:
        reasons.append("avoidance")
    if post["l0_checks"] > 0 and b_l0 - post["l0_changed_rate"] >= L0_CHANGED_DROP:
        reasons.append("l0_changed_drop")
    return {"vetoed": bool(reasons), "reason": ",".join(reasons) or "none", "metrics": metrics}


# ---------------------------------------------------------------- reuse

def reuse_sessions(state_db, rule, now):
    """Sessions where the rule's family was called and a later non-read call happened in the same session."""
    fam = rule.get("tool_family") or ""
    members = set(policy._family_members(fam))
    if not members or state_db is None:
        return []
    nonread_names = [k for k, v in policy._FAMILY.items() if v in NON_READ_FAMILIES]
    names = sorted(members | set(nonread_names))
    created = _f(rule.get("created_at"), now)
    rows = policy._q(state_db,
                     "select session_id, tool_name from messages where role='tool' and timestamp>=? and timestamp<? "
                     "and tool_name IN (%s) order by session_id, id" % ",".join("?" * len(names)),
                     [created, now] + names)
    seen_family, qualified = set(), set()
    for sid, tn in rows:
        if not sid:
            continue
        if sid in seen_family and policy.tool_family(tn) in NON_READ_FAMILIES:
            qualified.add(sid)
        if tn in members:
            seen_family.add(sid)
    return sorted(qualified)


# ---------------------------------------------------------------- decide

def _has_event(db, rule_id, kind):
    try:
        with db.lock:
            return db.c.execute("select 1 from policy_events where rule_id=? and kind=? limit 1",
                                (rule_id, kind)).fetchone() is not None
    except Exception:
        return False


def _decide_one(rule, ctx, already_promoted=False):
    now = ctx["now"]
    veto = veto_check(rule, ctx)
    opps = veto["metrics"]["opportunities"]
    created = _f(rule.get("created_at"), now)
    age_days = (now - created) / 86400.0
    ttl_days = _f(rule.get("ttl_days"), 7)
    ttl_opp = _f(rule.get("ttl_opportunities"), 30)
    reuse = reuse_sessions(ctx.get("state_db"), rule, now) if opps >= MIN_OPPORTUNITIES else []
    decision = {"opportunities": opps, "veto": veto, "reuse_sessions": reuse,
                "age_days": round(age_days, 3), "post_fails": veto["metrics"].get("post_fails")}
    if veto["vetoed"]:
        decision.update(action=KIND_VETOED, reason=veto["reason"])
    elif rule.get("status", "candidate") == "candidate" and (age_days > ttl_days or opps >= ttl_opp):
        if opps < MIN_OPPORTUNITIES:
            decision.update(action=KIND_EXPIRED_NO_OPP, reason="expired_with_opportunities=%d" % opps)
        else:
            decision.update(action=KIND_EXPIRED_NEUTRAL, reason="expired_neutral opportunities=%d" % opps)
    elif opps >= MIN_OPPORTUNITIES and len(reuse) >= REUSE_MIN_SESSIONS and not already_promoted:
        decision.update(action=KIND_PROMOTED_ELIGIBLE, reason="reuse_sessions=%d" % len(reuse))
    else:
        decision.update(action="skip", reason="keep")
    return decision


def decide_rules(rules, now_ctx):
    """Pure: returns [(rule, decision)]. Per-rule failures become action 'error'."""
    out = []
    for r in rules:
        try:
            out.append((r, _decide_one(r, now_ctx, now_ctx.get("promoted_ids", {}).get(r.get("id"), False))))
        except Exception as e:
            _log.debug("policy_eval: decide failed: %s", type(e).__name__)
            out.append((r, {"action": "error", "reason": "%s" % type(e).__name__}))
    return out


def _ctx(state_db, autonomy_db, now, promoted_ids=None):
    return {"now": now, "state_db": state_db, "autonomy_db": autonomy_db, "promoted_ids": promoted_ids or {}}


def _summary_of(decided):
    summary = {"candidates": len(decided), "vetoed": [], "expired_no_opportunity": [],
               "expired_neutral": [], "promoted_eligible": [], "skipped": [], "errors": []}
    key = {KIND_VETOED: "vetoed", KIND_EXPIRED_NO_OPP: "expired_no_opportunity",
           KIND_EXPIRED_NEUTRAL: "expired_neutral", KIND_PROMOTED_ELIGIBLE: "promoted_eligible",
           "skip": "skipped", "error": "errors"}
    for r, d in decided:
        summary[key.get(d["action"], "skipped")].append(
            {"id": r.get("id"), "opportunities": d.get("opportunities"), "reason": d.get("reason"),
             "reuse_sessions": len(d.get("reuse_sessions") or [])})
    return summary


# ---------------------------------------------------------------- apply

def _default_outcome_fn(pm_id, failed, counter_seed_id=None, source="auto", profile_home=None):
    """Record outcomes in the selected profile's seed database."""
    try:
        if str(_SEED_SRC) not in sys.path:
            sys.path.append(str(_SEED_SRC))
        from hermes.memory import store
        if profile_home:
            home = Path(profile_home)
            store.configure(home / "seed_data" / "seeds" / "seeds.db")
        return store.record_program_memory_outcome(
            pm_id, failed=failed, counter_seed_id=counter_seed_id, source=source
        )
    except Exception as e:
        return {"found": None, "recorded": False, "reason": "error:%s" % type(e).__name__}


def _apply_stats(data_dir, decided):
    """Write back opportunities/fails/reuse_sessions for rules still active after this tick."""
    fresh = policy.load_active_rules(data_dir)
    by_id = {r.get("id"): d for r, d in decided if d.get("action") not in (KIND_VETOED, KIND_EXPIRED_NO_OPP,
                                                                         KIND_EXPIRED_NEUTRAL, "error")}
    changed = False
    for r in fresh:
        d = by_id.get(r["id"])
        if d is None:
            continue
        st = dict(r.get("stats") or {})
        new = {"opportunities": d["opportunities"], "fails": d.get("post_fails") or 0,
               "reuse_sessions": d["reuse_sessions"]}
        if any(st.get(k) != v for k, v in new.items()):
            r["stats"] = dict(st, **new)
            changed = True
    if changed:
        return policy.save_active_rules(data_dir, fresh)
    return {"ok": True, "skipped": True}


def _apply(data_dir, db, profile, decided, outcome_fn):
    actions = []
    for rule, d in decided:
        rid = rule.get("id")
        act = d.get("action")
        if act in ("skip", "error"):
            continue
        try:
            if act == KIND_PROMOTED_ELIGIBLE:
                db.record_policy_event(rid, KIND_PROMOTED_ELIGIBLE, d["reason"],
                                       {"reuse_sessions": d["reuse_sessions"], "opportunities": d["opportunities"]},
                                       profile)
                actions.append((rid, act))
                continue
            if act == KIND_VETOED:
                res = policy.remove_rule(data_dir, rid, d["reason"], source="auto")
                if not res.get("ok"):
                    continue
                outcome = None
                pm_id = rule.get("source_pm_id") or ""
                if pm_id:
                    try:
                        outcome = outcome_fn(pm_id, failed=True, counter_seed_id=rid, source="auto")
                    except Exception as e:
                        outcome = {"recorded": False, "reason": "error:%s" % type(e).__name__}
                db.record_policy_event(rid, KIND_VETOED, d["reason"],
                                       {"metrics": d["veto"]["metrics"], "source_pm_id": pm_id,
                                        "outcome": outcome}, profile)
            else:  # expired_*
                res = policy.remove_rule(data_dir, rid, d["reason"], source=act)
                if not res.get("ok"):
                    continue
                db.record_policy_event(rid, act, d["reason"],
                                       {"opportunities": d["opportunities"], "age_days": d["age_days"]}, profile)
            actions.append((rid, act))
        except Exception as e:
            _log.debug("policy_eval: apply failed for %s: %s", rid, type(e).__name__)
    return actions


def evaluate_rules(profile_home, data_dir, db, now=None, outcome_fn=None):
    """Per-tick bookkeeping. Returns a summary dict; never raises."""
    try:
        now = _f(now, time.time())
        if outcome_fn is None:
            outcome_fn = lambda **kwargs: _default_outcome_fn(
                **kwargs, profile_home=profile_home
            )
        profile = Path(profile_home).name if profile_home else ""
        rules = policy.load_active_rules(data_dir)
        if not rules:
            return {"ok": True, "candidates": 0, "actions": []}
        sdb = policy.open_state_db(profile_home)
        adb = policy.open_autonomy_db(profile_home)
        try:
            promoted = {r["id"]: _has_event(db, r["id"], KIND_PROMOTED_ELIGIBLE) for r in rules}
            decided = decide_rules(rules, _ctx(sdb, adb, now, promoted))
            actions = _apply(data_dir, db, profile, decided, outcome_fn)
            # Apply stats after actions, from a fresh active.json read, so a
            # concurrent rollback cannot be overwritten by stale state.
            _apply_stats(data_dir, decided)
            summary = _summary_of(decided)
            summary.update(ok=True, actions=actions)
            return summary
        finally:
            for c in (sdb, adb):
                try:
                    if c is not None:
                        c.close()
                except Exception:
                    pass
    except Exception as e:
        _log.warning("policy_eval: evaluate_rules degraded: %s", type(e).__name__)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120]), "actions": []}


def rule_rows(profile_home, data_dir=None, now=None):
    """Read-only per-rule view for `autonomy policy show`: [] on any failure, never raises."""
    try:
        now = _f(now, time.time())
        home = Path(profile_home)
        data_dir = data_dir or (home / "autonomy_data")
        rules = policy.load_active_rules(data_dir)
        sdb = policy.open_state_db(home)
        adb = policy.open_autonomy_db(home)
        try:
            decided = decide_rules(rules, _ctx(sdb, adb, now))
        finally:
            for c in (sdb, adb):
                try:
                    if c is not None:
                        c.close()
                except Exception:
                    pass
        rows = []
        for r, d in decided:
            veto = d.get("veto") or {}
            rows.append({
                "id": r.get("id"), "status": r.get("status"), "channel": r.get("channel"),
                "ttl": "%sd/%sopp" % (r.get("ttl_days"), r.get("ttl_opportunities")),
                "opportunities": (r.get("stats") or {}).get("opportunities", 0),
                "veto_reason": veto.get("reason", "n/a"), "veto_metrics": veto.get("metrics") or {},
                "text": r.get("text", ""),
            })
        return rows
    except Exception:
        return []


def dry_run(profile_home, data_dir=None, now=None):
    """Read-only report: no active.json write, no DB write, no outcome call."""
    try:
        now = _f(now, time.time())
        home = Path(profile_home)
        data_dir = data_dir or (home / "autonomy_data")
        rules = policy.load_active_rules(data_dir)
        sdb = policy.open_state_db(home)
        adb = policy.open_autonomy_db(home)
        try:
            decided = decide_rules(rules, _ctx(sdb, adb, now))
        finally:
            for c in (sdb, adb):
                try:
                    if c is not None:
                        c.close()
                except Exception:
                    pass
        rep = _summary_of(decided)
        rep.update(ok=True, profile=home.name, state_db_open=sdb is not None, autonomy_db_open=adb is not None)
        return rep
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}
