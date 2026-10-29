from pathlib import Path

from tools.viewer.viewer_app.server import STATIC_ROOT


def test_modular_offline_hud_and_hash_tabs() -> None:
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    styles = "\n".join(path.read_text(encoding="utf-8") for path in (STATIC_ROOT / "css").glob("*.css"))
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in (STATIC_ROOT / "js").glob("*.js"))
    assert "XIAOZHI · 自主意识监控" in page and 'data-panel="seeds"' in page
    assert "种子星图" in page and "思考流" in page and "系统状态" in page
    assert "#050b14" in styles and "#00e5ff" in styles and "conic-gradient" in styles
    assert "prefers-reduced-motion" in styles and "visibilitychange" in scripts
    assert "location.hash" in scripts and "history.replaceState(null,'','#seeds')" in scripts
    assert "setInterval" in scripts and "15000" in scripts
    assert "new Map" in scripts and "requestAnimationFrame" in scripts
    assert "http://" not in page + styles + scripts and "https://" not in page + styles + scripts
    assert "<script src=" not in page and "type=\"module\"" in page


def test_modules_remain_small_and_single_entrypoint() -> None:
    root = STATIC_ROOT.parent
    assert len((root / "viewer.py").read_text(encoding="utf-8").splitlines()) < 60
    for path in [*root.joinpath("viewer_app").rglob("*.py"), *root.joinpath("static/js").glob("*.js")]:
        assert len(path.read_text(encoding="utf-8").splitlines()) < 150, path
