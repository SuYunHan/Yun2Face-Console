from pathlib import Path


def test_route_preview_has_amap_layer_with_canvas_fallback():
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")
    assert "AMap.Map" in source
    assert "map_key" in source
    assert "routeCanvas" in source
    assert "drawAmapRoute" in source
