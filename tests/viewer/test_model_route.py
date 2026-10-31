from pathlib import Path

from tools.viewer.viewer_app.sources import model_route


def test_model_route_allowlists_config_and_preserves_chain(profile: Path) -> None:
    (profile / "config.yaml").write_text(
        "model: main-model\nprovider: custom:main\n"
        "fallback_providers:\n"
        "  - provider: custom:backup\n    model: backup-model\n"
        "api_key: MUST_NOT_LEAK\n",
        encoding="utf-8",
    )
    data = model_route.load(profile, "demo")
    assert data["primary"] == {"provider": "custom:main", "model": "main-model"}
    assert data["fallback_providers"] == [{"provider": "custom:backup", "model": "backup-model"}]
    assert data["summary"] == "一直使用主路由"
    assert "MUST_NOT_LEAK" not in repr(data)


def test_model_route_normalises_latest_fallback_without_raw_log(profile: Path) -> None:
    (profile / "config.yaml").write_text("model: main\nprovider: custom:main\n", encoding="utf-8")
    logs = profile / "logs"
    logs.mkdir()
    (logs / "gateway.log").write_text(
        "2026-10-31 04:00:00 Provider fallback: secret-url unavailable; using backup after HTTP 502\n",
        encoding="utf-8",
    )
    data = model_route.load(profile, "demo")
    assert data["last_fallback"] == {
        "time": "2026-10-31 04:00:00",
        "reason_code": "upstream",
        "reason": "上游不可用",
    }
    assert "secret-url" not in repr(data)


def test_model_route_rejects_environment_and_url_values(profile: Path) -> None:
    (profile / "config.yaml").write_text(
        "model: ${SECRET_MODEL}\nprovider: https://private.example\n",
        encoding="utf-8",
    )
    data = model_route.load(profile, "demo")
    assert data["primary"] is None
