from pathlib import Path

from tools.viewer.viewer_app.server import STATIC_ROOT


def test_modular_offline_hud_and_hash_tabs() -> None:
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    styles = "\n".join(path.read_text(encoding="utf-8") for path in (STATIC_ROOT / "css").glob("*.css"))
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in (STATIC_ROOT / "js").glob("*.js"))
    assert 'id="hudTitle"' in page and 'data-panel="seeds"' in page
    assert "data.profile" in scripts and "document.title=title" in scripts
    assert "种子星图" in page and "思考流" in page and "系统状态" in page
    assert "#050b14" in styles and "#00e5ff" in styles and "conic-gradient" in styles
    assert "prefers-reduced-motion" in styles and "visibilitychange" in scripts
    assert "location.hash" in scripts and "history.replaceState(null,'','#seeds')" in scripts
    assert "startRefresh" in scripts and "viewer:updated" in scripts
    assert 'id="refreshNow"' in page and 'id="lastUpdated"' in page
    assert "手动刷新" in page
    assert 'id="pauseToggle"' not in page and 'id="pollInterval"' not in page
    assert "new Map" in scripts and "requestAnimationFrame" in scripts
    assert "http://" not in page + styles + scripts and "https://" not in page + styles + scripts
    assert "<script src=" not in page and "type=\"module\"" in page


def test_manual_refresh_only_and_incremental_rendering() -> None:
    refresh = (STATIC_ROOT / "js/refresh.js").read_text(encoding="utf-8")
    app = (STATIC_ROOT / "js/app.js").read_text(encoding="utf-8")
    seeds = (STATIC_ROOT / "js/seed-map.js").read_text(encoding="utf-8")
    thoughts = (STATIC_ROOT / "js/thought-stream.js").read_text(encoding="utf-8")
    assert "refreshNow" in refresh and "viewer:refresh" in refresh
    assert "setTimeout" not in refresh + app and "setInterval" not in refresh + app
    assert "localStorage" not in refresh and "viewer:pause" not in refresh + app
    assert "dataset.signature" in seeds and "dataset.signature" in thoughts
    assert "root.children[index]!==node" in seeds and "root.children[index]!==node" in thoughts


def test_model_route_card_uses_text_content() -> None:
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    script = (STATIC_ROOT / "js/model-route.js").read_text(encoding="utf-8")
    assert "模型路由" in page and "modelRouteFallbacks" in page
    assert "/api/model-route" in script
    assert "textContent" in script and "innerHTML" not in script


def test_notes_to_team_card_is_hidden_and_uses_safe_text_content() -> None:
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    notes = (STATIC_ROOT / "js/notes.js").read_text(encoding="utf-8")
    assert 'id="notesToTeamCard" class="glass" hidden' in page
    assert "给团队的留言" in page
    assert "/api/notes-to-team" in notes
    assert "textContent" in notes and "innerHTML" not in notes
    assert "setTimeout" not in notes and "setInterval" not in notes


def test_modules_remain_small_and_single_entrypoint() -> None:
    root = STATIC_ROOT.parent
    assert len((root / "viewer.py").read_text(encoding="utf-8").splitlines()) < 60
    for path in [*root.joinpath("viewer_app").rglob("*.py"), *root.joinpath("static/js").glob("*.js")]:
        assert len(path.read_text(encoding="utf-8").splitlines()) < 150, path
