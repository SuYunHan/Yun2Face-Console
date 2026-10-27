from pathlib import Path


def test_history_preview_uses_amap_and_canvas_fallback():
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "drawAmapHistory" in source
    assert "historyMap" in html
    assert "showHistoryCanvas" in source
