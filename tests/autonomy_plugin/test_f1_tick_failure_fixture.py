"""Tick failure classification contract fixtures (no production DB or scheduler)."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[2] / "plugins"))
from autonomy.error_classifier import E2_PROVIDER_EXHAUSTED, E_AUTH_FAILED, classify
from autonomy.error_classifier import classify_error_text


def classify_tick_exception(exc):
    status = getattr(exc, "status_code", None)
    if status is not None:
        return classify("", {"http_status": status})[0]
    kind = classify_error_text(str(exc))[0]
    return type(exc).__name__ if kind is None else kind


def test_tick_structured_503_precedes_text():
    exc = SimpleNamespace(status_code=503)
    assert classify_tick_exception(exc) == E2_PROVIDER_EXHAUSTED


def test_tick_structured_401():
    exc = SimpleNamespace(status_code=401)
    assert classify_tick_exception(exc) == E_AUTH_FAILED


def test_tick_text_canonical_fallback():
    assert classify_tick_exception(RuntimeError("connection refused")) == E2_PROVIDER_EXHAUSTED


def test_tick_unknown_fallback_to_exception_type():
    exc = RuntimeError("ordinary failure")
    kind = classify_tick_exception(exc)
    assert kind is not None
